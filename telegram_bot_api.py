"""Telegram Bot 扩展模块（独立 Blueprint）。

弱绑定约束（务必保持）：
- 只依赖 Flask 与本模块的 core/telegram_bot_*，不反向 import server。
- 与主程序的唯一接触面是 init_telegram_bot() 注入的回调（提交任务 / 查询任务）。
- server.py 中的挂载被 try/except 包裹：本模块任何异常都不影响主程序。
- 删除本文件且不挂载，即完全卸载，主程序功能不受任何影响。

模块默认关闭（enabled=False），需使用者主动启用。
"""

from __future__ import annotations

import os

from flask import Blueprint, jsonify, request

from core.telegram_bot_runner import TelegramBotRunner
from core.telegram_bot_store import TelegramBotStore

telegram_bot_bp = Blueprint("telegram_bot", __name__, url_prefix="/api/telegram-bot")

_runner: TelegramBotRunner | None = None
_store: TelegramBotStore | None = None
_logger = None
_downloads_dir = ""
_submit_task_fn = None
_mounted = False

# 可配置的字段白名单：其余字段一律忽略，避免配置被意外污染
_CONFIGURABLE = (
    "token", "whitelist", "auto_start", "local_api_url",
    "local_max_send_mb", "proxy", "owner_chat_id", "translation",
)


def _log(msg: str, level: str = "info") -> None:
    if _logger:
        getattr(_logger, level, _logger.info)(f"[telegram-bot] {msg}")


def init_telegram_bot(data_dir, downloads_dir, logger=None,
                      submit_task=None, get_task=None, extract_caption=None):
    """由 server.py 调用，注入依赖并准备好模块。

    与 convert 模块保持同一挂载范式：注入式初始化，零全局副作用。
    """
    global _runner, _store, _logger, _downloads_dir, _submit_task_fn, _mounted

    _logger = logger
    _downloads_dir = str(downloads_dir) if downloads_dir else ""
    _submit_task_fn = submit_task

    _store = TelegramBotStore(data_dir, logger=logger)

    def _submit(url, output_name="", format_id="", output_dir=""):
        """提交下载：默认保存到本模块的下载目录。"""
        if not _submit_task_fn:
            raise RuntimeError("下载入口未注入")
        return _submit_task_fn(url, output_name, format_id,
                               output_dir or _downloads_dir)

    def _get(task_id):
        return get_task(task_id) if get_task else None

    _runner = TelegramBotRunner(
        _store,
        _submit,
        _get,
        logger=logger,
        extract_caption=extract_caption,
    )

    # 默认关闭：只有使用者显式开启自动启动，才会随主程序拉起
    cfg = _store.get_config()
    _mounted = True
    if cfg.get("enabled") and cfg.get("auto_start") and cfg.get("token"):
        ok, msg = _runner.start()
        _log(f"自动启动：{msg}", "info" if ok else "warning")
    return telegram_bot_bp


# ---------------------------------------------------------------- 状态

@telegram_bot_bp.route("/status", methods=["GET"])
def api_tgb_status():
    cfg = _store.public_config() if _store else {}
    live = _runner.status() if _runner else {}
    return jsonify({
        "available": _mounted,
        "module_ok": True,
        "enabled": bool((_store.get_config() if _store else {}).get("enabled")),
        "running": bool(live.get("running")),
        "username": live.get("username", ""),
        "local_available": bool(live.get("local_available")),
        "local_configured": bool(live.get("local_configured")),
        "pending": live.get("pending", 0),
        "last_error": live.get("last_error", ""),
        "access_configured": bool(_store and _store.has_access_policy()),
        "downloads_dir": _downloads_dir,
        "config": cfg,
    })


# ---------------------------------------------------------------- 配置

@telegram_bot_bp.route("/config", methods=["GET"])
def api_tgb_get_config():
    if not _store:
        return jsonify({"error": "模块未加载"}), 503
    return jsonify({"config": _store.public_config()})


@telegram_bot_bp.route("/config", methods=["POST"])
def api_tgb_set_config():
    if not _store:
        return jsonify({"error": "模块未加载"}), 503
    data = request.get_json(silent=True) or {}
    patch = {k: v for k, v in data.items() if k in _CONFIGURABLE}
    if not patch:
        return jsonify({"error": "没有可更新的配置项"}), 400

    if "token" in patch and not str(patch["token"]).strip():
        # 空值表示不修改（前端回显的是脱敏串），避免误清空已保存的凭据
        patch.pop("token")

    try:
        config = _store.update_config(patch)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "config": config})


