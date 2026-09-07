"""格式转换引擎（独立模块）。

设计约束：
1. 本模块不 import server.py，也不读写 tasks.json / groups.json，
   与下载链路完全解耦；所有外部依赖通过 configure() 注入。
2. 任务只存在于内存，进程退出即清空（产物文件仍留在磁盘）。
   这样不会与下载任务的持久化文件产生任何竞争。
3. 图片优先使用 Pillow；缺失时回退到本机媒体组件；两者都没有时如实报告不可用。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

try:  # 可选依赖，缺失时自动降级
    from PIL import Image, ImageOps, ImageSequence

    _PIL_OK = True
except Exception:  # pragma: no cover - 取决于运行环境
    Image = ImageOps = ImageSequence = None
    _PIL_OK = False

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# ------------------------------------------------------------------
# 格式定义
# ------------------------------------------------------------------
IMAGE_FORMATS = {
    "jpg": {"label": "JPG", "ext": ".jpg", "mime": "image/jpeg", "pil": "JPEG",
            "quality": True, "alpha": False, "animated": False},
    "png": {"label": "PNG", "ext": ".png", "mime": "image/png", "pil": "PNG",
            "quality": True, "alpha": True, "animated": False},
    "webp": {"label": "WEBP", "ext": ".webp", "mime": "image/webp", "pil": "WEBP",
             "quality": True, "alpha": True, "animated": True},
    "bmp": {"label": "BMP", "ext": ".bmp", "mime": "image/bmp", "pil": "BMP",
            "quality": False, "alpha": False, "animated": False},
    "gif": {"label": "GIF", "ext": ".gif", "mime": "image/gif", "pil": "GIF",
            "quality": False, "alpha": True, "animated": True},
    "tiff": {"label": "TIFF", "ext": ".tiff", "mime": "image/tiff", "pil": "TIFF",
             "quality": False, "alpha": True, "animated": False},
    "ico": {"label": "ICO", "ext": ".ico", "mime": "image/x-icon", "pil": "ICO",
            "quality": False, "alpha": True, "animated": False},
}

# allow_copy：该容器能否安全承载常见源编码，决定是否提供「不重新编码」选项
VIDEO_FORMATS = {
    "mp4": {"label": "MP4", "ext": ".mp4", "mime": "video/mp4",
            "video": ["h264", "h265", "mpeg4"], "audio": ["aac", "mp3"],
            "playable": True, "allow_copy": True},
    "mkv": {"label": "MKV", "ext": ".mkv", "mime": "video/x-matroska",
            "video": ["h264", "h265", "vp9", "mpeg4"], "audio": ["aac", "mp3", "opus"],
            "playable": False, "allow_copy": True},
    "mov": {"label": "MOV", "ext": ".mov", "mime": "video/quicktime",
            "video": ["h264", "h265", "mpeg4"], "audio": ["aac", "mp3"],
            "playable": True, "allow_copy": True},
    "webm": {"label": "WEBM", "ext": ".webm", "mime": "video/webm",
             "video": ["vp9", "vp8"], "audio": ["opus"],
             "playable": True, "allow_copy": False},
    "avi": {"label": "AVI", "ext": ".avi", "mime": "video/x-msvideo",
            "video": ["mpeg4", "h264"], "audio": ["mp3", "aac"],
            "playable": False, "allow_copy": False},
    "wmv": {"label": "WMV", "ext": ".wmv", "mime": "video/x-ms-wmv",
            "video": ["wmv2"], "audio": ["wmav2"],
            "playable": False, "allow_copy": False},
    "flv": {"label": "FLV", "ext": ".flv", "mime": "video/x-flv",
            "video": ["h264", "flv1"], "audio": ["aac", "mp3"],
            "playable": False, "allow_copy": False},
    "gif": {"label": "GIF 动图", "ext": ".gif", "mime": "image/gif",
            "video": ["gif"], "audio": [],
            "playable": True, "allow_copy": False},
}

VIDEO_CODEC_LABEL = {
    "h264": "H.264（通用性最好）",
    "h265": "H.265（同画质体积更小）",
    "mpeg4": "MPEG-4（老设备兼容）",
    "vp9": "VP9（网页友好）",
    "vp8": "VP8（网页友好·旧）",
    "wmv2": "WMV2",
    "flv1": "FLV1",
    "gif": "动图序列",
    "copy": "不重新编码（最快，仅换容器）",
}
AUDIO_CODEC_LABEL = {
    "aac": "AAC", "mp3": "MP3", "opus": "Opus", "wmav2": "WMA",
    "copy": "保持原样",
}

_VIDEO_ENCODER = {
    "h264": "libx264", "h265": "libx265", "mpeg4": "mpeg4",
    "vp9": "libvpx-vp9", "vp8": "libvpx", "wmv2": "wmv2", "flv1": "flv",
}
_AUDIO_ENCODER = {
    "aac": "aac", "mp3": "libmp3lame", "opus": "libopus", "wmav2": "wmav2",
}

IMAGE_INPUT_EXTS = {
    ".jpg", ".jpeg", ".jpe", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff",
    ".ico", ".heic", ".heif", ".jfif", ".avif", ".ppm", ".tga",
}
VIDEO_INPUT_EXTS = {
    ".mp4", ".m4v", ".mkv", ".mov", ".avi", ".wmv", ".flv", ".webm", ".ts",
    ".mpg", ".mpeg", ".3gp", ".rmvb", ".vob", ".m2ts", ".ogv", ".asf",
}

RESOLUTION_PRESETS = [
    ("keep", "保持原始"),
    ("2160", "4K · 2160P"),
    ("1440", "2K · 1440P"),
    ("1080", "全高清 · 1080P"),
    ("720", "高清 · 720P"),
    ("480", "标清 · 480P"),
    ("360", "流畅 · 360P"),
    ("custom", "自定义宽高"),
]

MAX_UPLOAD_BYTES = 4 * 1024 * 1024 * 1024  # 单个文件 4GB 上限

_ILLEGAL_RE = re.compile(r'[\\/:*?"<>|]')
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL", "CLOCK$",
    *{f"COM{i}" for i in range(0, 10)},
    *{f"LPT{i}" for i in range(0, 10)},
}


def safe_stem(raw, fallback="file"):
    """把任意来源的文件名清理成安全主体名（不含扩展名、不含目录成分）。"""
    s = _CTRL_RE.sub("", str(raw or ""))
    s = s.replace("\\", "/").split("/")[-1]
    s = _ILLEGAL_RE.sub("_", s)
    s = Path(s).stem
    s = re.sub(r"_{2,}", "_", s).strip(" ._")
    if s.upper() in WINDOWS_RESERVED:
        s = "_" + s
    if len(s) > 80:
        s = s[:80].rstrip(" ._")
    return s or fallback


def guess_kind(filename):
    """按扩展名判断文件属于图片还是视频；无法判断返回 ''。"""
    ext = Path(str(filename or "")).suffix.lower()
    if ext in IMAGE_INPUT_EXTS:
        return "image"
    if ext in VIDEO_INPUT_EXTS:
        return "video"
    return ""


# ------------------------------------------------------------------
# 运行环境
# ------------------------------------------------------------------
class _Config:
    def __init__(self):
        self.output_dir = Path.cwd() / "downloads" / "Converted"
        self.temp_dir = Path.cwd() / ".convert_tmp"
        self.media_tool = None   # 视频转换组件路径
        self.probe_tool = None   # 媒体信息读取组件路径
        self.logger = None
        self.base_dir = None     # 应用根目录，用于向前端提供相对路径显示


_cfg = _Config()


def configure(output_dir, temp_dir, media_tool=None, probe_tool=None, logger=None, base_dir=None):
    """由宿主应用注入运行环境。重复调用安全。"""
    _cfg.output_dir = Path(output_dir)
    _cfg.temp_dir = Path(temp_dir)
    _cfg.media_tool = media_tool or None
    _cfg.probe_tool = probe_tool or None
    _cfg.logger = logger
    _cfg.base_dir = Path(base_dir) if base_dir else None
    _cfg.output_dir.mkdir(parents=True, exist_ok=True)
    _cfg.temp_dir.mkdir(parents=True, exist_ok=True)


def _log(level, msg):
    if _cfg.logger:
        getattr(_cfg.logger, level, _cfg.logger.info)(f"[convert] {msg}")


def _rel_output_dir():
    """相对应用根目录的输出路径（用 / 分隔，跨平台友好），便于打包分发后展示。

    若未配置 base_dir 或输出目录不在其下，返回空串，由前端回退到绝对路径。
    """
    if not _cfg.base_dir:
        return ""
    try:
        rel = Path(_cfg.output_dir).resolve().relative_to(_cfg.base_dir.resolve())
        return rel.as_posix()
    except Exception:
        return ""


def capabilities():
    """如实报告本机可用的转换能力，供前端展示与禁用控件。"""
    image_engine = "builtin" if _PIL_OK else ("media" if _cfg.media_tool else "")
    return {
        "image": bool(image_engine),
        "image_engine": image_engine,
        "video": bool(_cfg.media_tool),
        "animated_image": _PIL_OK,
        "output_dir": str(_cfg.output_dir),
        "output_dir_rel": _rel_output_dir(),
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "image_formats": [
            {"key": k, "label": v["label"], "quality": v["quality"],
             "alpha": v["alpha"], "animated": v["animated"]}
            for k, v in IMAGE_FORMATS.items()
        ],
        "video_formats": [
            {"key": k, "label": v["label"],
             "video_codecs": [{"key": c, "label": VIDEO_CODEC_LABEL.get(c, c)}
                              for c in (v["video"] + (["copy"] if v.get("allow_copy") else []))],
             "audio_codecs": [{"key": c, "label": AUDIO_CODEC_LABEL.get(c, c)}
                              for c in (v["audio"] + (["copy"] if v.get("allow_copy") else []))],
             "playable": v["playable"]}
            for k, v in VIDEO_FORMATS.items()
        ],
        "resolution_presets": [{"key": k, "label": t} for k, t in RESOLUTION_PRESETS],
    }


# ------------------------------------------------------------------
# 任务模型
# ------------------------------------------------------------------
class ConvertJob:
    __slots__ = (
        "id", "kind", "source_name", "source_path", "source_size", "output_dir", "target",
        "options", "status", "progress", "error", "output_path", "output_name",
        "output_size", "created_at", "started_at", "finished_at", "detail",
        "own_source", "_proc", "_cancel",
    )

    def __init__(self, kind, source_name, source_path, source_size, target, options,
                 own_source=True, output_dir=None):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.source_name = source_name
        self.source_path = str(source_path)
        self.source_size = int(source_size or 0)
        self.output_dir = str(output_dir) if output_dir else ""
        self.target = target
        self.options = dict(options or {})
        # 源文件是否由本任务独占：独占才会在结束后清理，避免同一源被多任务共用时误删
        self.own_source = bool(own_source)
        self.status = "queued"
        self.progress = 0
        self.error = ""
        self.output_path = ""
        self.output_name = ""
        self.output_size = 0
        self.detail = ""
        self.created_at = datetime.now().isoformat(timespec="seconds")
        self.started_at = ""
        self.finished_at = ""
        self._proc = None
        self._cancel = False

    def to_dict(self):
        return {
            "id": self.id,
            "kind": self.kind,
            "source_name": self.source_name,
            "source_size": self.source_size,
            "output_dir": self.output_dir or str(_cfg.output_dir),
            "target": self.target,
            "target_label": (VIDEO_FORMATS if self.kind == "video" else IMAGE_FORMATS)
                .get(self.target, {}).get("label", self.target.upper()),
            "options": self.options,
            "status": self.status,
            "progress": self.progress,
            "error": self.error,
            "detail": self.detail,
            "output_name": self.output_name,
            "output_size": self.output_size,
            "has_output": bool(self.output_path and os.path.exists(self.output_path)),
            "preview_kind": self._preview_kind(),
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }

    def _preview_kind(self):
        if self.status != "done":
            return ""
        if self.kind == "image" or self.target == "gif":
            return "image"
        meta = VIDEO_FORMATS.get(self.target, {})
        return "video" if meta.get("playable") else ""


# ------------------------------------------------------------------
# 转换管理器
# ------------------------------------------------------------------
class ConvertManager:
    def __init__(self, workers=2):
        self._jobs = {}
        self._order = []
        self._lock = threading.RLock()
        self._pending = []
        self._wake = threading.Condition(self._lock)
        self._workers = []
        self._worker_count = max(1, int(workers))
        self._started = False

    # -- 生命周期 --
    def start(self):
        with self._lock:
            if self._started:
                return
            self._started = True
            for i in range(self._worker_count):
                t = threading.Thread(target=self._loop, name=f"convert-worker-{i}", daemon=True)
                t.start()
                self._workers.append(t)

    def _loop(self):
        while True:
            with self._wake:
                while not self._pending:
                    self._wake.wait()
                job_id = self._pending.pop(0)
                job = self._jobs.get(job_id)
            if not job:
                continue
            if job._cancel:
                self._finish(job, "canceled", error="已取消")
                continue
            self._run(job)

    # -- 提交 / 查询 --
    def submit(self, job):
        self.start()
        with self._wake:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._pending.append(job.id)
            self._wake.notify()
        return job

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)

    def list(self):
        with self._lock:
            return [self._jobs[i].to_dict() for i in self._order if i in self._jobs]

    def cancel(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status in {"done", "failed", "canceled"}:
                return False
            job._cancel = True
            proc = job._proc
            if job.status == "queued":
                if job_id in self._pending:
                    self._pending.remove(job_id)
                self._finish(job, "canceled", error="已取消")
                return True
        if proc:
            try:
                proc.terminate()
            except Exception:
                pass
        return True

    def clear_finished(self):
        removed = 0
        with self._lock:
            keep = []
            for jid in self._order:
                job = self._jobs.get(jid)
                if job and job.status in {"done", "failed", "canceled"}:
                    self._jobs.pop(jid, None)
                    removed += 1
                else:
                    keep.append(jid)
            self._order = keep
        return removed

    def remove(self, job_id, delete_output=False):
        with self._lock:
            job = self._jobs.pop(job_id, None)
            if job_id in self._order:
                self._order.remove(job_id)
        if not job:
            return False
        if delete_output and job.output_path:
            try:
                os.remove(job.output_path)
            except Exception:
                pass
        return True

    # -- 执行 --
    def _finish(self, job, status, error="", detail=""):
        job.status = status
        job.error = error
        if detail:
            job.detail = detail
        job.finished_at = datetime.now().isoformat(timespec="seconds")
        if status == "done":
            job.progress = 100
        job._proc = None
        self._cleanup_source(job)

    @staticmethod
    def _cleanup_source(job):
        """转换结束后立即删除上传副本，避免磁盘占用翻倍。

        仅清理本任务独占的临时文件；共用源（同一文件转多种格式）不动。
        """
        if not job.own_source:
            return
        try:
            path = Path(job.source_path)
            if path.exists() and _cfg.temp_dir in path.parents:
                path.unlink()
        except Exception:
            pass

    def _run(self, job):
        job.status = "running"
        job.started_at = datetime.now().isoformat(timespec="seconds")
        job.progress = 1
        try:
            if job.kind == "image":
                out = _convert_image(job)
            else:
                out = _convert_video(job)
            if job._cancel:
                try:
                    if out and os.path.exists(out):
                        os.remove(out)
                except Exception:
                    pass
                self._finish(job, "canceled", error="已取消")
                return
            job.output_path = str(out)
            job.output_name = Path(out).name
            job.output_size = os.path.getsize(out) if os.path.exists(out) else 0
            if job.output_size <= 0:
                raise RuntimeError("输出文件为空")
            self._finish(job, "done")
            _log("info", f"{job.source_name} -> {job.output_name} ({job.output_size/1024:.0f}KB)")
        except _Canceled:
            self._finish(job, "canceled", error="已取消")
        except Exception as exc:  # noqa: BLE001 - 任何失败都要如实回报
            _log("warning", f"{job.source_name} 转换失败: {exc}")
            self._finish(job, "failed", error=_friendly_error(exc))


class _Canceled(Exception):
    pass


manager = ConvertManager()


def _friendly_error(exc):
    text = str(exc) or exc.__class__.__name__
    lowered = text.lower()
    if "no such file" in lowered or "cannot find" in lowered:
        return "源文件已丢失，请重新添加"
    if "permission" in lowered:
        return "没有写入权限，请更换输出位置后重试"
    if "space" in lowered and "no" in lowered:
        return "磁盘空间不足"
    if "unsupported" in lowered or "cannot identify" in lowered:
        return "该文件格式暂不支持，或文件已损坏"
    if "encoder" in lowered and "not" in lowered:
        return "本机组件不支持所选编码，请换一种编码后重试"
    if len(text) > 160:
        text = text[:160] + "…"
    return text


# ------------------------------------------------------------------
# 输出路径
# ------------------------------------------------------------------
def _unique_output(stem, ext, output_dir=None):
    out_dir = Path(output_dir) if output_dir else _cfg.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir / f"{stem}{ext}"
    if not base.exists():
        return base
    for i in range(1, 1000):
        candidate = out_dir / f"{stem}_{i}{ext}"
        if not candidate.exists():
            return candidate
    return out_dir / f"{stem}_{int(time.time())}{ext}"


# ------------------------------------------------------------------
# 图片转换
# ------------------------------------------------------------------
def _target_size(width, height, opts):
    """根据分辨率选项算出目标宽高；返回 None 表示保持原尺寸。"""
    mode = str(opts.get("resize_mode") or "keep")
    if mode == "keep" or width <= 0 or height <= 0:
        return None
    if mode == "percent":
        pct = max(1, min(400, int(opts.get("percent") or 100)))
        if pct == 100:
            return None
        return max(1, round(width * pct / 100)), max(1, round(height * pct / 100))
    if mode == "long_edge":
        edge = max(1, int(opts.get("long_edge") or 0))
        if not edge or max(width, height) == edge:
            return None
        ratio = edge / max(width, height)
        return max(1, round(width * ratio)), max(1, round(height * ratio))
    if mode == "exact":
        w = int(opts.get("width") or 0)
        h = int(opts.get("height") or 0)
        if w <= 0 and h <= 0:
            return None
        if opts.get("keep_ratio", True):
            if w > 0 and h > 0:
                ratio = min(w / width, h / height)
            elif w > 0:
                ratio = w / width
            else:
                ratio = h / height
            return max(1, round(width * ratio)), max(1, round(height * ratio))
        return max(1, w or width), max(1, h or height)
    return None


def _save_kwargs(target, opts):
    meta = IMAGE_FORMATS[target]
    quality = max(1, min(100, int(opts.get("quality") or 88)))
    kwargs = {}
    if target == "jpg":
        kwargs.update(quality=quality, optimize=True, progressive=bool(opts.get("progressive", True)))
    elif target == "webp":
        if quality >= 100:
            kwargs.update(lossless=True, quality=100)
        else:
            kwargs.update(quality=quality, method=4)
    elif target == "png":
        # 质量滑块映射为压缩等级：质量越高压得越轻、速度越快
        kwargs.update(optimize=True, compress_level=max(0, min(9, round((100 - quality) / 11))))
    elif target == "tiff":
        kwargs.update(compression="tiff_lzw")
    if not meta["quality"]:
        kwargs.pop("quality", None)
    return kwargs


def _flatten(image, target):
    """目标格式不支持透明时，用白底合成，避免出现黑块。"""
    meta = IMAGE_FORMATS[target]
    if meta["alpha"]:
        return image if image.mode in ("RGB", "RGBA", "P", "L") else image.convert("RGBA")
    if image.mode in ("RGBA", "LA", "P"):
        rgba = image.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.split()[-1])
        return canvas
    return image.convert("RGB") if image.mode != "RGB" else image


def _convert_image(job):
    if not _PIL_OK:
        return _convert_image_via_media(job)
    meta = IMAGE_FORMATS[job.target]
    opts = job.options
    src = Path(job.source_path)
    out = _unique_output(safe_stem(job.source_name, "image"), meta["ext"], job.output_dir)

    with Image.open(src) as im:
        frames = getattr(im, "n_frames", 1)
        animated = frames > 1 and meta["animated"] and bool(opts.get("keep_animation", True))
        if opts.get("auto_orient", True) and not animated:
            try:
                im = ImageOps.exif_transpose(im)
            except Exception:
                pass
        size = _target_size(im.width, im.height, opts)
        job.detail = f"{im.width}×{im.height}"
        job.progress = 30

        if animated:
            out_frames = []
            durations = []
            for frame in ImageSequence.Iterator(im):
                f = frame.convert("RGBA")
                if size:
                    f = f.resize(size, Image.LANCZOS)
                out_frames.append(_flatten(f, job.target) if job.target != "gif" else f.convert("P", palette=Image.ADAPTIVE))
                durations.append(frame.info.get("duration", 80))
            job.progress = 70
            head, *rest = out_frames
            head.save(out, format=meta["pil"], save_all=True, append_images=rest,
                      duration=durations, loop=im.info.get("loop", 0),
                      **({} if job.target == "gif" else _save_kwargs(job.target, opts)))
            final = size or (im.width, im.height)
            job.detail = f"{final[0]}×{final[1]} · {len(out_frames)} 帧"
        else:
            picture = im.convert("RGBA") if im.mode == "P" else im
            if size:
                picture = picture.resize(size, Image.LANCZOS)
                job.detail = f"{size[0]}×{size[1]}"
            picture = _flatten(picture, job.target)
            if job.target == "ico":
                edge = min(256, max(picture.size))
                picture = picture.resize((edge, edge), Image.LANCZOS)
            job.progress = 70
            picture.save(out, format=meta["pil"], **_save_kwargs(job.target, opts))
    job.progress = 96
    return out


def _convert_image_via_media(job):
    """无图像库时的兜底：用本机媒体组件完成静态图片转换。"""
    if not _cfg.media_tool:
        raise RuntimeError("本机缺少图片转换组件")
    meta = IMAGE_FORMATS[job.target]
    out = _unique_output(safe_stem(job.source_name, "image"), meta["ext"], job.output_dir)
    cmd = [_cfg.media_tool, "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
           "-i", job.source_path]
    opts = job.options
    mode = str(opts.get("resize_mode") or "keep")
    if mode == "percent":
        pct = max(1, min(400, int(opts.get("percent") or 100)))
        if pct != 100:
            cmd += ["-vf", f"scale=iw*{pct/100:.4f}:-1"]
    elif mode == "long_edge":
        edge = max(1, int(opts.get("long_edge") or 0))
        cmd += ["-vf", f"scale='if(gt(iw,ih),{edge},-1)':'if(gt(iw,ih),-1,{edge})'"]
    elif mode == "exact":
        w = int(opts.get("width") or 0) or -1
        h = int(opts.get("height") or 0) or -1
        cmd += ["-vf", f"scale={w}:{h}"]
    if meta["quality"] and job.target == "jpg":
        quality = max(1, min(100, int(opts.get("quality") or 88)))
        cmd += ["-q:v", str(max(2, round(31 - quality * 0.29)))]
    cmd += ["-frames:v", "1", str(out)]
    _run_tool(job, cmd, total_seconds=0)
    return out


# ------------------------------------------------------------------
# 视频转换
# ------------------------------------------------------------------
def probe_duration(path):
    """读取媒体时长（秒）；失败返回 0。"""
    tool = _cfg.probe_tool
    if not tool:
        return 0.0
    try:
        res = subprocess.run(
            [tool, "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30, creationflags=_NO_WINDOW,
        )
        info = json.loads(res.stdout or "{}")
        return float((info.get("format") or {}).get("duration") or 0)
    except Exception:
        return 0.0


def _scale_filter(opts):
    mode = str(opts.get("resolution") or "keep")
    if mode == "keep":
        return ""
    if mode == "custom":
        w = int(opts.get("width") or 0)
        h = int(opts.get("height") or 0)
        if w > 0 and h > 0:
            return f"scale={w // 2 * 2}:{h // 2 * 2}"
        if w > 0:
            return f"scale={w // 2 * 2}:-2"
        if h > 0:
            return f"scale=-2:{h // 2 * 2}"
        return ""
    try:
        height = int(mode)
    except (TypeError, ValueError):
        return ""
    # 只缩不放，避免把低清素材硬拉大
    return f"scale=-2:'min({height},ih)'"


def _build_video_cmd(job, out_path):
    meta = VIDEO_FORMATS[job.target]
    opts = job.options
    cmd = [_cfg.media_tool, "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
           "-progress", "pipe:1", "-i", job.source_path]

    filters = []
    scale = _scale_filter(opts)
    if scale:
        filters.append(scale)

    if job.target == "gif":
        fps = int(opts.get("fps") or 12)
        filters.insert(0, f"fps={max(1, min(30, fps))}")
        filters.append("split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer")
        cmd += ["-filter_complex", ",".join(filters), "-loop", "0", "-an", str(out_path)]
        return cmd

    if filters:
        cmd += ["-vf", ",".join(filters)]

    fps = opts.get("fps")
    if fps and str(fps) != "keep":
        cmd += ["-r", str(max(1, min(120, int(fps))))]

    vcodec = str(opts.get("video_codec") or (meta["video"][0] if meta["video"] else "h264"))
    if vcodec == "copy" and not scale and not (fps and str(fps) != "keep"):
        cmd += ["-c:v", "copy"]
    else:
        encoder = _VIDEO_ENCODER.get(vcodec, "libx264")
        cmd += ["-c:v", encoder]
        if encoder in ("libx264", "libx265"):
            cmd += ["-preset", str(opts.get("preset") or "medium")]
        bitrate = str(opts.get("bitrate") or "auto")
        if bitrate != "auto":
            kbps = max(100, min(100000, int(bitrate)))
            cmd += ["-b:v", f"{kbps}k", "-maxrate", f"{int(kbps * 1.5)}k", "-bufsize", f"{kbps * 2}k"]
        elif encoder in ("libx264", "libx265"):
            cmd += ["-crf", str(max(0, min(51, int(opts.get("crf") or 23))))]
        elif encoder == "libvpx-vp9":
            cmd += ["-crf", "32", "-b:v", "0"]
        if encoder in ("libx264", "libx265"):
            cmd += ["-pix_fmt", "yuv420p"]

    if not meta["audio"] or opts.get("mute"):
        cmd += ["-an"]
    else:
        acodec = str(opts.get("audio_codec") or meta["audio"][0])
        if acodec == "copy":
            cmd += ["-c:a", "copy"]
        else:
            cmd += ["-c:a", _AUDIO_ENCODER.get(acodec, "aac"),
                    "-b:a", f"{max(32, min(512, int(opts.get('audio_bitrate') or 128)))}k"]

    if job.target in ("mp4", "mov"):
        cmd += ["-movflags", "+faststart"]
    cmd += [str(out_path)]
    return cmd


def _convert_video(job):
    if not _cfg.media_tool:
        raise RuntimeError("本机缺少视频转换组件")
    meta = VIDEO_FORMATS[job.target]
    out = _unique_output(safe_stem(job.source_name, "video"), meta["ext"], job.output_dir)
    total = probe_duration(job.source_path)
    if total:
        job.detail = f"时长 {int(total // 60)}:{int(total % 60):02d}"
    cmd = _build_video_cmd(job, out)
    try:
        _run_tool(job, cmd, total_seconds=total)
    except Exception:
        try:
            if out.exists():
                out.unlink()
        except Exception:
            pass
        raise
    return out


def _run_tool(job, cmd, total_seconds=0.0):
    """执行外部媒体组件并解析进度；被取消时抛 _Canceled。"""
    _log("debug", " ".join(str(c) for c in cmd))
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        creationflags=_NO_WINDOW,
    )
    job._proc = proc
    tail = []

    def _drain_err():
        try:
            for line in proc.stderr:
                line = line.strip()
                if line:
                    tail.append(line)
                    if len(tail) > 12:
                        tail.pop(0)
        except Exception:
            pass

    err_thread = threading.Thread(target=_drain_err, daemon=True)
    err_thread.start()

    try:
        for raw in proc.stdout:
            if job._cancel:
                break
            line = raw.strip()
            if line.startswith("out_time_us=") and total_seconds > 0:
                try:
                    done = int(line.split("=", 1)[1]) / 1_000_000
                    job.progress = max(1, min(99, int(done / total_seconds * 100)))
                except (ValueError, ZeroDivisionError):
                    pass
            elif line.startswith("frame=") and total_seconds <= 0:
                job.progress = min(95, job.progress + 1)
            elif line == "progress=end":
                job.progress = max(job.progress, 97)
    finally:
        if job._cancel:
            try:
                proc.terminate()
            except Exception:
                pass
        code = proc.wait()
        err_thread.join(timeout=1)
        job._proc = None

    if job._cancel:
        raise _Canceled()
    if code != 0:
        detail = "；".join(tail[-3:]) if tail else f"退出码 {code}"
        raise RuntimeError(detail)


# ------------------------------------------------------------------
# 临时文件
# ------------------------------------------------------------------
def stage_upload(file_storage, filename):
    """把上传文件落到独立临时目录，返回 (路径, 字节数)。"""
    _cfg.temp_dir.mkdir(parents=True, exist_ok=True)
    stem = safe_stem(filename, "upload")
    ext = Path(str(filename or "")).suffix.lower()
    ext = _ILLEGAL_RE.sub("", ext)[:12]
    target = _cfg.temp_dir / f"{uuid.uuid4().hex[:10]}_{stem}{ext}"
    file_storage.save(str(target))
    return target, target.stat().st_size


def purge_temp(max_age_hours=6):
    """清掉超龄的上传残留（进程重启后的孤儿文件）。"""
    if not _cfg.temp_dir.exists():
        return 0
    deadline = time.time() - max_age_hours * 3600
    removed = 0
    for item in _cfg.temp_dir.iterdir():
        try:
            if item.is_file() and item.stat().st_mtime < deadline:
                item.unlink()
                removed += 1
            elif item.is_dir() and item.stat().st_mtime < deadline:
                shutil.rmtree(item, ignore_errors=True)
                removed += 1
        except Exception:
            continue
    return removed
