"""文件名与路径安全处理（P0-1）。

所有下载入口（单视频 / 批量 / Excel 导入 / 播放列表）必须统一使用本模块，
后端强制验证，不依赖前端。
"""
import re
from datetime import datetime
from pathlib import Path

# Windows 保留设备名（不含扩展名部分，大小写不敏感）
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL", "CLOCK$",
    *{f"COM{i}" for i in range(1, 10)},
    *{f"LPT{i}" for i in range(1, 10)},
    "COM0", "LPT0",
}

# 路径分隔符 + Windows 非法字符
_ILLEGAL_RE = re.compile(r'[\\/:*?"<>|]')
# 控制字符（含 DEL）
_CTRL_RE = re.compile(r'[\x00-\x1f\x7f]')

# 主体（不含扩展名）最大长度；Windows MAX_PATH=260，为目录留余量
MAX_STEM_LEN = 120

ALLOWED_EXTS = (".mp4", ".mkv", ".webm", ".flv", ".mp3", ".m4a", ".ts")


def sanitize_filename(raw, default_ext=".mp4"):
    """把任意用户输入清理成安全的**纯文件名**（不含任何目录成分）。

    - 移除路径分隔符（/ \\）、盘符冒号、控制字符和 Windows 非法字符；
    - 处理 CON / NUL / COM1 等 Windows 保留名；
    - 去掉首尾点和空格（Windows 不允许结尾点/空格）；
    - 主体长度截断到 MAX_STEM_LEN；
    - 空输入 → 时间戳默认名。
    始终返回带扩展名的安全文件名。
    """
    s = str(raw or "").strip()
    # 控制字符直接删除
    s = _CTRL_RE.sub("", s)
    # 路径分隔符 / 非法字符替换为下划线（含 \\ / : * ? " < > |）
    s = _ILLEGAL_RE.sub("_", s)
    # 折叠连续下划线，去首尾 点/空格/下划线
    s = re.sub(r"_{2,}", "_", s).strip(" ._")

    # 拆扩展名
    p = Path(s)
    stem, ext = p.stem, p.suffix.lower()
    if ext not in ALLOWED_EXTS:
        # 未知/无扩展名：整个 s 作为主体，补默认扩展名
        stem, ext = s, default_ext

    stem = stem.strip(" ._")
    # 保留名处理（Windows 对 CON.mp4 同样保留）
    if stem.upper() in WINDOWS_RESERVED:
        stem = "_" + stem
    # 长度限制
    if len(stem) > MAX_STEM_LEN:
        stem = stem[:MAX_STEM_LEN].rstrip(" ._")
    if not stem:
        stem = "video_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    return stem + ext


def is_within(base_dir, path):
    """path 解析后是否位于 base_dir 内（含 base_dir 自身的子孙）。"""
    try:
        base = Path(base_dir).resolve()
        p = Path(path).resolve()
        return p == base or base in p.parents
    except Exception:
        return False


def resolve_within(base_dir, filename):
    """把 filename 拼到 base_dir 下并解析，确认仍位于 base_dir 内。

    返回解析后的绝对 Path；若逃逸（..\\、绝对路径等）抛 ValueError。
    """
    base = Path(base_dir).resolve()
    candidate = (base / filename).resolve()
    if candidate.parent != base and base not in candidate.parents:
        raise ValueError(f"路径逃逸输出目录: {filename!r}")
    return candidate
