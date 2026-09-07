"""格式转换模块的 HTTP 接口（独立 Blueprint）。

挂载方式（server.py 只需两行）：
    from convert_api import convert_bp, init_convert
    init_convert(...); app.register_blueprint(convert_bp)

本文件不修改任何既有路由，也不触碰下载任务的数据文件。
"""
from __future__ import annotations

import mimetypes
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file

from core import convert_engine as engine

convert_bp = Blueprint("convert", __name__, url_prefix="/api/convert")

_STAGED = {}
_STAGED_LOCK = threading.Lock()
_STAGE_TTL = 2 * 3600  # 暂存文件 2 小时未使用即视为过期
_LOG = None


def init_convert(output_dir, temp_dir, media_tool=None, probe_tool=None, logger=None, base_dir=None):
    """由宿主应用注入运行环境；重复调用安全。"""
    global _LOG
    _LOG = logger
    engine.configure(
        output_dir=output_dir,
        temp_dir=temp_dir,
        media_tool=media_tool,
        probe_tool=probe_tool,
        logger=logger,
        base_dir=base_dir,
    )
    try:
        engine.purge_temp()
    except Exception:
        pass
    return True


def _sweep_staged():
    deadline = time.time() - _STAGE_TTL
    with _STAGED_LOCK:
        expired = [k for k, v in _STAGED.items() if v["ts"] < deadline]
        for key in expired:
            item = _STAGED.pop(key, None)
            if item:
                try:
                    os.remove(item["path"])
                except Exception:
                    pass


def _fail(message, code=400):
    return jsonify({"error": message}), code


@convert_bp.route("/capabilities", methods=["GET"])
def api_capabilities():
    return jsonify(engine.capabilities())


@convert_bp.route("/upload", methods=["POST"])
def api_upload():
    """接收单个待转换文件，落到独立临时目录并返回句柄。"""
    _sweep_staged()
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return _fail("没有收到文件")
    name = uploaded.filename
    kind = engine.guess_kind(name)
    if not kind:
        return _fail("暂不支持这种文件类型")
    caps = engine.capabilities()
    if kind == "image" and not caps["image"]:
        return _fail("本机暂不具备图片转换能力")
    if kind == "video" and not caps["video"]:
        return _fail("本机暂不具备视频转换能力")
    try:
        path, size = engine.stage_upload(uploaded, name)
    except Exception as exc:  # noqa: BLE001
        return _fail(f"文件暂存失败：{exc}", 500)
    if size <= 0:
        try:
            os.remove(path)
        except Exception:
            pass
        return _fail("文件内容为空")
    if size > engine.MAX_UPLOAD_BYTES:
        try:
            os.remove(path)
        except Exception:
            pass
        return _fail("文件超过 4GB 上限")
    token = uuid.uuid4().hex[:16]
    with _STAGED_LOCK:
        _STAGED[token] = {"path": str(path), "name": name, "size": size,
                          "kind": kind, "ts": time.time()}
    return jsonify({"token": token, "name": name, "size": size, "kind": kind})


@convert_bp.route("/jobs", methods=["POST"])
def api_create_jobs():
    """把已上传的文件批量提交转换。"""
    data = request.get_json(silent=True) or {}
    items = data.get("items") or []
    if not isinstance(items, list) or not items:
        return _fail("没有待转换的文件")
    if len(items) > 100:
        return _fail("单次最多提交 100 个文件")

    caps = engine.capabilities()
    created, errors = [], []
    for raw in items:
        token = str((raw or {}).get("token") or "")
        with _STAGED_LOCK:
            staged = _STAGED.get(token)
        if not staged:
            errors.append({"token": token, "error": "文件已过期，请重新添加"})
            continue
        kind = staged["kind"]
        target = str((raw or {}).get("target") or "").lower()
        table = engine.VIDEO_FORMATS if kind == "video" else engine.IMAGE_FORMATS
        if target not in table:
            errors.append({"token": token, "error": "输出格式无效"})
            continue
        if kind == "video" and not caps["video"]:
            errors.append({"token": token, "error": "本机暂不具备视频转换能力"})
            continue
        if kind == "image" and not caps["image"]:
            errors.append({"token": token, "error": "本机暂不具备图片转换能力"})
            continue
        options = (raw or {}).get("options") or {}
        if not isinstance(options, dict):
            options = {}
        job = engine.ConvertJob(
            kind=kind, source_name=staged["name"], source_path=staged["path"],
            source_size=staged["size"], target=target, options=options,
            output_dir=(raw or {}).get("output_dir") or "",
        )
        engine.manager.submit(job)
        with _STAGED_LOCK:
            _STAGED.pop(token, None)
        created.append(job.to_dict())

    if not created and errors:
        return jsonify({"created": [], "errors": errors}), 400
    return jsonify({"created": created, "errors": errors})