# ---------------------------------------------------------------- 启停

@telegram_bot_bp.route("/start", methods=["POST"])
def api_tgb_start():
    if not _store or not _runner:
        return jsonify({"error": "模块未加载"}), 503
    if not _store.raw_token().strip():
        return jsonify({"error": "请先填写令牌"}), 400

    _store.update_config({"enabled": True})
    _runner._local_checked_at = 0.0
    ok, msg = _runner.start()
    _store.add_log(f"启动：{msg}", "info" if ok else "error")
    return jsonify({"ok": ok, "message": msg})


@telegram_bot_bp.route("/stop", methods=["POST"])
def api_tgb_stop():
    if not _store or not _runner:
        return jsonify({"error": "模块未加载"}), 503
    ok, msg = _runner.stop()
    _store.update_config({"enabled": False})
    _store.add_log(f"停止：{msg}", "info" if ok else "warning")
    return jsonify({"ok": ok, "message": msg})


# ---------------------------------------------------------------- 翻译

@telegram_bot_bp.route("/translation/providers", methods=["GET"])
def api_tgb_translation_providers():
    """返回可用翻译源及其所需字段，供前端动态渲染配置表单。"""
    try:
        from core.translate import PROVIDERS
        return jsonify({"providers": PROVIDERS})
    except Exception as e:  # noqa: BLE001
        safe_error = _store.redact_text(e) if _store else ""
        _log(f"读取翻译服务列表失败: {safe_error}", "warning")
        return jsonify({"error": "暂时无法读取翻译服务列表"}), 500


@telegram_bot_bp.route("/translation/test", methods=["POST"])
def api_tgb_translation_test():
    """用传入的临时配置做一次翻译验证（不持久化，不影响已保存配置）。"""
    data = request.get_json(silent=True) or {}
    try:
        from core.translate import build_translator
        cfg = {
            "enabled": True,
            "provider": data.get("provider", "deepseek"),
            "api_key": data.get("api_key", ""),
            "secret_id": data.get("secret_id", ""),
            "secret_key": data.get("secret_key", ""),
            "base_url": data.get("base_url", ""),
            "model": data.get("model", ""),
            "region": data.get("region", "ap-guangzhou"),
            "target_lang": data.get("target_lang", "zh"),
        }
        tr = build_translator(cfg)
        if not tr:
            return jsonify({"ok": False, "error": "不支持的翻译源或未启用"})
        result = tr.translate("Hello, world! This is a test.", "zh")
        return jsonify({"ok": True, "result": result})
    except Exception as e:  # noqa: BLE001
        safe_error = _store.redact_text(e) if _store else ""
        _log(f"翻译测试失败: {safe_error}", "warning")
        return jsonify({"ok": False, "error": "翻译测试失败，请检查凭证和网络设置"})


# ---------------------------------------------------------------- 日志

@telegram_bot_bp.route("/logs", methods=["GET"])
def api_tgb_logs():
    if not _store:
        return jsonify({"logs": []})
    limit = request.args.get("limit", "50")
    try:
        limit = max(1, min(200, int(limit)))
    except ValueError:
        limit = 50
    return jsonify({"logs": _store.get_logs(limit)})


@telegram_bot_bp.route("/logs/clear", methods=["POST"])
def api_tgb_clear_logs():
    if not _store:
        return jsonify({"ok": False}), 503
    _store.clear_logs()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- 维护

@telegram_bot_bp.route("/mappings/prune", methods=["POST"])
def api_tgb_prune():
    """清理过期映射记录，避免长期运行后残留数据堆积。"""
    if not _store:
        return jsonify({"ok": False}), 503
    removed = _store.prune_mappings()
    return jsonify({"ok": True, "removed": removed})


@telegram_bot_bp.route("/downloads", methods=["GET"])
def api_tgb_downloads():
    """列出已下载文件，便于界面展示与快速定位。"""
    items = []
    if _downloads_dir and os.path.isdir(_downloads_dir):
        try:
            for name in sorted(os.listdir(_downloads_dir)):
                path = os.path.join(_downloads_dir, name)
                if not os.path.isfile(path):
                    continue
                items.append({
                    "name": name,
                    "path": path,
                    "size": os.path.getsize(path),
                })
        except Exception as exc:  # noqa: BLE001
            _log(f"读取下载目录失败: {exc}", "warning")
    return jsonify({"dir": _downloads_dir, "items": items[-100:]})