@convert_bp.route("/jobs", methods=["GET"])
def api_list_jobs():
    jobs = engine.manager.list()
    active = sum(1 for j in jobs if j["status"] in ("queued", "running"))
    return jsonify({"jobs": jobs, "active": active})


@convert_bp.route("/jobs/<job_id>/cancel", methods=["POST"])
def api_cancel_job(job_id):
    if not engine.manager.cancel(job_id):
        return _fail("该任务已结束，无法取消")
    return jsonify({"ok": True})


@convert_bp.route("/jobs/<job_id>", methods=["DELETE"])
def api_delete_job(job_id):
    delete_output = str(request.args.get("purge") or "") == "1"
    if not engine.manager.remove(job_id, delete_output=delete_output):
        return _fail("记录不存在", 404)
    return jsonify({"ok": True})


@convert_bp.route("/jobs/clear", methods=["POST"])
def api_clear_jobs():
    return jsonify({"ok": True, "removed": engine.manager.clear_finished()})


@convert_bp.route("/jobs/<job_id>/file", methods=["GET"])
def api_job_file(job_id):
    """提供转换产物：inline 预览或附件下载。"""
    job = engine.manager.get(job_id)
    if not job or job.status != "done" or not job.output_path:
        return _fail("产物不存在", 404)
    path = Path(job.output_path)
    root = Path(job.output_dir).resolve() if getattr(job, "output_dir", None) else Path(engine._cfg.output_dir).resolve()
    try:
        resolved = path.resolve()
    except Exception:
        return _fail("产物路径无效", 400)
    if root not in resolved.parents:
        return _fail("产物路径无效", 400)
    if not resolved.is_file():
        return _fail("产物已被移动或删除", 404)
    table = engine.VIDEO_FORMATS if job.kind == "video" else engine.IMAGE_FORMATS
    mime = table.get(job.target, {}).get("mime") or \
        mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
    as_attachment = str(request.args.get("mode") or "") != "inline"
    return send_file(str(resolved), mimetype=mime, as_attachment=as_attachment,
                     download_name=resolved.name, conditional=True)


@convert_bp.route("/open-output", methods=["POST"])
def api_open_output():
    """在系统文件管理器中打开输出目录或选中某产物（仅本机可用）。

    返回被打开的路径，便于前端在无法弹出资源管理器（如远程/沙箱）时
    仍能以提示+复制路径的方式给用户明确反馈。
    """
    target = Path(engine._cfg.output_dir)
    try:
        data = request.get_json(silent=True) or {}
        preferred = str(data.get("output_dir") or "").strip()
        if preferred:
            target = Path(preferred)
        target.mkdir(parents=True, exist_ok=True)
        job_id = str(data.get("job_id") or "")
        opened = None
        if job_id:
            job = engine.manager.get(job_id)
            if job and job.output_path and os.path.exists(job.output_path):
                opened = os.path.normpath(job.output_path)
                if sys.platform.startswith("win"):
                    subprocess.Popen(["explorer", f'/select,"{opened}"'])
                    return jsonify({"ok": True, "path": opened})
                if sys.platform == "darwin":
                    subprocess.Popen(["open", "-R", opened])
                else:
                    subprocess.Popen(["xdg-open", opened])
                return jsonify({"ok": True, "path": opened})
        opened = str(target)
        if sys.platform.startswith("win"):
            subprocess.Popen(["explorer", os.path.normpath(opened)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", opened])
        else:
            subprocess.Popen(["xdg-open", opened])
        return jsonify({"ok": True, "path": opened})
    except Exception as exc:  # noqa: BLE001
        return _fail(f"无法打开输出位置：{exc}", 500)
