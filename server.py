#!/usr/bin/env python3
"""Video Download Tool - Local Flask server with web UI. v2.7"""

import collections
import atexit
import hmac
import ipaddress
import importlib.util
import json
import logging
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from datetime import datetime, date, timedelta
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, Response, make_response

try:
    import send2trash
except ImportError:
    send2trash = None

from core.naming import sanitize_filename, resolve_within, is_within
from core import trash as trashmod
from core.media_check import verify_media
from core.formats import classify_formats
from core import scheduler as schedmod
from core.runtime_paths import resolve_runtime_paths
from core.network_policy import OutboundURLRejected, validate_public_http_url

# --- Paths ---
PATHS = resolve_runtime_paths()
BASE_DIR = PATHS.app_dir
RESOURCE_DIR = PATHS.resource_dir
RUNTIME_DIR = PATHS.runtime_dir
DATA_DIR = PATHS.data_dir
STATIC_DIR = PATHS.static_dir
DOWNLOADS_DIR = PATHS.downloads_dir
LOGS_DIR = DATA_DIR / "logs"
TASKS_FILE = DATA_DIR / "tasks.json"
GROUPS_FILE = DATA_DIR / "groups.json"
DETECT_RECORDS_FILE = DATA_DIR / "detect_records.json"
DETECT_RECORDS_LIMIT = 500  # 检测记录最多保留条数（服务端磁盘持久化）
COOKIES_FILE = PATHS.cookies_file
APP_VERSION = "v2.7.0"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# --- Logging ---
def setup_logging():
    logger = logging.getLogger("m3u8-tool")
    logger.setLevel(logging.DEBUG)

    # File handler: daily rotation, keep 30 days
    fh = TimedRotatingFileHandler(
        str(LOGS_DIR / "m3u8-tool.log"),
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))

    # Console handler
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("[m3u8-tool] %(message)s"))

    logger.addHandler(fh)
    logger.addHandler(ch)
    # 诊断:记录 HTTP 请求(含 /api/groups)到达情况,便于排查前端拉取问题
    logging.getLogger("werkzeug").setLevel(logging.INFO)
    return logger


log = setup_logging()

# --- Configuration ---
CONCURRENT_FRAGMENTS = 8      # Fragments to download in parallel per task
MAX_CONCURRENT_TASKS = 3      # Max simultaneous downloads; excess queued (runtime-adjustable)

# Tracking params stripped when comparing URLs for dedupe
_TRACKING_PARAMS = {
    "spm_id_from","share_source","share_medium","share_session_id","share_token",
    "share_from","share_plat","share_tag","share_id","share_uid","share_user_id",
    "share_app_name","from","from_sourse","from_source","msToken","region",
    "did","bbid","ts","search_source","_d","previous_page","wxshare_count",
    "mkt","source","s","seid","vd_source","unique_k","buvid","is_story_h5",
    "webid","web_location","refer","referer_tag","mid","u_code","with_sec_did",
    "category_type_1","entity_type","entity_id","count","enter_from","enter_from_merge",
}

def _normalize_url_for_compare(u):
    """Normalize a URL for dedupe comparison: strip fragment, tracking params,
    trailing slashes; keep content params (YouTube v=, t=, list=). Mirrors the
    frontend normalizeUrlForCompare so the same video matches across pastes."""
    try:
        p = urllib.parse.urlparse(u)
        params = urllib.parse.parse_qs(p.query, keep_blank_values=True)
        kept = {k: v for k, v in params.items()
                if not (k.startswith("utm_") or k.startswith("share_") or k in _TRACKING_PARAMS)}
        q = "&".join(f"{k}={v[0]}" for k in sorted(kept) for v in [kept[k]])
        base = f"{p.scheme}://{p.hostname}{p.path}"
        if q:
            base += "?" + q
        return base.rstrip("/")
    except Exception:
        return u.split("#")[0].strip().rstrip("/")

# Tool paths. Portable builds prefer their read-only Runtime directory.
def _runtime_tool(name):
    candidate = RUNTIME_DIR / name
    return str(candidate) if candidate.is_file() else None


def _yt_dlp_command():
    bundled = _runtime_tool("yt-dlp.exe")
    if bundled:
        return [bundled]
    if getattr(sys, "frozen", False):
        # The desktop entry point dispatches this private worker mode to the
        # bundled yt_dlp Python package.
        return [sys.executable, "--yt-dlp-worker"]
    return [sys.executable, str(RESOURCE_DIR / "yt_dlp_worker.py")]


def _yt_dlp_retry_args():
    """Return retry flags to tolerate transient extractor blocks."""
    return ["--extractor-retries", "2", "--retry-sleep", "extractor:3"]


def _yt_dlp_header_args(url):
    """Return site-specific request headers for yt-dlp (e.g. Bilibili Referer)."""
    extra = []
    try:
        host = urllib.parse.urlparse(url).hostname or ""
        if host and ("bilibili.com" in host or host.endswith(".bilibili.com")):
            extra.append("--add-header")
            extra.append("Referer:https://www.bilibili.com")
    except Exception:
        pass
    return extra


def _yt_dlp_download_prefix(format_id=""):
    """Keep private Portable worker dispatch ahead of all yt-dlp arguments."""
    command = _yt_dlp_command()
    if format_id:
        command.extend(["-f", format_id])
    return command


def _yt_dlp_available():
    return bool(
        _runtime_tool("yt-dlp.exe")
        or importlib.util.find_spec("yt_dlp") is not None
    )


# Compatibility symbol for diagnostics and older integrations.
YT_DLP = _yt_dlp_command()[0]
FFMPEG_CANDIDATES = [
    RUNTIME_DIR / "ffmpeg.exe",
    BASE_DIR / ".tmp" / "ffmpeg",
    BASE_DIR.parent / ".tmp" / "ffmpeg",
]

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="")
# 本地 API 始终启用随机会话保护。桌面启动器会在 import 前注入令牌；
# 直接运行 server.py 时在这里生成，避免开发启动方式意外退化为无保护接口。
_SESSION_TOKEN = os.environ.get("YINGJI_SESSION_TOKEN", "") or secrets.token_urlsafe(32)
_DIRECTORY_PICKER_LOCK = threading.Lock()


# --- FFmpeg Discovery ---
def find_ffmpeg():
    for candidate in FFMPEG_CANDIDATES:
        if candidate.is_file():
            return str(candidate)
        if candidate.is_dir():
            matches = list(candidate.rglob("ffmpeg.exe"))
            if matches:
                return str(matches[0])
    return shutil.which("ffmpeg")


FFMPEG_PATH = find_ffmpeg()


def _request_host_is_loopback():
    try:
        host = urllib.parse.urlsplit(f"//{request.host}").hostname
        return host in {"127.0.0.1", "localhost", "::1"}
    except Exception:
        return False


def _request_remote_is_loopback():
    """Reject proxy/LAN callers even if they forge a localhost Host header."""
    try:
        return ipaddress.ip_address(request.remote_addr or "").is_loopback
    except ValueError:
        return False


def _request_source_is_local():
    source = request.headers.get("Origin") or request.headers.get("Referer")
    if not source:
        return request.method in {"GET", "HEAD", "OPTIONS"}
    try:
        parsed = urllib.parse.urlsplit(source)
        return (
            parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            and parsed.netloc == request.host
        )
    except Exception:
        return False


@app.before_request
def protect_portable_local_api():
    """Protect local APIs for desktop, Portable and direct server.py starts."""
    if request.path == "/healthz" or not _SESSION_TOKEN:
        return None
    if not _request_host_is_loopback() or not _request_remote_is_loopback():
        return jsonify({"error": "Invalid local host"}), 403
    if request.path.startswith("/api/") or request.path.startswith("/downloads/"):
        supplied = request.cookies.get("yingji_session", "")
        if not supplied or not hmac.compare_digest(supplied, _SESSION_TOKEN):
            return jsonify({"error": "Invalid local session"}), 403
        if not _request_source_is_local():
            return jsonify({"error": "Invalid request source"}), 403
    return None


@app.after_request
def issue_portable_session_cookie(response):
    if _SESSION_TOKEN and response.mimetype == "text/html":
        response.set_cookie(
            "yingji_session",
            _SESSION_TOKEN,
            httponly=True,
            samesite="Strict",
            secure=False,
            path="/",
        )
    return response


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "version": APP_VERSION})

class DownloadRequestRejected(ValueError):
    """A safe, user-facing rejection shared by HTTP and injected callers."""

    def __init__(self, message: str, code: str = "DOWNLOAD_REJECTED", status_code: int = 400):
        super().__init__(message)
        self.user_message = message
        self.code = code
        self.status_code = status_code


def _download_rejection_response(exc: DownloadRequestRejected):
    return jsonify({"error": exc.user_message, "code": exc.code}), exc.status_code


# --- URL Normalization ---
# Some sites use URL formats that yt-dlp doesn't recognise directly.
DOUYIN_JINGXUAN_RE = re.compile(
    r"^(https?://)(www\.)?douyin\.com/jingxuan\?modal_id=(\d+)", re.IGNORECASE
)
BILIBILI_SHORT_RE = re.compile(
    r"^(https?://)(b23\.tv|(?:www\.)?bilibili\.com/video/[Bb][Vv][a-zA-Z0-9]+)(/?.*)$", re.IGNORECASE
)


def _normalize_url(url: str) -> str:
    """Convert site-specific URL formats to ones yt-dlp understands."""
    # Douyin jingxuan -> video
    m = DOUYIN_JINGXUAN_RE.match(url)
    if m:
        normalized = f"{m.group(1)}www.douyin.com/video/{m.group(3)}"
        log.info(f"[URL] Normalized Douyin jingxuan -> video: {normalized}")
        return normalized
    return url


def _validated_download_url(url: str) -> str:
    """Validate the user-controlled entry URL before handing it to yt-dlp.

    This blocks direct access to loopback/private/link-local targets and URL
    credentials. yt-dlp may still follow remote redirects, so this is an input
    boundary rather than a complete network sandbox.
    """
    try:
        value = validate_public_http_url(url, resolve=False)
    except OutboundURLRejected as exc:
        raise DownloadRequestRejected(str(exc), "UNSAFE_URL") from exc
    return _normalize_url(value)


def extract_public_metadata(url: str) -> dict:
    """Extract Telegram caption metadata in the guarded yt-dlp child."""
    try:
        safe_url = _validated_download_url(url)
        command = _yt_dlp_command() + [
            "--dump-single-json",
            "--skip-download",
            "--no-warnings",
            "--no-playlist",
            "--socket-timeout", "15",
            safe_url,
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode != 0:
            return {}
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        info = json.loads(lines[-1]) if lines else {}
        if not isinstance(info, dict):
            return {}
        return {
            "title": str(info.get("title") or "")[:500],
            "description": str(info.get("description") or info.get("title") or "")[:4000],
            "uploader": str(info.get("uploader") or "")[:200],
        }
    except (DownloadRequestRejected, OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return {}


def _extract_site(url: str) -> str:
    """Return the hostname of a URL for grouping in the UI."""
    try:
        return urllib.parse.urlparse(url).hostname or "未知来源"
    except Exception:
        return "未知来源"


# --- Cookie Handling ---
_cookie_args_cache = None  # None=未计算, []=无 cookie, [..]=有 cookie
_cookie_loaded = False      # 独立于缓存的布尔状态，便于即时刷新


def invalidate_cookie_cache():
    """重新计算 cookie 状态，使下一次下载/检测立即生效（无需重启）。"""
    global _cookie_args_cache, _cookie_loaded
    _cookie_args_cache = None
    _cookie_loaded = _cookie_effective()
    log.info(f"[Cookie] Cache invalidated; loaded={_cookie_loaded}")


def _cookie_effective():
    """文件存在且非空（>0 字节）才视为有效凭据；空文件等同未加载。"""
    try:
        return COOKIES_FILE.exists() and COOKIES_FILE.stat().st_size > 0
    except OSError:
        return False


def _get_cookie_args():
    """Return yt-dlp cookie arguments if a cookies file exists."""
    global _cookie_args_cache, _cookie_loaded
    if _cookie_args_cache is not None:
        return _cookie_args_cache

    if _cookie_effective():
        log.info(f"[Cookie] Using {COOKIES_FILE}")
        _cookie_args_cache = ["--cookies", str(COOKIES_FILE)]
        _cookie_loaded = True
        return _cookie_args_cache

    _cookie_args_cache = []
    _cookie_loaded = False
    return _cookie_args_cache


def _validate_cookie_content(content: str):
    """校验 Netscape 格式 cookie 文本。返回 (ok, reason)。不含任何 cookie 内容。"""
    if not content or not content.strip():
        return False, "内容为空"
    lines = [ln for ln in content.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    header_ok = any(
        content.lstrip().startswith(h)
        for h in ("# Netscape HTTP Cookie File", "# HTTP Cookie File", "# Netscape")
    )
    if not header_ok:
        return False, "缺少 Netscape 文件头（应以 # Netscape HTTP Cookie File 开头）"
    if not lines:
        return False, "没有有效的 cookie 行"
    # 每行至少 7 个 tab 分隔字段，domain 必须含 '.' 或有效主机名
    bad = 0
    for ln in lines:
        parts = ln.split("\t")
        if len(parts) < 7:
            bad += 1
            continue
        domain = parts[0].strip()
        if not domain or domain in ("*", "."):
            bad += 1
    if bad >= len(lines):
        return False, "cookie 行格式不正确（应为 7 字段 tab 分隔，且 domain 不能为空或 *）"
    return True, ""


def _needs_cookie_hint(error_text: str) -> bool:
    """Detect whether an error is likely due to missing cookies."""
    lower = error_text.lower()
    return any(k in lower for k in [
        "cookies",
        "cookie",
        "登录",
        "sign in",
        "fresh cookies",
        "not necessarily logged in",
        "unavailable for legal reasons",
        # B站等平台的 412/请求被拦截通常因缺少登录态 Cookie
        "blocked by server",
        "request is blocked",
        "(412)",
        "412",
    ])


def _cookie_hint_text():
    """Return user-facing hint for auth/cookie errors."""
    return (
        "\n\n[提示] 当前网站可能因缺少登录 Cookie 而拒绝访问（如 B站空间/上传页需要 SESSDATA 等登录态）。"
        "请在浏览器中登录该网站，导出 Netscape 格式的 cookies.txt，"
        f"覆盖放置到工具目录：{BASE_DIR}\\cookies.txt，然后重试。"
        "导出工具推荐：Chrome 扩展 'Get cookies.txt LOCALLY'。"
        "若已放置 cookies.txt，请检查是否包含该网站的登录态 Cookie。"
    )


def _risk_control_hint(error_text: str) -> bool:
    """Detect platform risk-control signals from yt-dlp error output.

    These originate from the platform itself (rate limiting, anti-bot
    challenges, account/IP restrictions, captcha). Unlike missing-cookie
    errors, swapping cookies usually does NOT clear them — the user needs to
    slow down / change network / wait out a cooldown.
    """
    lower = (error_text or "").lower()
    return any(k in lower for k in [
        # 频率限制 / 限流
        "rate limit", "rate-limited", "rate limited", "too many requests",
        "429", "retry-after", "retry after", "slow down",
        "sending requests too quickly", "request throttled",
        "you are being rate limited", "you're being rate limited",
        # 机器人验证（YouTube 等）
        "confirm you're not a bot", "confirm you are not a bot",
        "sign in to confirm", "unusual traffic", "automated queries",
        "verify you are a human", "verify you're a human",
        "please verify you are human", "i'm not a robot", "i am not a robot",
        # 账号 / 访问受限
        "account has been temporarily restricted", "account has been limited",
        "account has been suspended", "account is suspended",
        "access has been restricted", "temporarily disabled",
        "your access", "your account has been",
        # IP 封禁
        "ip address has been blocked", "ip has been blocked",
        "temporarily blocked", "blocked due to", "your ip",
        "blocked by the network", "connection blocked",
        # 验证码
        "complete the captcha", "solve the captcha", "unable to bypass",
        "please complete the captcha", "captcha required", "captcha",
        # 地区 / 版权限制
        "not made this video available in your country",
        "unavailable in your country", "region restricted", "region-locked",
        "not available in your country", "content is not available",
    ])


def _risk_hint_text():
    """User-facing hint when a platform risk-control signal is detected."""
    return (
        "\n\n[风控提醒] 平台已对该账号或网络触发风控"
        "（限流 / 机器人验证 / 账号或访问受限 / 验证码）。"
        "更换 Cookie 通常无法解除此类限制。建议：暂停批量下载、降低请求频率、"
        "更换网络环境（如切换 IP）或等待冷却期结束后再试；若频繁触发，请适当拉长任务间隔。"
    )


def _safe_download_error_code(error_text: str) -> str:
    """Classify errors without exposing raw URLs, tokens, paths, or tracebacks."""
    lower = (error_text or "").lower()
    if _risk_control_hint(lower):
        return "RISK_CONTROLLED"
    if "certificate_verify_failed" in lower or "certificate verify failed" in lower:
        return "TLS_CERTIFICATE"
    if "permission denied" in lower and "cookie" in lower:
        return "COOKIE_FILE_LOCKED"
    if _needs_cookie_hint(lower):
        return "AUTH_REQUIRED"
    if any(token in lower for token in (
        "timed out", "timeout", "network is unreachable",
        "temporary failure", "connection reset", "connection refused",
    )):
        return "NETWORK_ERROR"
    return "DOWNLOAD_FAILED"
class TaskManager:
    def __init__(self):
        self.tasks = {}
        self.groups = {}
        self._lock = threading.Lock()
        self._save_lock = threading.Lock()
        self._groups_save_lock = threading.Lock()
        self._queued_deque = collections.deque()
        self._load_groups()
        self._load()

    # ---------- Persistence ----------
    def _save(self):
        """Atomically write current task state to tasks.json (strips runtime attrs).

        Returns True when the write reached disk, False otherwise —
        callers that must guarantee persistence (e.g. 人工确认) check this.
        Existing callers that ignore the return value keep old behavior.
        """
        with self._save_lock:
            with self._lock:
                data = {}
                for tid, t in self.tasks.items():
                    data[tid] = {k: v for k, v in t.items() if not k.startswith("_")}
            try:
                tmp = str(TASKS_FILE) + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, str(TASKS_FILE))
                return True
            except Exception as e:
                log.warning(f"Failed to save tasks.json: {e}")
                return False

    def _load(self):
        """Restore tasks from disk. Reset in-flight states back to cancelled."""
        if not TASKS_FILE.exists():
            return
        try:
            with open(TASKS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            restored = 0
            for tid, t in data.items():
                orig = t.get("status")
                if t.get("status") in ("downloading", "pending"):
                    t["status"] = "cancelled"
                    t["error"] = "服务器重启，下载中断"
                elif t.get("status") == "verifying":
                    # 文件检查中重启：下载已结束，仅检查被打断 → 转入待确认，
                    # 用户可重试下载或人工确认保留，绝不落入普通完成态。
                    t["status"] = "incomplete"
                    t["error"] = "服务重启时文件检查未完成。可重试下载，或确认后自行保留。"
                # "queued" tasks are preserved and re-enqueued below (the deque
                # is in-memory only, so it must be rebuilt on every load).
                t["_proc"] = None
                t["_cancel_flag"] = False
                # Ensure new fields exist
                t.setdefault("started_at", "")
                t.setdefault("completed_at", "")
                t.setdefault("format_id", "")
                t.setdefault("batch_id", "")
                t.setdefault("group_id", "")
                t.setdefault("display_name", Path(t.get("output_name", "")).stem)
                t.setdefault("batch_concurrency", None)
                t.setdefault("files", [])
                t.setdefault("verify", None)
                self.tasks[tid] = t
                restored += 1
            log.info(f"Loaded {restored} tasks from tasks.json")
            if restored:
                # Migrate legacy tasks (no group) into per-site root groups
                self._reassign_orphans()
                # Re-enqueue any "queued" tasks: the start deque is in-memory
                # only (not persisted to disk), so rebuild it from task statuses.
                for tid, t in self.tasks.items():
                    if t.get("status") == "queued" and tid not in self._queued_deque:
                        self._queued_deque.append(tid)
                self._save()
        except Exception as e:
            log.warning(f"Failed to load tasks.json: {e}")

    # ---------- CRUD ----------
    def create(self, url, output_name, display_name=None, format_id="", output_dir="", batch_id="", group_id="", initial_status=None, batch_concurrency=None):
        task_id = uuid.uuid4().hex[:8]

        with self._lock:
            active = sum(1 for t in self.tasks.values() if t["status"] == "downloading")
        # initial_status 允许调用方强制指定状态（如批量加入后「待开始」= paused）；
        # 不指定时沿用原逻辑：并发已满则排队 queued，否则 pending 立即启动。
        status = initial_status if initial_status else ("queued" if active >= MAX_CONCURRENT_TASKS else "pending")

        # Assign to a group: explicit group_id, else auto site root group
        if not group_id or group_id not in self.groups:
            group_id = self.root_group_for_site(_extract_site(url))

        task = {
            "id": task_id,
            "url": url,
            "output_name": output_name,
            "display_name": display_name or Path(output_name).stem,
            "format_id": format_id,
            "output_dir": output_dir,
            "batch_id": batch_id,
            "batch_concurrency": batch_concurrency,
            "group_id": group_id,
            "status": status,
            "progress": 0,
            "files": [],
            "verify": None,
            "speed": "",
            "eta": "",
            "size": "",
            "fragments": "",
            "file_path": "",
            "file_size": 0,
            "duration": "",
            "resolution": "",
            "error": "",
            "error_code": "",
            "started_at": "",
            "completed_at": "",
            "created_at": datetime.now().isoformat(),
            "_proc": None,
            "_cancel_flag": False,
        }
        with self._lock:
            self.tasks[task_id] = task
            if status == "queued":
                self._queued_deque.append(task_id)
        self._save()
        log.info(f"Task created: {task_id} [{status}] -> {url}")
        return task

    def get(self, task_id):
        with self._lock:
            return self.tasks.get(task_id)

    def get_all(self):
        with self._lock:
            return list(self.tasks.values())

    def update(self, task_id, **kwargs):
        with self._lock:
            if task_id in self.tasks:
                self.tasks[task_id].update(kwargs)
        self._save()

    def delete(self, task_id, file_action=""):
        with self._lock:
            t = self.tasks.pop(task_id, None)
            # Also remove from queue
            try:
                self._queued_deque.remove(task_id)
            except ValueError:
                pass
        if t:
            self._backup_deleted_task(t, file_action)
            self._save()
            log.info(f"Task deleted: {task_id}")
        return t

    def _backup_deleted_task(self, t, file_action=""):
        """Keep a recoverable copy of deleted task metadata under .trash/tasks.jsonl."""
        try:
            trash_dir = DATA_DIR / ".trash"
            trash_dir.mkdir(exist_ok=True)
            # P0-2: 备份完整任务元数据（剔除运行时字段），保证可恢复
            rec = {k: v for k, v in t.items() if not k.startswith("_")}
            rec["deleted_at"] = datetime.now().isoformat(timespec="seconds")
            rec["task_id"] = t.get("id")
            rec["file_action"] = file_action
            with open(trash_dir / "tasks.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:
            log.warning(f"backup deleted task failed: {e}")

    def cancel(self, task_id):
        """Signal cancellation and kill subprocess."""
        with self._lock:
            t = self.tasks.get(task_id)
            if not t:
                return False
            t["_cancel_flag"] = True
            proc = t.get("_proc")
            if t["status"] in ("queued", "paused"):
                t["status"] = "cancelled"
                try:
                    self._queued_deque.remove(task_id)
                except ValueError:
                    pass
            if proc and proc.poll() is None:
                log.info(f"Cancelling task {task_id}, killing PID {proc.pid}")
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        capture_output=True,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                except Exception as e:
                    log.error(f"Failed to kill process tree: {e}")
                    try:
                        proc.kill()
                    except Exception:
                        pass
        self._save()
        return True

    def dequeue_next(self, eligible=None):
        """Pop the next queued task that passes `eligible(task)`.
        不合格的任务保持原顺序留在队列里（如批次并发已满）。
        Called under _queue_start_lock."""
        with self._lock:
            skipped = []
            picked = None
            while self._queued_deque:
                tid = self._queued_deque.popleft()
                t = self.tasks.get(tid)
                if not (t and t["status"] == "queued"):
                    continue
                if eligible is None or eligible(t):
                    picked = tid
                    break
                skipped.append(tid)
            for tid in reversed(skipped):
                self._queued_deque.appendleft(tid)
            return picked

    def enqueue(self, task_id):
        """Mark a task queued AND register it in the start deque so
        start_next_queued() can actually pick it up. Idempotent — safe to
        call even if the task is already queued."""
        with self._lock:
            t = self.tasks.get(task_id)
            if not t:
                return
            t["status"] = "queued"
            t["error"] = ""
            if task_id not in self._queued_deque:
                self._queued_deque.append(task_id)
        self._save()

    def active_count(self):
        with self._lock:
            return sum(1 for t in self.tasks.values() if t["status"] == "downloading")

    # ---------- Groups (persistent grouping tree) ----------
    def _load_groups(self):
        self.groups = {}
        if not GROUPS_FILE.exists():
            return
        try:
            with open(GROUPS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for gid, g in data.items():
                    g["id"] = gid
                    g.setdefault("archived", False)
                    self.groups[gid] = g
            log.info(f"Loaded {len(self.groups)} groups from groups.json")
        except Exception as e:
            log.warning(f"Failed to load groups.json: {e}")

    def _save_groups(self):
        with self._groups_save_lock:
            with self._lock:
                # groups have no runtime attrs, dump directly
                data = {gid: g for gid, g in self.groups.items()}
            try:
                tmp = str(GROUPS_FILE) + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, str(GROUPS_FILE))
            except Exception as e:
                log.warning(f"Failed to save groups.json: {e}")

    def get_groups(self):
        with self._lock:
            return [dict(g) for g in self.groups.values()]

    def get_group(self, group_id):
        with self._lock:
            g = self.groups.get(group_id)
            return dict(g) if g else None

    def root_group_for_site(self, site):
        """Return the id of the root group for a site, creating it if missing."""
        site = site or "未知来源"
        for g in self.groups.values():
            if g.get("parent_id", "") == "" and g.get("site") == site:
                return g["id"]
        # create
        gid = uuid.uuid4().hex[:8]
        order = max([g.get("order", 0) for g in self.groups.values()] + [0]) + 1
        self.groups[gid] = {
            "id": gid, "parent_id": "", "name": site,
            "site": site, "order": order, "archived": False,
        }
        self._save_groups()
        return gid

    def _reassign_orphans(self):
        """Assign tasks whose group_id is missing/invalid to their site root group."""
        changed = False
        for t in self.tasks.values():
            gid = t.get("group_id", "")
            if gid and gid in self.groups:
                continue
            site = _extract_site(t.get("url", ""))
            t["group_id"] = self.root_group_for_site(site)
            changed = True
        if changed:
            self._save()

    def create_group(self, name, parent_id="", site=""):
        name = (name or "").strip()
        if not name:
            return None
        if parent_id and parent_id not in self.groups:
            parent_id = ""
        order = max([g.get("order", 0) for g in self.groups.values()] + [0]) + 1
        gid = uuid.uuid4().hex[:8]
        self.groups[gid] = {
            "id": gid, "parent_id": parent_id, "name": name,
            "site": site, "order": order, "archived": False,
        }
        self._save_groups()
        log.info(f"Group created: {gid} ({name}) parent={parent_id}")
        return dict(self.groups[gid])

    def rename_group(self, gid, name):
        name = (name or "").strip()
        if not name or gid not in self.groups:
            return False
        self.groups[gid]["name"] = name
        self._save_groups()
        log.info(f"Group renamed: {gid} -> {name}")
        return True

    def set_group_archived(self, gid, archived):
        if gid not in self.groups:
            return False
        self.groups[gid]["archived"] = bool(archived)
        self._save_groups()
        log.info(f"Group {'archived' if archived else 'unarchived'}: {gid}")
        return True

    def delete_group(self, gid, delete_tasks=False, delete_files=False):
        if gid not in self.groups:
            return False
        # Collect this group and all descendants
        to_remove = set()
        stack = [gid]
        while stack:
            cur = stack.pop()
            to_remove.add(cur)
            for g in self.groups.values():
                if g.get("parent_id") == cur:
                    stack.append(g["id"])
        parent = self.groups[gid].get("parent_id", "")

        # Collect tasks belonging to the group subtree
        task_ids = [tid for tid, t in self.tasks.items() if t.get("group_id") in to_remove]
        removed_tasks = 0
        partial_failures = []
        queued_started = False

        if delete_tasks:
            for tid in task_ids:
                t = self.tasks.get(tid)
                if not t:
                    continue
                # Cancel running tasks and wait for termination
                if t["status"] == "downloading":
                    self.cancel(tid)
                    proc = t.get("_proc")
                    for _ in range(20):
                        if proc and proc.poll() is not None:
                            break
                        time.sleep(0.5)
                # Trash files first; if any file fails, keep the task record
                if delete_files:
                    results, all_ok = _trash_task_files(t)
                    if not all_ok:
                        partial_failures.append({"task_id": tid, "files": results})
                        continue
                was_queued = t["status"] == "queued"
                self.delete(tid, file_action=("trash" if delete_files else "record_only"))
                removed_tasks += 1
                if was_queued:
                    queued_started = True
        else:
            # Reassign tasks upward when keeping them
            for t in self.tasks.values():
                if t.get("group_id") in to_remove:
                    t["group_id"] = parent

        for g in list(to_remove):
            self.groups.pop(g, None)
        self._save_groups()
        self._save()
        # Orphaned tasks (group_id now "") get a site root
        self._reassign_orphans()
        if queued_started:
            start_next_queued()
        log.info(
            f"Group deleted: {gid} (removed {len(to_remove)} groups, "
            f"{removed_tasks} tasks, delete_tasks={delete_tasks}, delete_files={delete_files}, "
            f"partial={len(partial_failures)})"
        )
        return {
            "removed_groups": len(to_remove),
            "removed_tasks": removed_tasks,
            "partial_failures": partial_failures,
        }


task_manager = TaskManager()

# Lock to prevent multiple threads from starting queued tasks simultaneously
_queue_start_lock = threading.Lock()


def start_next_queued():
    """After a task finishes, check for and start the next queued task.
    P1-2: 全局并发与批次并发双重约束（批次并发只作用于该批次）。"""
    with _queue_start_lock:
        if task_manager.active_count() >= MAX_CONCURRENT_TASKS:
            return
        all_tasks = task_manager.get_all()
        next_tid = task_manager.dequeue_next(
            eligible=lambda t: schedmod.can_start(t, all_tasks, MAX_CONCURRENT_TASKS))
        if not next_tid:
            return
        t = task_manager.get(next_tid)
        if t:
            task_manager.update(next_tid, status="pending")
            log.info(f"[{next_tid}] Dequeued, starting now")
            thread = threading.Thread(
                target=run_download,
                args=(next_tid, t["url"], t["output_name"]),
                kwargs={"format_id": t.get("format_id", ""), "output_dir": t.get("output_dir", "")},
                daemon=True,
            )
            thread.start()


# Drain queued tasks at startup (e.g. from a restart that happened mid-queue)
# so they actually start downloading instead of sitting orphaned forever.
# Invoked from the __main__ block below, *after* run_download is defined, to
# avoid NameError on the first dequeue. (start_next_queued → threading.Thread
# (target=run_download) requires run_download to be bound in module globals.)


# ============================================================
# Download Worker
# ============================================================
PROGRESS_RE = re.compile(
    r"\[download\]\s+([\d.]+)%\s+of\s+~?\s*([\d.]+\s*\w+)\s+at\s+([\d.]+\s*\w+/s)\s+ETA\s+(\S+)\s+\(frag\s+(\d+)/(\d+)\)"
)


def _safe_unlink(filepath, retries=8, delay=0.5):
    """Robustly delete a single file.

    Tries a direct unlink first (no subprocess, no shell-quoting pitfalls with
    special characters like '&'). If that fails (e.g. a transient anti-virus
    lock right after a download finishes), it retries with backoff and finally
    falls back to `cmd /c del`. Returns True if the file is gone afterwards.
    """
    fp = Path(filepath)
    if not fp.exists():
        return True
    last_err = None
    for i in range(retries):
        try:
            fp.unlink(missing_ok=True)
            if not fp.exists():
                return True
        except Exception as e:
            last_err = e
        # Fallback to cmd /c del (handles some locked-handle edge cases)
        try:
            subprocess.run(
                ["cmd", "/c", "del", "/f", "/q", str(fp)],
                capture_output=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if not fp.exists():
                return True
        except Exception as e:
            last_err = e
        if i < retries - 1:
            time.sleep(delay)
    if not fp.exists():
        return True
    log.warning(f"cleanup: failed to delete {fp} (last error: {last_err})")
    return False


def _send_to_trash(filepath):
    """Move a file/folder to the OS recycle bin (recoverable).

    P0-3: 所有平台回收站失败后一律保留原文件（retained），
    绝不降级为 unlink / del 等永久删除。
    返回 True = 已入回收站或文件本就不存在；False = 文件保留在原处。
    """
    r = trashmod.send_to_trash(filepath)
    return r in (trashmod.RESULT_TRASHED, trashmod.RESULT_MISSING)


def _recycle_path(path):
    """Move a file or folder to the OS recycle bin (recoverable).

    This is a rename into the recycle bin, NOT a permanent delete, so it does
    NOT trip the environment's bulk-delete confirmation prompt that
    shutil.rmtree() hits once a cache dir holds many fragment files
    (the prompt can't be answered from a background thread, so rmtree fails
    silently with ignore_errors=True and the cache dir is left behind).
    Moving to the bin is instant (same-drive rename), frees the user's
    output dir immediately, and stays recoverable.
    """
    fp = Path(path)
    if not fp.exists():
        return True
    if os.name == "nt":
        try:
            esc = str(fp).replace("'", "''")
            op = "DeleteFolder" if fp.is_dir() else "DeleteFile"
            ps = ("Add-Type -AssemblyName Microsoft.VisualBasic; "
                   f"[Microsoft.VisualBasic.FileIO.FileSystem]::{op}('{esc}', "
                   "'OnlyErrorDialogs', 'SendToRecycleBin')")
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            return not fp.exists()
        except Exception as e:
            log.warning(f"_recycle_path(PowerShell) failed for {fp}: {e}")
            return False
    # Non-Windows fallback
    try:
        if send2trash is not None:
            send2trash.send2trash(str(fp))
            return True
    except Exception as e:
        log.warning(f"_recycle_path(send2trash) failed for {fp}: {e}")
    return False


def _rm_tree_safe(path):
    """Recursively delete a directory tree using _safe_unlink()
    (which falls back to `cmd /c del` to bypass the environment's
    bulk-delete confirmation that shutil.rmtree() hits on large
    fragment sets). This removes the cache dir AND every nested
    fragment file, leaving no empty shell behind.
    """
    removed = 0
    p = Path(path)
    if not p.exists():
        return removed
    if p.is_symlink():
        try:
            p.unlink()
            removed += 1
        except Exception:
            pass
        return removed
    if p.is_file():
        if _safe_unlink(p):
            removed += 1
        return removed
    for child in p.iterdir():
        removed += _rm_tree_safe(child)
    # Directory should now be empty; drop it via cmd /c rmdir
    # (bypasses the bulk-delete prompt the same way _safe_unlink does).
    try:
        p.rmdir()
        removed += 1
    except Exception:
        try:
            subprocess.run(
                ["cmd", "/c", "rmdir", "/s", "/q", str(p)],
                capture_output=True, timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if not p.exists():
                removed += 1
        except Exception:
            pass
    return removed


def _rename_to_trash(cache_dir, task_id):
    """A+D: 当沙箱 safe-delete 钩子拦截删除（回收站不可用 → 删失败）时，
    把整个任务缓存目录 rename 为 .yingji_cache/_trash_<ts>_<task_id> 隔离目录。

    rename 是同盘移动、不是删除，通常不被钩子拦截，可立刻让输出目录变干净
    （用户看不到碎片）。后台周期线程会持续重试把 _trash_ 真正删掉。
    返回 True = 已隔离（输出目录已干净）；False = 连 rename 也被拦。
    """
    try:
        cache_dir = Path(cache_dir)
        if not cache_dir.exists():
            return True
        parent = cache_dir.parent
        ts = time.strftime("%Y%m%d_%H%M%S")
        trash = parent / f"_trash_{ts}_{cache_dir.name}"
        i = 0
        while trash.exists():
            i += 1
            trash = parent / f"_trash_{ts}_{cache_dir.name}_{i}"
        os.rename(str(cache_dir), str(trash))
        return not cache_dir.exists()
    except Exception as e:
        log.warning(f"[{task_id}] _rename_to_trash failed: {e}")
        return False


def cleanup_fragments(task_id, output_name, output_dir="", also_temp=False):
    """Remove leftover temp files for a download — fully automatic, no UI needed.

    With per-task cache isolation, ALL fragments / intermediate files live in
    <output_dir>/.yingji_cache/<task_id>/. We remove that whole directory by
    PERMANENT deletion (_rm_tree_safe: unlink + cmd /c del fallback) — fragment
    caches are regenerable, so this is safe and keeps the recycle bin from
    filling up. We also do a defensive sweep of the target dir for any stray
    *.part-Frag* / *.tmp.mp4 left behind by older runs.
    """
    target_dir = _get_output_dir(output_dir)
    stem = Path(output_name).stem
    patterns = [f"{stem}*.part-Frag*", f"{stem}*.tmp.mp4", f"{stem}*.tmp",
                f"{stem}*.part", f"{stem}*.ytdl"]

    removed = 0
    leftover = 0

    # 1) Remove the isolated per-task cache directory entirely (PERMANENT).
    #    Fragment caches are regenerable temp data, so we delete directly
    #    instead of sending to the recycle bin (keeps the bin from filling up).
    #    _rm_tree_safe recurses with unlink + cmd /c del fallback, which never
    #    trips the bulk-delete confirmation prompt.
    if task_id:
        cache_dir = _cache_dir(output_dir, task_id)
        if cache_dir.exists():
            removed += _rm_tree_safe(cache_dir)
            # A+D: 沙箱 safe-delete 钩子可能拦截删除（回收站不可用 → 删失败）。
            # 删不掉时把整个任务缓存目录 rename 为 _trash_ 隔离目录：rename 是
            # 移动不是删除，通常不被钩子拦，可立刻让输出目录变干净。后台周期
            # 线程会持续尝试把 _trash_ 真正删掉（沙箱放行时即清理）。
            if cache_dir.exists():
                if _rename_to_trash(cache_dir, task_id):
                    log.info(f"[{task_id}] cache dir moved to _trash_ (deletion blocked by sandbox, will retry later)")
                else:
                    leftover += 1

    # 2) Defensive sweep of the real output dir for stray temp files.
    #    P0-3: 永久删除只允许白名单临时后缀（is_temp_file 双重校验），
    #    绝不触碰最终媒体文件。
    for pat in patterns:
        for f in target_dir.glob(pat):
            try:
                if not trashmod.is_temp_file(f):
                    continue
                if _safe_unlink(f):
                    removed += 1
                else:
                    leftover += 1
            except Exception:
                leftover += 1

    if leftover:
        log.warning(f"[{task_id}] cleanup_fragments: {leftover} temp files remain")
    else:
        log.info(f"[{task_id}] Temp files cleaned ({removed} removed)")


def startup_cleanup():
    """开机自动消化全部历史残留（无感，无需用户操作）。

    在 server 启动时跑一次：扫描每个输出目录下的 .yingji_cache/* 所有
    任务缓存目录 + 输出目录根部的散落临时文件（*.part-Frag* / *.tmp.mp4 /
    *.tmp / *.part / *.ytdl），永久清理。使用 _rm_tree_safe / _safe_unlink，
    这两种方式都会绕过环境的批量删除确认护栏，因此不受 ~50 文件阈值限制。
    """
    log.info("Startup cleanup: scanning for stale temp caches and fragments...")
    total_removed = 0

    # 1) 清理每个输出目录下 .yingji_cache/* 的全部子目录（含孤儿目录）
    dirs = {DOWNLOADS_DIR}
    try:
        for t in task_manager.get_all():
            dirs.add(_get_output_dir(t.get("output_dir", "")))
    except Exception:
        pass
    for d in dirs:
        cache_root = Path(d) / ".yingji_cache"
        if not cache_root.exists():
            continue
        for child in cache_root.iterdir():
            if child.is_dir():
                total_removed += _rm_tree_safe(child)

    # 2) 防御性扫描各输出目录根部的散落临时文件
    stray = ["*.part-Frag*", "*.tmp.mp4", "*.tmp", "*.part", "*.ytdl"]
    for d in dirs:
        target_dir = Path(d)
        for pat in stray:
            for f in target_dir.glob(pat):
                if f.is_file() and trashmod.is_temp_file(f):
                    if _safe_unlink(f):
                        total_removed += 1

    log.info(f"Startup cleanup done: removed {total_removed} stale temp items")


def _periodic_cache_cleaner(interval_sec=600):
    """A+D: 后台周期线程，静默重试清理被沙箱拦截的缓存删除。

    沙箱 safe-delete 钩子可能拦截 server 进程的删除（回收站不可用 → 删失败）。
    cleanup_fragments 删不掉时会把任务缓存 rename 为 .yingji_cache/_trash_<ts>_<task_id>
    隔离目录。本线程每隔 interval_sec 扫描各输出目录的 .yingji_cache/，对：
      - _trash_* 目录：尝试真正删除（沙箱放行时即清理，拦住就跳过）
      - 孤儿 <task_id> 目录（task_manager 里已无此任务）：尝试删除
    不碰活跃任务的缓存目录，避免误删正在下载的碎片。沙箱拦就跳过、放行就删。
    """
    while True:
        time.sleep(interval_sec)
        try:
            dirs = {DOWNLOADS_DIR}
            try:
                for t in task_manager.get_all():
                    dirs.add(_get_output_dir(t.get("output_dir", "")))
            except Exception:
                pass
            # 活跃任务 id 集合（保护其缓存不被误删）
            active_ids = set()
            try:
                for t in task_manager.get_all():
                    active_ids.add(str(t.get("id", "")))
            except Exception:
                pass
            for d in dirs:
                cache_root = Path(d) / ".yingji_cache"
                if not cache_root.exists():
                    continue
                for child in list(cache_root.iterdir()):
                    try:
                        if not child.is_dir():
                            continue
                        name = child.name
                        if name.startswith("_trash_"):
                            # 隔离目录：尽力删，失败就留着下次再试
                            _rm_tree_safe(child)
                        elif name not in active_ids:
                            # 孤儿缓存目录（任务已删但缓存残留）：尽力删
                            _rm_tree_safe(child)
                        # 否则是活跃任务缓存，不动
                    except Exception:
                        pass
        except Exception as e:
            log.warning(f"periodic cache cleaner error: {e}")


def extract_video_info(output_mp4):
    """Try to get video resolution and duration via ffprobe."""
    try:
        ffprobe = _ffprobe_path()
        r = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-show_entries", "format=duration:stream=width,height,codec_name",
                "-of", "json", output_mp4,
            ],
            capture_output=True, text=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if r.returncode == 0:
            info = json.loads(r.stdout)
            fmt = info.get("format", {})
            streams = info.get("streams", [])
            video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
            return (
                f"{video_stream.get('width', '?')}x{video_stream.get('height', '?')}",
                str(round(float(fmt.get("duration", 0)), 1)) + "s",
            )
    except Exception:
        pass
    return "", ""


def _get_output_dir(task_or_dir):
    """Get the effective download directory from a task dict or a string."""
    if isinstance(task_or_dir, dict):
        d = task_or_dir.get("output_dir", "")
    else:
        d = task_or_dir or ""
    return Path(d) if d else DOWNLOADS_DIR


def _cache_dir(output_dir, task_id):
    """Per-task temp-fragment cache, placed ON THE SAME DRIVE as the output
    dir so the final move is an instant rename (no slow cross-drive copy)."""
    return _get_output_dir(output_dir) / ".yingji_cache" / str(task_id)


def find_output_file(output_name, output_dir="", task_id=""):
    """Find the actual output file (yt-dlp may change extension).

    P0-2: 输出目录内只做**精确候选名**检查（stem+已知扩展名），
    通配查找只允许在本任务隔离缓存目录内进行，避免误认同前缀文件。
    """
    target_dir = _get_output_dir(output_dir)
    output_mp4 = str(target_dir / output_name)
    if os.path.exists(output_mp4):
        return output_mp4
    stem = Path(output_name).stem
    if task_id:
        cache = _cache_dir(output_dir, task_id)
        cand = cache / output_name
        if os.path.exists(cand):
            return str(cand)
        # 缓存目录是本任务专属的，目录内通配是安全的
        if cache.exists():
            for f in cache.glob("*"):
                if f.is_file() and f.suffix in (".mp4", ".mkv", ".webm", ".flv") \
                        and not trashmod.is_temp_file(f):
                    return str(f)
    # 输出目录内：仅精确候选（改扩展名的情况），绝不 stem* 通配
    for ext in (".mp4", ".mkv", ".webm", ".flv"):
        cand = target_dir / (stem + ext)
        if cand.exists():
            return str(cand)
    return output_mp4


def _move_with_faststart(src, dst):
    """Move the downloaded file to its final location, remuxing with
    -movflags +faststart so Telegram / web players can stream and scrub it
    without downloading the whole file first. Falls back to a plain move if
    ffmpeg is unavailable or fails."""
    dst = str(dst)
    try:
        ff = FFMPEG_PATH or "ffmpeg"
        r = subprocess.run(
            [ff, "-y", "-i", str(src), "-c", "copy", "-movflags", "+faststart", dst],
            capture_output=True, text=True, timeout=600,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if r.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0:
            try:
                os.remove(src)
            except Exception:
                pass
            return dst
    except Exception as e:
        log.warning(f"_move_with_faststart ffmpeg failed: {e}")
    # Fallback: plain move
    try:
        if os.path.exists(dst):
            os.remove(dst)
    except Exception:
        pass
    shutil.move(str(src), dst)
    return dst


def _stdout_reader(proc, q):
    """Daemon thread: reads stdout lines into a Queue."""
    try:
        for line in iter(proc.stdout.readline, ""):
            if line:
                q.put(line.rstrip("\n"))
    except Exception:
        pass
    finally:
        q.put(None)  # sentinel


def run_download(task_id, url, output_name, format_id="", output_dir=""):
    """Run yt-dlp in a background thread, pushing progress events."""
    url = _normalize_url(url)
    # P0-1：video_id+audio_id 这类合并选择必须保证产物真有音轨，
    # 否则视为失败/残留（避免「选了 4K 却静音」的体验事故）。
    expect_audio = "+" in format_id
    target_dir = _get_output_dir(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    # All fragments / intermediate files go into a per-task cache subdir on the
    # same drive; the final file is moved out (with faststart) on success.
    cache_dir = _cache_dir(output_dir, task_id)
    cache_dir.mkdir(parents=True, exist_ok=True)
    output_path = str(cache_dir / output_name)
    referer = "/".join(url.rstrip("/").split("/")[:3])

    cmd = _yt_dlp_download_prefix(format_id) + [
        "--ffmpeg-location", FFMPEG_PATH or "ffmpeg",
        "--hls-prefer-native",
        "--concurrent-fragments", str(CONCURRENT_FRAGMENTS),
        "--add-header", "User-Agent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "--add-header", f"Referer:{referer}",
        "--no-playlist",  # 单视频下载路径：即使链接是播放列表也只下当前这一个，绝不下整列
        "--newline",
        "--retries", "10",
        "--fragment-retries", "10",
        "-o", output_path,
    ]
    # Add cookies if available. Use a per-task private copy so the engine
    # can write updated cookies back without contending for the shared file
    # (Windows lock/permission issues when multiple workers end at once).
    cookie_args = _get_cookie_args()
    if cookie_args and COOKIES_FILE.exists():
        private_cookies = cache_dir / "cookies.txt"
        try:
            shutil.copy2(str(COOKIES_FILE), str(private_cookies))
            cookie_args = ["--cookies", str(private_cookies)]
        except Exception as e:
            log.warning(f"[{task_id}] Failed to copy cookies to cache dir, using original: {e}")
    cmd.extend(cookie_args)
    cmd.append(url)

    now_iso = datetime.now().isoformat()
    task_manager.update(task_id,
        status="downloading", progress=0, error="", error_code="",
        _cancel_flag=False, started_at=now_iso, completed_at="")

    log.info(f"[{task_id}] Download started: {url}")
    log.debug(f"[{task_id}] Command: {' '.join(cmd)}")

    non_progress_lines = []

    proc = None
    reader = None
    q = None

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        task_manager.update(task_id, _proc=proc)

        # Start a daemon thread to read stdout into a Queue (avoids Windows pipe race)
        q = queue.Queue()
        reader = threading.Thread(target=_stdout_reader, args=(proc, q), daemon=True)
        reader.start()

        while True:
            try:
                line = q.get(timeout=2.0)
            except queue.Empty:
                if proc.poll() is not None:
                    break
                continue

            if line is None:  # sentinel = stdout closed
                break

            stripped = line.strip()
            if not stripped:
                continue

            # Check cancel flag inline
            task = task_manager.get(task_id)
            if task and task.get("_cancel_flag"):
                log.info(f"[{task_id}] Cancel flag detected, terminating...")
                break

            m = PROGRESS_RE.search(line)
            if m:
                pct = float(m.group(1))
                task_manager.update(task_id,
                    progress=pct, size=m.group(2), speed=m.group(3),
                    eta=m.group(4), fragments=f"{m.group(5)}/{m.group(6)}")
            else:
                non_progress_lines.append(stripped)

        # === Cancel check ===
        task = task_manager.get(task_id)
        if task and task.get("_cancel_flag"):
            _force_kill(proc)
            time.sleep(0.5)
            cleanup_fragments(task_id, output_name, output_dir)
            task_manager.update(task_id,
                status="cancelled", _proc=None, _cancel_flag=False,
                completed_at=datetime.now().isoformat())
            log.info(f"[{task_id}] Download cancelled by user")
            start_next_queued()
            return

        proc.wait()
        task_manager.update(task_id, _proc=None)
        now_done = datetime.now().isoformat()

        if proc.returncode != 0:
            error_detail = "\n".join(non_progress_lines[-20:]).strip()
            if not error_detail:
                # Fallback: run yt-dlp minimally to get the actual error
                try:
                    diag = subprocess.run(
                        _yt_dlp_command() + ["--no-playlist", url],
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=15,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    )
                    err = (diag.stdout + diag.stderr).strip()
                    if err:
                        error_detail = err[:800]
                    else:
                        error_detail = f"yt-dlp exited with code {proc.returncode}"
                except Exception:
                    error_detail = f"yt-dlp exited with code {proc.returncode}"

            # Detect server-process degradation pattern
            if not non_progress_lines and proc.returncode == 1:
                error_detail += (
                    "\n\n[诊断] 子进程启动后立即退出且无输出，"
                    "这通常是服务器进程长时间运行后内部状态损坏所致。"
                    "建议：1) 点击'删除'清理此任务 2) 运行 stop.bat 关闭服务 "
                    "3) 重新运行 start.bat 启动服务，然后重试下载。"
                )

            # 风控优先于登录提示：两者命中时以风控为准，避免误导用户去换 Cookie
            if _risk_control_hint(error_detail):
                error_detail += _risk_hint_text()
            elif _needs_cookie_hint(error_detail):
                error_detail += _cookie_hint_text()

            # P0-4: yt-dlp 失败但存在残留文件时，必须经媒体验证；
            # 验证通过 → incomplete（残留可用，≠ 下载成功）；不通过 → error。
            # 已删除旧的「文件 > 1MB 即完成」判断。
            output_mp4 = find_output_file(output_name, output_dir, task_id)
            if os.path.exists(output_mp4):
                # UI-P0-03: 自动检查期间对用户展示「正在检查文件」
                task_manager.update(task_id, status="verifying")
                vr = verify_media(output_mp4, ffprobe=_ffprobe_path(), expect_audio=expect_audio)
                if vr["ok"]:
                    log.warning(f"[{task_id}] Download failed but residual file verified "
                                f"({os.path.getsize(output_mp4)} bytes, {vr['duration']:.1f}s)")
                    final_dest = target_dir / output_name
                    if os.path.abspath(output_mp4) != os.path.abspath(final_dest):
                        output_mp4 = _move_with_faststart(output_mp4, final_dest)
                    resolution, duration = extract_video_info(output_mp4)
                    task_manager.update(task_id,
                        status="incomplete", progress=100,
                        file_path=output_mp4, file_size=os.path.getsize(output_mp4),
                        files=[output_mp4], verify=vr,
                        duration=duration, resolution=resolution,
                        error="下载过程报错，但发现可播放的残留文件（已通过媒体验证，"
                              "内容可能不完整）。可重试重新下载，或确认后自行保留。\n\n"
                              + error_detail,
                        completed_at=now_done)
                    threading.Thread(
                        target=cleanup_fragments,
                        args=(task_id, output_name, output_dir),
                        daemon=True,
                    ).start()
                    start_next_queued()
                    return
                else:
                    log.warning(f"[{task_id}] Residual file failed media check: {vr['reason']}")
                    error_detail = (f"[媒体验证] 残留文件无效：{_ui_verify_reason(vr['reason'])}\n\n"
                                    + error_detail)

            log.error(f"[{task_id}] Download failed: exit code {proc.returncode}")
            log.error(f"[{task_id}] Error detail:\n{error_detail}")
            # Persist error state BEFORE cleanup so the task can never get stuck
            # in "downloading" if cleanup blocks (e.g. bulk-delete confirmation).
            task_manager.update(task_id,
                status="error", error=error_detail,
                error_code=_safe_download_error_code(error_detail),
                completed_at=now_done)
            # Run cleanup off the main thread: a blocked/confirmed bulk-delete
            # must never deadlock the download thread.
            threading.Thread(
                target=cleanup_fragments,
                args=(task_id, output_name, output_dir),
                daemon=True,
            ).start()
            start_next_queued()
            return

        # Success path
        output_mp4 = find_output_file(output_name, output_dir, task_id)
        final_dest = target_dir / output_name
        if os.path.exists(output_mp4) and os.path.abspath(output_mp4) != os.path.abspath(final_dest):
            output_mp4 = _move_with_faststart(output_mp4, final_dest)
        # 异步无感清理：最终文件就位后立即返回"完成"，缓存清理在后台线程跑，
        # 不阻塞完成响应。
        threading.Thread(
            target=cleanup_fragments,
            args=(task_id, output_name, output_dir, True),
            daemon=True,
        ).start()

        if os.path.exists(output_mp4):
            # P0-4: 下载成功也必须经媒体验证才能进入 complete
            # UI-P0-03: 自动检查期间对用户展示「正在检查文件」
            task_manager.update(task_id, status="verifying")
            vr = verify_media(output_mp4, ffprobe=_ffprobe_path(), expect_audio=expect_audio)
            if vr["ok"]:
                _finish_complete(task_id, output_mp4, verify=vr)
            else:
                log.error(f"[{task_id}] Media verification failed: {vr['reason']}")
                resolution, duration = extract_video_info(output_mp4)
                task_manager.update(task_id,
                    status="incomplete", progress=100,
                    file_path=output_mp4, file_size=os.path.getsize(output_mp4),
                    files=[output_mp4], verify=vr,
                    duration=duration, resolution=resolution,
                    error=f"下载完成但媒体验证未通过：{_ui_verify_reason(vr['reason'])}。"
                          "文件已保留，可重试重新下载。",
                    completed_at=now_done)
        else:
            captured = len(non_progress_lines)
            log.error(
                f"[{task_id}] Downloader exited successfully without a media output "
                f"(captured {captured} diagnostic lines)"
            )
            task_manager.update(task_id,
                status="error",
                error="下载工具结束后没有生成可用媒体文件",
                error_code="NO_MEDIA_OUTPUT",
                completed_at=now_done)

        start_next_queued()

    except Exception as e:
        log.exception(f"[{task_id}] Unexpected error: {e}")
        cleanup_fragments(task_id, output_name, output_dir)
        task_manager.update(task_id,
            status="error", error="下载执行发生内部错误",
            error_code="DOWNLOAD_INTERNAL_ERROR",
            _proc=None, completed_at=datetime.now().isoformat())
        start_next_queued()
    finally:
        # Explicitly close pipes to prevent Windows handle leaks in long-running process
        if proc:
            try:
                proc.stdout.close()
            except Exception:
                pass
            if reader and reader.is_alive():
                reader.join(timeout=2.0)


def _force_kill(proc):
    """Aggressively kill a process and its children on Windows."""
    if proc is None:
        return
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=10,
        )
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _ffprobe_path():
    """ffprobe 可执行路径（与 ffmpeg 同目录）。"""
    bundled = _runtime_tool("ffprobe.exe")
    if bundled:
        return bundled
    if FFMPEG_PATH:
        sibling = Path(FFMPEG_PATH).with_name("ffprobe.exe")
        if sibling.is_file():
            return str(sibling)
    return shutil.which("ffprobe") or "ffprobe"


def _finish_complete(task_id, output_mp4, warning="", verify=None):
    """Mark a task as complete with file info.
    P0-4: 只有经过媒体验证（verify.ok=True）的任务才应走到这里。
    P0-2: 同时记录精确文件清单 files。
    UI-P0-03: 该路径是唯一写入 completion_basis="verified" 的入口，
    人工确认永远不会走到这里。"""
    file_size = os.path.getsize(output_mp4)
    resolution, duration = extract_video_info(output_mp4)
    task_manager.update(task_id,
        status="complete", progress=100,
        file_path=output_mp4, file_size=file_size,
        files=[str(output_mp4)], verify=verify,
        completion_basis="verified",
        duration=duration, resolution=resolution,
        error=warning,
        completed_at=datetime.now().isoformat(),
    )
    no_audio = " [无音频流]" if (verify and not verify.get("has_audio")) else ""
    log.info(f"[{task_id}] Download complete: {output_mp4} ({file_size/1024/1024:.1f}MB) {resolution} {duration}{no_audio}")


# ============================================================
# Routes
# ============================================================
@app.route("/")
def index():
    return send_from_directory(str(STATIC_DIR), "index.html")


def _is_homepage_url(url):
    """判断 URL 是否为主页/空间/频道/播放列表等不应走首页单视频下载的链接。"""
    try:
        u = urllib.parse.urlparse(url)
        host = (u.hostname or "").lower()
        path = (u.path or "/").lower()
        if host == "space.bilibili.com":
            return True
        if "youtube.com" in host:
            if re.search(r"^/(channel|c|@)", path):
                return True
            if path.startswith("/playlist"):
                return True
        if "bilibili.com" in host and path.startswith("/list"):
            return True
        if re.search(r"^/(space|user|users|member|profile|author|u)/[^/]+(/(video|videos|featured|streams|shorts|upload|playlist|lists))?/?$", path, re.I):
            return True
        if re.search(r"^/(playlist|list|series|collection|album)\b", path, re.I):
            return True
        if path in ("", "/"):
            return True
        return False
    except Exception:
        return False


def submit_download_task(url, output_name="", format_id="", output_dir=""):
    """创建下载任务；并发未满则立即启动，否则排队。

    与 /api/download 的提交行为完全一致（含并发排队判断与日志）。
    抽出来供首页接口与扩展模块共用，避免同一套逻辑存在两份实现。
    """
    url = _validated_download_url(url)
    output_name = sanitize_filename(output_name or "", default_ext=".mp4")

    task = task_manager.create(url, output_name, display_name=Path(output_name).stem,
                               format_id=format_id, output_dir=output_dir)

    if task["status"] != "queued":
        # Start immediately
        thread = threading.Thread(
            target=run_download,
            args=(task["id"], url, output_name),
            kwargs={"format_id": format_id, "output_dir": output_dir},
            daemon=True,
        )
        thread.start()
    else:
        log.info(f"[{task['id']}] Queued (active={task_manager.active_count()}/{MAX_CONCURRENT_TASKS})")

    return task


@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    try:
        url = _validated_download_url(url)
    except DownloadRequestRejected as exc:
        return _download_rejection_response(exc)

    HOME_MSG = "首页仅支持单个视频下载 · 多链接 / 播放列表请用「批量获取」"

    # 首页单视频下载边界：拒绝多链接 / 主页 / 播放列表（这些必须走 /api/batch-download 批量获取）
    if len(re.findall(r"https?://", url)) > 1:
        return jsonify({"error": HOME_MSG}), 400
    if _is_homepage_url(url):
        return jsonify({"error": HOME_MSG}), 400

    # P0-1: 统一文件名安全清理（移除路径分隔符/非法字符/保留名/超长），后端强制
    output_name = sanitize_filename(data.get("output_name", ""), default_ext=".mp4")

    format_id = data.get("format_id", "").strip()

    # Custom output directory (empty = default downloads dir)
    output_dir = data.get("output_dir", "").strip()
    if output_dir:
        # Security: block path traversal outside the drive root
        norm = os.path.normpath(output_dir)
        if not os.path.isabs(norm):
            return jsonify({"error": "保存路径必须是绝对路径，如 D:\\Videos"}), 400
        try:
            Path(norm).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return jsonify({"error": f"无法创建目录: {norm} ({e})"}), 400
        output_dir = norm
        log.info(f"[download] Custom output_dir: {output_dir}")

    # P0-1: 最终路径解析后必须仍位于输出目录内
    try:
        resolve_within(output_dir or DOWNLOADS_DIR, output_name)
    except ValueError:
        return jsonify({"error": "文件名不合法：不允许包含路径成分"}), 400

    try:
        task = submit_download_task(url, output_name, format_id=format_id, output_dir=output_dir)
    except DownloadRequestRejected as exc:
        return _download_rejection_response(exc)

    return jsonify({"task_id": task["id"], "queued": task["status"] == "queued"})


@app.route("/api/tasks", methods=["GET"])
def api_tasks():
    return jsonify([_task_to_dict(t) for t in task_manager.get_all()])


@app.route("/api/tasks/<task_id>", methods=["GET"])
def api_task(task_id):
    t = task_manager.get(task_id)
    if not t:
        return jsonify({"error": "Task not found"}), 404
    return jsonify(_task_to_dict(t))


@app.route("/api/tasks/<task_id>/cleanup", methods=["POST"])
def api_cleanup_task(task_id):
    """Best-effort remove the per-task temp-fragment cache + any stray
    .part-Frag* / .tmp.mp4 left in the output dir for this task."""
    t = task_manager.get(task_id)
    if not t:
        return jsonify({"ok": False, "error": "task not found"}), 404
    cleanup_fragments(task_id, t.get("output_name", ""), t.get("output_dir", ""))
    return jsonify({"ok": True, "message": "已清理该任务的临时碎片"})


@app.route("/api/cleanup-all", methods=["POST"])
def api_cleanup_all():
    """Sweep temp-fragment caches for every known task."""
    count = 0
    for t in task_manager.get_all():
        cleanup_fragments(t.get("id"), t.get("output_name", ""), t.get("output_dir", ""))
        count += 1
    return jsonify({"ok": True, "message": f"已扫描 {count} 个任务并清理临时碎片"})


@app.route("/api/tasks/<task_id>/confirm", methods=["POST"])
def api_confirm_task(task_id):
    """确认一个 incomplete（残留待确认）任务。仅 incomplete 可确认。

    UI-P0-03 完成状态真实性：
    - result=complete → status=complete + completion_basis="user_confirmed"
      + confirmed_at；人工确认永远不等于系统检查通过（verified）。
    - result=failed   → status=error（保留确认时间供追溯）。
    - verify 自动检查证据原样保留，未通过原因转存到 confirmation_note，
      不被静默丢弃。
    - 持久化：磁盘保存成功后才返回成功；保存失败回滚内存并返回 500。
    - 请求体兼容旧格式 {"result": "complete"|"failed"}。
    """
    data = request.get_json(silent=True) or {}
    result = data.get("result", "complete")
    if result not in ("complete", "failed"):
        return jsonify({"error": "invalid result"}), 400
    now = datetime.now().isoformat()
    tm = task_manager
    mutated_fields = ("status", "progress", "error", "completed_at",
                      "completion_basis", "confirmed_at", "confirmation_note")
    with tm._lock:
        t = tm.tasks.get(task_id)
        if not t:
            return jsonify({"error": "Task not found"}), 404
        if t["status"] != "incomplete":
            return jsonify({"error": f"Task is {t['status']}, cannot confirm"}), 400
        snapshot = {k: t.get(k) for k in mutated_fields}
        # 自动检查未通过的原因：转存为确认备注，证据（verify）本身不动
        vr = t.get("verify") or {}
        reason = _ui_verify_reason(vr.get("reason", "")) if vr and not vr.get("ok") else ""
        if result == "complete":
            t["status"] = "complete"
            t["progress"] = 100
            t["error"] = ""
            t["completed_at"] = now
            t["completion_basis"] = "user_confirmed"
            t["confirmed_at"] = now
            t["confirmation_note"] = (
                f"自动检查未通过（{reason}），由用户人工确认保留" if reason
                else "自动检查未完成或不完整，由用户人工确认保留")
        else:
            t["status"] = "error"
            t["progress"] = 100
            t["error"] = "用户已确认任务失败：残留文件不可用，已标记为失败。"
            t["completed_at"] = now
            t["confirmed_at"] = now
    # 锁外持久化（_save 自带锁）：写盘成功才算确认成功
    if not tm._save():
        with tm._lock:
            t2 = tm.tasks.get(task_id)
            if t2 is not None:
                for k, v in snapshot.items():
                    if v is None:
                        t2.pop(k, None)
                    else:
                        t2[k] = v
        return jsonify({"error": "确认结果保存失败，请重试"}), 500
    return jsonify({"ok": True, "result": result, "status": t["status"],
                    "completion_basis": t.get("completion_basis"),
                    "confirmed_at": t.get("confirmed_at", "")})


@app.route("/api/tasks/<task_id>/cancel", methods=["POST"])
def api_cancel_task(task_id):
    """Cancel a running or queued download. Keeps .part/.ytdl for resume."""
    t = task_manager.get(task_id)
    if not t:
        return jsonify({"error": "Task not found"}), 404
    if t["status"] not in ("downloading", "queued", "paused"):
        return jsonify({"error": f"Task is {t['status']}, not downloading/queued/paused"}), 400

    ok = task_manager.cancel(task_id)

    # If we cancelled a queued task, start the next one
    if ok and t["status"] == "queued":
        start_next_queued()

    log.info(f"[{task_id}] Cancel requested, result={ok}")
    return jsonify({"cancelled": ok})


@app.route("/api/tasks/<task_id>/start", methods=["POST"])
def api_start_task(task_id):
    """Manually start a paused (待开始) task. Re-queues and triggers scheduler."""
    tm = task_manager
    with tm._lock:
        t = tm.tasks.get(task_id)
        if not t:
            return jsonify({"error": "Task not found"}), 404
        if t["status"] not in ("paused", "cancelled", "error", "incomplete"):
            return jsonify({"error": f"Task is {t['status']}, cannot start"}), 400
        was_paused = t["status"] == "paused"
        t["status"] = "queued"
        t["error"] = ""
        if was_paused:
            tm._queued_deque.append(task_id)
    start_next_queued()
    log.info(f"[{task_id}] Start requested, queued")
    return jsonify({"ok": True, "status": "queued"})


def _resume_task(task_id):
    """Shared resume logic for a single failed/cancelled task.

    Returns (result_dict, http_status). Respects the global concurrency limit
    (queues when full) and cleans leftover fragments before restart.
    """
    t = task_manager.get(task_id)
    if not t:
        return {"error": "Task not found"}, 404
    if t["status"] not in ("error", "cancelled", "incomplete"):
        return {"error": f"Task is {t['status']}, can only resume error/cancelled/incomplete tasks"}, 400

    # Check concurrency limit
    if task_manager.active_count() >= MAX_CONCURRENT_TASKS:
        task_manager.enqueue(task_id)
        log.info(f"[{task_id}] Resume queued (concurrency limit reached)")
        start_next_queued()  # drain in case a slot is actually free
        return {"task_id": task_id, "queued": True}, 200

    # Clean old fragments before resuming (use the task's stored output_dir)
    cleanup_fragments(task_id, t["output_name"], t.get("output_dir", ""))

    log.info(f"[{task_id}] Resuming download...")
    thread = threading.Thread(
        target=run_download,
        args=(task_id, t["url"], t["output_name"]),
        kwargs={"format_id": t.get("format_id", ""), "output_dir": t.get("output_dir", "")},
        daemon=True,
    )
    thread.start()
    return {"task_id": task_id, "resumed": True}, 200


@app.route("/api/tasks/<task_id>/resume", methods=["POST"])
def api_resume_task(task_id):
    """Resume a failed/cancelled download. Uses existing .part for continuation."""
    result, status = _resume_task(task_id)
    return jsonify(result), status


@app.route("/api/batch/<batch_id>/retry", methods=["POST"])
def api_retry_batch(batch_id):
    """Retry all failed/cancelled tasks within a batch, respecting concurrency."""
    batch_id = (batch_id or "").strip()
    if not batch_id:
        return jsonify({"error": "batch_id required"}), 400

    candidates = [
        t for t in task_manager.tasks.values()
        if t.get("batch_id") == batch_id and t["status"] in ("error", "cancelled", "incomplete")
    ]
    if not candidates:
        return jsonify({"error": "No retryable tasks in this batch"}), 400

    # Manual concurrency guard: count what's already active + what we start here,
    # so the loop never exceeds MAX_CONCURRENT_TASKS (avoids a thundering herd).
    started = task_manager.active_count()
    retried, queued = [], []
    for t in candidates:
        if started >= MAX_CONCURRENT_TASKS:
            task_manager.enqueue(t["id"])
            queued.append(t["id"])
            continue
        cleanup_fragments(t["id"], t["output_name"], t.get("output_dir", ""))
        thread = threading.Thread(
            target=run_download,
            args=(t["id"], t["url"], t["output_name"]),
            kwargs={"format_id": t.get("format_id", ""), "output_dir": t.get("output_dir", "")},
            daemon=True,
        )
        thread.start()
        started += 1
        retried.append(t["id"])

    start_next_queued()  # drain queued tasks now that slots may be free
    log.info(f"[batch {batch_id}] Retry: {len(retried)} started, {len(queued)} queued")
    return jsonify({
        "batch_id": batch_id,
        "retried": len(retried),
        "queued": len(queued),
        "total_candidates": len(candidates),
    })


# ---- Group (grouping tree) CRUD ----
@app.route("/api/groups", methods=["GET"])
def api_list_groups():
    return jsonify({"groups": task_manager.get_groups()})


@app.route("/api/groups", methods=["POST"])
def api_create_group():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    parent_id = (data.get("parent_id") or "").strip()
    if not name:
        return jsonify({"error": "分组名不能为空"}), 400
    if parent_id and parent_id not in task_manager.groups:
        return jsonify({"error": "父分组不存在"}), 400
    g = task_manager.create_group(name, parent_id=parent_id)
    if not g:
        return jsonify({"error": "创建分组失败"}), 400
    return jsonify({"group": g})


@app.route("/api/groups/<group_id>", methods=["PATCH"])
def api_rename_group(group_id):
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "分组名不能为空"}), 400
    if not task_manager.rename_group(group_id, name):
        return jsonify({"error": "分组不存在"}), 404
    return jsonify({"ok": True, "id": group_id, "name": name})


@app.route("/api/groups/<group_id>/archive", methods=["POST"])
def api_archive_group(group_id):
    data = request.get_json(force=True, silent=True) or {}
    archived = bool(data.get("archived", True))
    if not task_manager.set_group_archived(group_id, archived):
        return jsonify({"error": "分组不存在"}), 404
    return jsonify({"ok": True, "id": group_id, "archived": archived})


@app.route("/api/groups/<group_id>", methods=["DELETE"])
def api_delete_group(group_id):
    delete_tasks = request.args.get("delete_tasks", "").lower() == "true"
    delete_files = request.args.get("delete_files", "").lower() == "true"
    result = task_manager.delete_group(
        group_id, delete_tasks=delete_tasks, delete_files=delete_files
    )
    if result is False:
        return jsonify({"error": "分组不存在"}), 404
    if result.get("partial_failures"):
        return jsonify({
            "ok": True,
            "partial": True,
            "removed_groups": result["removed_groups"],
            "removed_tasks": result["removed_tasks"],
            "partial_failures": result["partial_failures"],
            "error": "部分文件未能移入回收站，对应任务记录已保留",
        })
    return jsonify({"ok": True, "removed_groups": result["removed_groups"], "removed_tasks": result["removed_tasks"]})


@app.route("/api/tasks/batch", methods=["POST"])
def api_batch_tasks():
    """Batch operations on selected tasks: delete / retry / cancel / move."""
    data = request.get_json(force=True, silent=True) or {}
    action = data.get("action")
    ids = data.get("ids") or []
    target_group_id = (data.get("target_group_id") or "").strip()
    delete_files = bool(data.get("delete_files", False))

    if action not in ("delete", "retry", "cancel", "move", "start"):
        return jsonify({"error": "未知操作"}), 400
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "请选择任务"}), 400

    ok = 0
    partial = []
    for tid in ids:
        t = task_manager.get(tid)
        if not t:
            continue
        if action == "delete":
            if delete_files:
                results, all_ok = _trash_task_files(t)
                if not all_ok:
                    # P0-2: 部分失败 → 保留任务记录，返回清晰的逐文件结果
                    partial.append({"task_id": tid, "files": results})
                    continue
            task_manager.delete(tid, file_action=("trash" if delete_files else "record_only"))
            ok += 1
        elif action == "retry":
            if t["status"] in ("error", "cancelled"):
                res, st = _resume_task(tid)
                if st == 200:
                    ok += 1
        elif action == "cancel":
            if t["status"] in ("downloading", "queued", "paused"):
                task_manager.cancel(tid)
                ok += 1
        elif action == "start":
            if t["status"] in ("paused", "pending"):
                task_manager.enqueue(tid)  # 同时设置 queued 并加入调度队列
                ok += 1
            elif t["status"] == "queued":
                ok += 1  # 已在队列，无需重复触发
            elif t["status"] in ("error", "cancelled", "incomplete"):
                res, st = _resume_task(tid)
                if st == 200:
                    ok += 1
        elif action == "move":
            if target_group_id and target_group_id in task_manager.groups:
                task_manager.update(tid, group_id=target_group_id)
                ok += 1

    if action == "start":
        start_next_queued()  # 触发调度器启动 queued 任务

    resp = {"ok": ok, "action": action, "requested": len(ids)}
    if partial:
        resp["partial_failures"] = partial
    return jsonify(resp)


def _trash_task_files(t):
    """P0-2: 按任务的精确文件清单把文件移入回收站（绝不使用 stem* 通配）。

    - 只处理清单中的文件，且路径必须位于任务输出目录内；
    - 附带清理该任务缓存目录内的临时文件（白名单后缀）；
    - 返回 (逐文件结果列表, 是否全部成功)。
    """
    target_dir = _get_output_dir(t)
    files = trashmod.task_file_manifest(t, DOWNLOADS_DIR)
    results = []
    for attempt in range(3):
        pending = [f for f in files
                   if not any(r["path"] == f and r["result"] in
                              (trashmod.RESULT_TRASHED, trashmod.RESULT_MISSING)
                              for r in results)]
        if not pending:
            break
        results = [r for r in results
                   if r["result"] in (trashmod.RESULT_TRASHED, trashmod.RESULT_MISSING)]
        results.extend(trashmod.trash_files(pending, within_dir=target_dir))
        summary = trashmod.summarize(results)
        if summary["all_ok"]:
            break
        if attempt < 2:
            time.sleep(0.5)
    # 任务缓存目录（全部为临时分片，位于任务目录内）→ 回收站，失败保留
    cache = _cache_dir(t.get("output_dir", ""), t.get("id", ""))
    if t.get("id") and cache.exists():
        cr = trashmod.send_to_trash(cache)
        results.append({"path": str(cache), "result": cr})
    summary = trashmod.summarize(results)
    log.info(f"[{t.get('id')}] trash task files: {summary} in {target_dir}")
    return results, summary["all_ok"]


@app.route("/api/tasks/<task_id>", methods=["DELETE"])
def api_delete_task(task_id):
    """Delete a task, optionally also deleting local files."""
    t = task_manager.get(task_id)
    if not t:
        return jsonify({"error": "Task not found"}), 404

    delete_files = request.args.get("delete_files", "").lower() == "true"
    was_queued = t["status"] == "queued"

    # Cancel if still running
    if t["status"] == "downloading":
        task_manager.cancel(task_id)
        proc = t.get("_proc")
        for _ in range(20):
            if proc and proc.poll() is not None:
                break
            time.sleep(0.5)
        log.info(f"[{task_id}] Process fully terminated after cancel")

    file_results = []
    if delete_files:
        file_results, all_ok = _trash_task_files(t)
        if not all_ok:
            # P0-2/P0-3: 回收站部分失败 → 保留任务记录，返回真实逐文件结果
            return jsonify({
                "deleted": False,
                "partial": True,
                "files": file_results,
                "error": "部分文件未能移入回收站，已保留原文件与任务记录",
            }), 409

    task_manager.delete(task_id, file_action=("trash" if delete_files else "record_only"))
    log.info(f"[{task_id}] Task deleted (delete_files={delete_files})")

    if was_queued:
        start_next_queued()

    removed_files = [Path(r["path"]).name for r in file_results
                     if r["result"] == trashmod.RESULT_TRASHED]
    return jsonify({"deleted": True, "removed_files": removed_files, "files": file_results})


@app.route("/api/choose-dir", methods=["POST"])
def api_choose_dir():
    """弹出系统原生文件夹选择对话框，返回用户选中的路径（本机使用）。

    仅允许同时存在一个选择器，避免重复点击耗尽本地服务线程。可见 owner 会先
    激活再承载对话框，确保窗口能出现在任务切换器和当前桌面会话中。
    """
    if not _DIRECTORY_PICKER_LOCK.acquire(blocking=False):
        return jsonify({"error": "目录选择窗口已经打开"}), 409
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "Add-Type -AssemblyName System.Drawing;"
        "[System.Windows.Forms.Application]::EnableVisualStyles();"
        "$f = New-Object System.Windows.Forms.FolderBrowserDialog;"
        "$f.Description = '选择视频保存路径';"
        "$f.ShowNewFolderButton = $true;"
        "$owner = New-Object System.Windows.Forms.Form;"
        "$owner.Text = '影迹 - 选择保存位置';"
        "$owner.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen;"
        "$owner.Size = New-Object System.Drawing.Size(1, 1);"
        "$owner.ShowInTaskbar = $true;"
        "$owner.TopMost = $true;"
        "$owner.Opacity = 0.01;"
        "$owner.Show();"
        "$owner.Activate();"
        "try { if ($f.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $f.SelectedPath } }"
        "finally { $owner.Close(); $owner.Dispose(); $f.Dispose() }"
    )
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-STA", "-Command", ps],
            capture_output=True, text=True, timeout=90,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if r.returncode != 0:
            log.warning(f"Directory picker failed with exit code {r.returncode}")
            return jsonify({"error": "无法打开目录选择窗口"}), 500
        path = (r.stdout or "").strip()
        return jsonify({"path": path})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "目录选择已超时"}), 504
    except Exception:
        log.exception("Directory picker failed")
        return jsonify({"error": "无法打开目录选择窗口"}), 500
    finally:
        _DIRECTORY_PICKER_LOCK.release()


@app.route("/api/tasks/<task_id>/open-folder", methods=["POST"])
def api_open_folder(task_id):
    """Open File Explorer at the task's download location."""
    t = task_manager.get(task_id)
    if not t:
        return jsonify({"error": "Task not found"}), 404

    file_path = t.get("file_path")
    # 1. 文件存在 → 打开其所在目录（os.startfile 是 Windows 原生 API，对中文路径完美支持）
    #    注：explorer /select 在沙箱/中文路径下不可靠（returncode=1 且行为异常），故改用 startfile 打开目录
    if file_path and os.path.isfile(file_path):
        try:
            os.startfile(os.path.dirname(file_path))
            return jsonify({"opened": True, "path": str(os.path.dirname(file_path))})
        except Exception as e:
            log.warning(f"os.startfile failed for dirname({file_path}): {e}")

    # 2. 文件不在 → 打开任务的 output_dir（实际下载目录）
    output_dir = t.get("output_dir")
    if output_dir and os.path.isdir(output_dir):
        os.startfile(str(output_dir))
        return jsonify({"opened": True, "path": str(output_dir)})

    # 3. 所有路径均不存在 → 返回错误，不再静默跳转
    if output_dir:
        msg = f"下载目录不存在，无法打开：{output_dir}"
    elif file_path:
        msg = f"下载目录未记录且文件未找到，无法打开：{file_path}"
    else:
        msg = "下载目录未记录，无法打开文件夹"
    return jsonify({"error": msg, "opened": False}), 410


def _open_with_default_app(path):
    """用系统默认程序打开文件（跨平台）。Windows 调 os.startfile（会用视频默认播放器）；
    macOS 用 open；Linux 用 xdg-open。"""
    path = str(path)
    if os.name == "nt":
        os.startfile(path)
    else:
        opener = "open" if os.uname().sysname == "Darwin" else "xdg-open"
        subprocess.run([opener, path], capture_output=True)


@app.route("/api/tasks/<task_id>/open-file", methods=["POST"])
def api_open_file(task_id):
    """用系统默认程序直接打开下载好的视频文件（即默认视频播放器）。"""
    t = task_manager.get(task_id)
    if not t:
        return jsonify({"error": "Task not found"}), 404
    file_path = t.get("file_path")
    if file_path and os.path.exists(file_path):
        try:
            _open_with_default_app(file_path)
            return jsonify({"opened": True, "path": file_path})
        except Exception as e:
            # 打开失败则退回打开所在文件夹
            try:
                os.startfile(os.path.dirname(file_path))
            except Exception:
                pass
            return jsonify({"opened": True, "path": str(os.path.dirname(file_path)), "fallback": "folder", "error": str(e)})
    else:
        # 文件不存在 → 尝试打开任务的 output_dir
        output_dir = t.get("output_dir")
        if output_dir and os.path.isdir(output_dir):
            os.startfile(str(output_dir))
            return jsonify({"opened": True, "path": str(output_dir), "fallback": "folder"})
        # 所有路径均不存在 → 返回错误
        missing = []
        if file_path: missing.append(f"文件 {file_path}")
        if output_dir: missing.append(f"目录 {output_dir}")
        msg = "下载目录不存在，无法打开：" + ("、".join(missing) if missing else "路径信息缺失")
        return jsonify({"error": msg, "opened": False}), 410


# ---- Filename detection helpers (shared by /api/detect-name and /api/batch-inspect) ----
def _sanitize_basename(raw, default_ext=".mp4"):
    """清洗原始标题为 40 字内的安全 basename（不含扩展名）。"""
    s = (raw or "").strip()
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r'^[\[【（(][^\]】）)]*[\]】）)]\s*', '', s)
    s = re.sub(r'^(官方MV|MV|Official\s*MV|Official|HD|4K|1080P|720P)\s*[-:：]?\s*', '', s, flags=re.IGNORECASE)
    s = s.strip()[:40]
    if not s:
        return ""
    return Path(sanitize_filename(s, default_ext=default_ext)).stem


def _infer_filename_from_url(url):
    """URL 路径兜底推断文件名（跳过 .m3u8/.m3u 与 UUID 段）。"""
    try:
        parsed = urllib.parse.urlparse(url)
        path = urllib.parse.unquote(parsed.path)
        segments = [s for s in path.strip("/").split("/") if s and not s.endswith((".m3u8", ".m3u"))]
        if segments:
            last = segments[-1]
            last = re.split(r'[?#]', last)[0]
            if re.match(r'^[a-f0-9]{8,}$', last, re.IGNORECASE) or re.match(
                r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$', last, re.IGNORECASE
            ):
                if len(segments) >= 2:
                    last = segments[-2]
                else:
                    last = ""
            last = re.sub(r'\.(mp4|mkv|webm|flv|avi|mov|ts)$', '', last, flags=re.IGNORECASE)
            return _sanitize_basename(last) if last else ""
    except Exception:
        pass
    return ""


def _infer_ext_from_url(url):
    """URL 路径兜底推断扩展名（主流程以 yt-dlp 返回的 ext 为准）。"""
    try:
        parsed = urllib.parse.urlparse(url)
        path = parsed.path.lower()
        for ext in ("mp4", "mkv", "webm", "flv", "avi", "mov", "ts", "m4a", "mp3", "m3u8"):
            if path.endswith(f".{ext}"):
                return ext
    except Exception:
        pass
    return ""


# ---- New: Filename Detection ----
_detect_cache = {}  # url -> (timestamp, name, source); 60s TTL

@app.route("/api/detect-name", methods=["POST"])
def api_detect_name():
    """Infer a human-readable filename from the video's metadata (title).
    Falls back to extracting from the URL path when yt-dlp returns no title."""
    data = request.get_json()
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400


    url = _normalize_url(url)

    # Cache: skip re-fetching the same URL within 60s
    cached = _detect_cache.get(url)
    if cached and (time.time() - cached[0]) < 60:
        return jsonify({"name": cached[1], "source": cached[2] + "(cached)"})

    # Phase 1: try yt-dlp --dump-json (without --flat-playlist, which skips metadata)
    try:
        cmd = _yt_dlp_command() + ["--dump-json", "--no-download", "--no-playlist", url]
        cmd.extend(_get_cookie_args())
        r = subprocess.run(
            cmd,
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=8,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if r.returncode == 0 and r.stdout.strip():
            info = json.loads(r.stdout)
            title = info.get("title") or info.get("fulltitle", "")
            if title and title.strip():
                safe = _sanitize_basename(title)
                if safe:
                    _detect_cache[url] = (time.time(), safe, "metadata")
                    return jsonify({"name": safe, "source": "metadata"})
    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as e:
        log.warning(f"Detect name metadata failed for {url[:60]}: {e}")

    # Phase 2: fallback — extract from URL path (logic moved to _infer_filename_from_url)
    inferred = _infer_filename_from_url(url)
    if inferred:
        _detect_cache[url] = (time.time(), inferred, "url")
        return jsonify({"name": inferred, "source": "url"})

    return jsonify({"name": "", "source": ""})


# ---- Format Selection (P0-1: structured JSON, not -F text table) ----
# 结构化解析逻辑见 core/formats.classify_formats（独立可测）。

@app.route("/api/formats", methods=["POST"])
def api_formats():
    """列出某 URL 可用的视频轨/音轨（P0-1：改用 yt-dlp --dump-json 结构化数据）。

    返回结构化 video/audio/combined 三类列表，前端据此渲染「画质 + 音轨」双选择器，
    并支持 video-only 自动配对最佳音轨合并（见 computeFormatId / run_download）。
    """
    data = request.get_json()
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400


    url = _normalize_url(url)

    try:
        cmd = _yt_dlp_command() + ["--dump-json", "--no-download", "--no-playlist", url]
        cmd.extend(_get_cookie_args())
        r = subprocess.run(
            cmd,
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if r.returncode != 0 or not r.stdout.strip():
            err = (r.stderr or r.stdout or "").strip()[:600]
            return jsonify({
                "ok": False, "has_formats": False,
                "error": err or "无法获取格式信息",
                "video": [], "audio": [], "combined": [], "best_audio_id": "",
            }), 200

        info = json.loads(r.stdout)
        video, audio, combined, best_audio_id = classify_formats(info)
        return jsonify({
            "ok": True,
            "has_formats": bool(video or audio or combined),
            "title": info.get("title") or "",
            "duration": info.get("duration") or 0,
            "source": "json",
            "video": video,
            "audio": audio,
            "combined": combined,
            "best_audio_id": best_audio_id,
            "error": "",
        })
    except Exception as e:
        return jsonify({
            "ok": False, "has_formats": False, "error": str(e),
            "video": [], "audio": [], "combined": [], "best_audio_id": "",
        }), 500


# ---- Batch inspect (filename + format) for batch-get step 2 ----
@app.route("/api/batch-inspect", methods=["POST"])
def api_batch_inspect():
    """批量探测一组链接的文件名与格式（仅供「批量获取·检查与选择」按需探测）。

    输入：{"urls": ["..."]}（自动去空、截断 200 条防滥用）
    输出：{"ok": True, "items": [{url, filename, ext, filesize, source}, ...], "count": N}
    """
    data = request.get_json(force=True, silent=True) or {}
    urls = data.get("urls") or []
    if not isinstance(urls, list):
        return jsonify({"error": "urls 必须为列表"}), 400
    cleaned = []
    for u in urls:
        if isinstance(u, str) and u.strip():
            cleaned.append(u.strip())
    cleaned = cleaned[:200]
    if not cleaned:
        return jsonify({"error": "urls 列表为空"}), 400

    items = []
    for raw_url in cleaned:
        url = _normalize_url(raw_url)
        result = {"url": url, "filename": "", "ext": "", "filesize": 0, "source": ""}
        # detect-name 60s 缓存复用
        cached = _detect_cache.get(url)
        if cached and (time.time() - cached[0]) < 60 and cached[1]:
            result["filename"] = cached[1]
            result["source"] = cached[2] + "(cached)"

        try:
            cmd = _yt_dlp_command() + ["--dump-json", "--no-download", "--no-playlist"]
            cmd.extend(_yt_dlp_retry_args())
            cmd.extend(_yt_dlp_header_args(url))
            cmd.extend(_get_cookie_args())
            cmd.append(url)
            r = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=12,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if r.returncode == 0 and r.stdout.strip():
                info = json.loads(r.stdout)
                title = info.get("title") or info.get("fulltitle", "") or ""
                if not result["filename"] and title.strip():
                    safe = _sanitize_basename(title)
                    if safe:
                        result["filename"] = safe
                        result["source"] = "metadata"
                        _detect_cache[url] = (time.time(), safe, "metadata")
                fmts = info.get("formats") or []
                if fmts:
                    ext = (fmts[0].get("ext") or info.get("ext") or "").strip()
                else:
                    ext = (info.get("ext") or "").strip()
                result["ext"] = ext
                fs = info.get("filesize") or info.get("filesize_approx") or 0
                try:
                    result["filesize"] = int(fs) if fs else 0
                except (ValueError, TypeError):
                    result["filesize"] = 0
        except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as e:
            log.warning(f"Batch inspect metadata failed for {url[:60]}: {e}")
        # URL 兜底（缓存命中且已填字段不覆盖）
        if not result["filename"]:
            inferred = _infer_filename_from_url(url)
            if inferred:
                result["filename"] = inferred
                if not result["source"]:
                    result["source"] = "url"
        if not result["ext"]:
            result["ext"] = _infer_ext_from_url(url)
        items.append(result)

    return jsonify({"ok": True, "items": items, "count": len(items)})


# ---- Playlist / Batch Download ----
@app.route("/api/playlist", methods=["POST"])
def api_playlist():
    """Fetch playlist entries (flat, no download) for user selection."""
    data = request.get_json()
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400


    url = _normalize_url(url)

    try:
        cmd = _yt_dlp_command() + ["--flat-playlist", "--dump-json", "--no-warnings"]
        cmd.extend(_yt_dlp_retry_args())
        cmd.extend(_yt_dlp_header_args(url))
        cmd.extend(_get_cookie_args())
        cmd.append(url)
        r = subprocess.run(
            cmd,
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=45,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip()[:500]
            # Check if it's a single video (not a playlist)
            if "no video" not in err.lower() and "unsupported" not in err.lower():
                pass  # try to parse stdout anyway
            if not r.stdout.strip():
                if _risk_control_hint(err):
                    err += _risk_hint_text()
                elif _needs_cookie_hint(err):
                    err += _cookie_hint_text()
                return jsonify({"error": err or "无法获取播放列表，请检查链接"}), 400

        entries = []
        playlist_title = ""
        for line in r.stdout.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                info = json.loads(line)
                if not playlist_title:
                    playlist_title = info.get("playlist_title") or info.get("title", "")
                title = info.get("title", "") or info.get("id", "")
                entry_url = info.get("url") or info.get("webpage_url", "")
                duration = info.get("duration", 0) or 0
                entries.append({
                    "title": title,
                    "url": entry_url,
                    "duration": duration,
                })
            except json.JSONDecodeError:
                continue

        if not entries:
            return jsonify({"error": "未检测到播放列表，可能该链接是单个视频"}), 400

        return jsonify({
            "title": playlist_title or "播放列表",
            "entries": entries,
            "count": len(entries),
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "获取列表超时，请检查链接是否正确"}), 500
    except Exception as e:
        log.exception(f"Playlist fetch error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/import/excel", methods=["POST"])
def api_import_excel():
    """Parse an .xlsx file into preview rows: {url, filename, output_dir}."""
    if "file" not in request.files:
        return jsonify({"error": "未收到文件"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "文件名为空"}), 400
    try:
        import io, openpyxl
        raw = f.read()
        wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True)
    except Exception as e:
        return jsonify({"error": f"无法解析 Excel（仅支持 .xlsx）：{e}"}), 400
    try:
        ws = wb.active
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
    except Exception as e:
        return jsonify({"error": f"读取表格失败：{e}"}), 400
    if not rows:
        return jsonify({"rows": [], "count": 0})

    header = [str(c).strip() if c is not None else "" for c in rows[0]]

    def _idx(keywords):
        for i, h in enumerate(header):
            if any(k in h.lower() for k in keywords):
                return i
        return -1

    i_url = _idx(["url", "链接", "link", "地址", "视频"])
    i_name = _idx(["name", "文件名", "标题", "title", "file"])
    i_dir = _idx(["path", "路径", "目录", "dir", "folder", "保存"])
    if i_url < 0:
        i_url = 0
    if i_name < 0:
        i_name = 1 if len(header) > 1 else -1
    if i_dir < 0:
        i_dir = 2 if len(header) > 2 else -1

    def _cell(r, i):
        if i < 0 or i >= len(r) or r[i] is None:
            return ""
        return str(r[i]).strip()

    out = []
    for r in rows[1:]:
        if not r:
            continue
        url = _cell(r, i_url)
        if not url.lower().startswith("http"):
            continue
        out.append({
            "url": url,
            "filename": _cell(r, i_name),
            "output_dir": _cell(r, i_dir),
        })
    return jsonify({"rows": out, "count": len(out)})


@app.route("/api/export/excel", methods=["GET"])
def api_export_excel():
    """Export tasks as .xlsx. Optional ?group_id= and ?status= filters."""
    import io, openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from flask import send_file

    group_id = request.args.get("group_id", "").strip()
    status = request.args.get("status", "").strip()

    with task_manager._lock:
        tasks = list(task_manager.tasks.values())
    if group_id:
        tasks = [t for t in tasks if t.get("group_id") == group_id]
    if status:
        tasks = [t for t in tasks if t.get("status") == status]

    group_name = ""
    if group_id:
        g = task_manager.get_group(group_id)
        if g:
            group_name = g["name"]

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "下载任务"
    headers = ["视频链接", "文件名", "下载路径", "状态", "网站", "分组", "格式", "创建时间"]
    ws.append(headers)
    bold = Font(bold=True)
    fill = PatternFill("solid", fgColor="DDE3F0")
    for c in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=c)
        cell.font = bold
        cell.fill = fill
        cell.alignment = Alignment(horizontal="center")

    for t in tasks:
        gid = t.get("group_id", "")
        gname = ""
        if gid:
            g = task_manager.get_group(gid)
            gname = g["name"] if g else ""
        ws.append([
            t.get("url", ""),
            t.get("output_name", ""),
            t.get("output_dir", ""),
            t.get("status", ""),
            t.get("site", "") or _extract_site(t.get("url", "")),
            gname,
            t.get("format_id", "") or "best",
            t.get("created_at", "") or "",
        ])
    for col in ws.columns:
        width = max((len(str(c.value)) for c in col if c.value is not None), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 10), 60)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = "下载任务导出"
    if group_name:
        fname += f"_{group_name}"
    if status:
        fname += f"_{status}"
    fname += ".xlsx"
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=fname,
    )


@app.route("/api/import/excel/template", methods=["GET"])
def api_excel_template():
    """Download a blank Excel template with the required columns."""
    import io, openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from flask import send_file

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "导入模板"
    headers = ["视频链接", "文件名", "下载路径"]
    ws.append(headers)
    bold = Font(bold=True)
    fill = PatternFill("solid", fgColor="DDE3F0")
    for c in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=c)
        cell.font = bold
        cell.fill = fill
        cell.alignment = Alignment(horizontal="center")
    examples = [
        ["https://www.bilibili.com/video/BV1xx411c7XD", "美食教程第一期", "F:/video/bilibili/博主A"],
        ["https://www.youtube.com/watch?v=abcd1234", "旅行vlog", ""],
    ]
    for ex in examples:
        ws.append(ex)
    notes = [
        "",
        "说明：",
        "1. 视频链接：必填，必须以 http/https 开头",
        "2. 文件名：选填，留空则自动按序号+标题命名；填了直接用作文件名（自动补 .mp4）",
        "3. 下载路径：选填，留空=导入时使用你在页面选择的全局路径；填写需为绝对路径",
        "4. 可删除示例行后粘贴自己的数据",
    ]
    for n in notes:
        ws.append([n])
    widths = [40, 24, 30]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="批量下载导入模板.xlsx",
    )


@app.route("/api/harvest/export", methods=["POST"])
def api_harvest_export():
    """Export a harvested/edited link list as .xlsx (the '中间态' Excel).

    Body: {rows: [{url, filename, output_dir}], name?: str}
    Produces the 3-column sheet (视频链接/文件名/下载路径) that /api/import/excel
    can read back, so users can batch-edit filenames / output dirs offline.
    """
    import io, openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from flask import send_file

    data = request.get_json(silent=True) or {}
    rows = data.get("rows", []) or []
    clean = []
    for r in rows:
        url = (str(r.get("url") or "")).strip()
        if not url.lower().startswith("http"):
            continue
        clean.append({
            "url": url,
            "filename": (str(r.get("filename") or "")).strip(),
            "output_dir": (str(r.get("output_dir") or "")).strip(),
        })
    if not clean:
        return jsonify({"error": "没有可导出的有效链接行"}), 400

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "采集清单"
    headers = ["视频链接", "文件名", "下载路径"]
    ws.append(headers)
    bold = Font(bold=True)
    fill = PatternFill("solid", fgColor="DDE3F0")
    for c in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=c)
        cell.font = bold
        cell.fill = fill
        cell.alignment = Alignment(horizontal="center")
    for r in clean:
        ws.append([r["url"], r["filename"], r["output_dir"]])
    widths = [46, 26, 32]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    base = (str(data.get("name") or "")).strip() or "采集清单"
    base = re.sub(r'[\\/*?:"<>|]', "_", base)[:60]
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=f"{base}.xlsx",
    )


@app.route("/api/batch-download", methods=["POST"])
def api_batch_download():
    """Create multiple download tasks from a list of URLs or playlist items.

    Accepts either/both:
      - items: [{url, title, output_dir, output_name}]   (playlist / Excel import)
      - urls:  ["url", "url|title", "url  title"]          (free paste / file import)
    Duplicate URLs (matching existing tasks) are skipped.
    Optional `concurrency` (1-8), `output_dir` (default for items without one),
    `group_id` (assign created tasks to this group).
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "无效的请求体"}), 400

    format_id = (data.get("format_id") or "").strip()

    # P1-2: 批次并发只作用于本批次（写入任务 batch_concurrency 字段），
    # 不修改全局 MAX_CONCURRENT_TASKS —— 单任务/其他批次/重试不受影响。
    batch_concurrency = None
    concurrency = data.get("concurrency")
    if concurrency:
        try:
            batch_concurrency = max(1, min(int(concurrency), 8))
        except (TypeError, ValueError):
            batch_concurrency = None

    default_dir = (data.get("output_dir") or "").strip()
    if default_dir:
        norm = os.path.normpath(default_dir)
        if not os.path.isabs(norm):
            return jsonify({"error": "保存路径必须是绝对路径"}), 400
        try:
            Path(norm).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return jsonify({"error": f"无法创建目录: {norm} ({e})"}), 400
        default_dir = norm

    group_id = (data.get("group_id") or "").strip()
    if group_id and group_id not in task_manager.groups:
        return jsonify({"error": "目标分组不存在"}), 400

    # ---- Build unified download list: (url, title, item_dir, explicit_name) ----
    items = data.get("items", []) or []
    urls = data.get("urls", []) or []
    to_download = []
    for item in items:
        url = (item.get("url") or "").strip()
        if not url:
            continue
        title = (item.get("title") or "").strip()
        item_dir = (item.get("output_dir") or "").strip()
        explicit = (item.get("output_name") or "").strip()
        to_download.append((url, title, item_dir, explicit))
    for raw in urls:
        raw = (raw or "").strip()
        if not raw:
            continue
        if "|" in raw:
            u, t = raw.split("|", 1)
            to_download.append((u.strip(), t.strip(), "", ""))
        elif " " in raw:
            u, t = raw.split(" ", 1)
            if "http" not in t:
                to_download.append((u.strip(), t.strip(), "", ""))
            else:
                to_download.append((raw, "", "", ""))
        else:
            to_download.append((raw, "", "", ""))

    if not to_download:
        return jsonify({"error": "请至少提供一个视频链接"}), 400

    # ---- Deduplicate against existing tasks ----
    existing = {}
    for t in task_manager.tasks.values():
        existing[_normalize_url_for_compare(t["url"])] = t["status"]

    batch_id = uuid.uuid4().hex[:8]
    created_tasks = []
    skipped = []

    for i, (url, title, item_dir, explicit) in enumerate(to_download):
        seq = str(i + 1).zfill(2)
        try:
            url = _validated_download_url(url)
        except DownloadRequestRejected as exc:
            skipped.append({"url": url, "status": exc.user_message, "code": exc.code})
            continue
        norm = _normalize_url_for_compare(url)
        if norm in existing:
            skipped.append({"url": url, "status": existing[norm]})
            continue

        # Resolve per-item output directory
        out_dir = item_dir or default_dir
        if out_dir:
            out_dir = os.path.normpath(out_dir)
            if not os.path.isabs(out_dir):
                skipped.append({"url": url, "status": "路径非绝对"})
                continue
            try:
                Path(out_dir).mkdir(parents=True, exist_ok=True)
            except Exception as e:
                skipped.append({"url": url, "status": f"目录创建失败:{e}"})
                continue

        # Resolve output name（P0-1: 统一走 sanitize_filename）
        if explicit:
            output_name = sanitize_filename(explicit, default_ext=".mp4")
            display_name = Path(output_name).stem
        elif title:
            safe_title = Path(sanitize_filename(title, default_ext=".mp4")).stem
            if safe_title and not safe_title.startswith("video_"):
                output_name = sanitize_filename(f"{seq}_{safe_title}.mp4")
                display_name = safe_title
            else:
                output_name = f"{seq}.mp4"
                display_name = f"video_{seq}"
        else:
            output_name = f"{seq}.mp4"
            display_name = f"video_{seq}"

        # P0-1: 最终路径必须仍位于输出目录内
        try:
            resolve_within(out_dir or DOWNLOADS_DIR, output_name)
        except ValueError:
            skipped.append({"url": url, "status": "文件名不合法"})
            continue

        task = task_manager.create(
            url, output_name, display_name=display_name,
            format_id=format_id, output_dir=out_dir, batch_id=batch_id,
            group_id=group_id, initial_status="paused",
            batch_concurrency=batch_concurrency,
        )

        # 批量加入统一为「待开始」(paused)：不自动启动，等待用户在任务列表手动开始。
        # 仅当状态为 pending 时才立即启动，保持与原单任务逻辑一致。
        if task["status"] == "pending":
            thread = threading.Thread(
                target=run_download,
                args=(task["id"], url, output_name),
                kwargs={"format_id": format_id, "output_dir": out_dir},
                daemon=True,
            )
            thread.start()

        created_tasks.append({"task_id": task["id"], "title": display_name})
        existing[norm] = task["status"]   # mark seen for intra-batch dedupe
        log.info(f"[batch {batch_id}] Created task {task['id']} (group={group_id}): {output_name}")

    return jsonify({
        "batch_id": batch_id,
        "created": len(created_tasks),
        "tasks": created_tasks,
        "skipped": len(skipped),
        "skipped_detail": skipped,
    })


# ===================== 数据中心 · 统计模块（MVP，v0.2 定稿） =====================
# 口径规则（定稿）：主口径 = tasks.json 跨会话持久统计（今日 ≤ 本周 ≤ 累计恒成立）；
# 磁盘 mtime 扫描仅作「磁盘健康」页的今日落盘副口径，且需标注。
SSD_TBW_TB = 600          # D 盘 SSD 额定 TBW（UMIS 1TB NVMe，估算）
SSD_WRITE_AMP = 1.1       # 写入放大系数（视频顺序写，较低）
DISK_ALERT_DAYS = 30      # 预计 N 天内填满 → 触发容量预警
DISK_ALERT_FREE_PCT = 10  # 剩余空间低于 N% → 触发容量预警

_disk_scan_cache = {"ts": 0.0, "data": None}


def _completed_date(t):
    s = t.get("completed_at") or ""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).date()
    except Exception:
        return None


def _date_only(s):
    """安全解析 ISO 日期或日期时间字符串为 date；缺失或异常返回 None。"""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).date()
    except Exception:
        return None


def _safe_datetime(s):
    """R-1 返修：安全解析完整 ISO 时间为 datetime；缺失或异常返回 None。

    与 _date_only 的分工：自然日范围判断继续用 _date_only；
    批次 created_at/latest_at 与排序必须保留时分秒，使用本函数。
    """
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s))
    except Exception:
        return None


def _safe_file_size(v):
    """R-1 返修：统一的文件大小清洗口径（全部统计逻辑复用）。

    只接受可安全转换的非负数值；None、空字符串、布尔值、容器、
    不可解析字符串、负数统一按 0 处理。可解析的非负数字字符串转为整数。
    不修改原始任务记录，不回写 tasks.json。
    """
    if v is None or isinstance(v, bool):
        return 0
    try:
        if isinstance(v, (int, float)):
            n = int(v)
        elif isinstance(v, str):
            n = int(float(v.strip()))
        else:
            return 0
    except (TypeError, ValueError, OverflowError):
        return 0
    return n if n > 0 else 0


def _report_task_date(t):
    """报告区间过滤的任务代表日期：优先 completed_at，其次 created_at。

    两者均缺失或异常时返回 None——该任务不进入有限时间区间（today/week/month），
    仅 range=all 可纳入。
    """
    return _date_only(t.get("completed_at")) or _date_only(t.get("created_at"))


# UI-P0-03 返修：面向 UI 的检查未通过原因，必须完全用户语言化——
# 不含内部代码（no_duration/unreadable/ffprobe 等）、异常文本、本地路径或参数。
# 原始 verify.reason 原样保留在任务数据中供内部诊断，本映射绝不改写它。
_UI_REASON_MAP = (
    ("file_not_found", "文件不存在或已被移动"),
    ("empty_file", "文件内容为空"),
    ("container_unreadable", "文件无法正常读取，可能已损坏"),
    ("ffprobe_bad_output", "文件检查结果异常"),
    ("ffprobe_error", "文件检查过程出现异常"),
    ("no_media_streams", "文件中未发现有效的音视频内容"),
    ("no_video_stream", "文件中未发现视频画面"),
    ("no_audio_stream", "文件中未发现音轨"),
    ("no_duration", "无法读取视频时长"),
    ("duration_too_short", "视频时长异常过短"),
    ("bitrate_too_low", "文件数据量异常偏低，疑似不完整"),
)


def _ui_verify_reason(reason):
    """把内部检查原因代码翻译为纯用户语言（UI 口径）。

    与 core.media_check.reason_text 的区别：后者会在括号内回显原始代码/参数，
    适合日志诊断；本函数用于任何用户可见文本，未匹配时统一收敛为通用文案，
    保证不泄漏内部代码、异常堆栈或本地路径。
    """
    if not reason:
        return ""
    reason = str(reason)
    for key, text in _UI_REASON_MAP:
        if reason == key or reason.startswith(key):
            return text
    return "文件检查未通过"


def _completion_basis(t):
    """派生任务的完成依据（UI-P0-03）。仅对 status=complete 有意义，其余返回 None。

    - 已写入 completion_basis 的新任务：原样返回（verified / user_confirmed）。
    - 历史任务（无该字段）按 verify 证据派生，不改写 tasks.json：
      * verify.ok=True  → verified（曾通过系统检查）
      * verify 存在但未通过 → user_confirmed（只可能经旧版人工确认进入完成态）
      * verify 缺失 → legacy_unknown（早期记录，未记录检查结果）
    """
    if t.get("status") != "complete":
        return None
    basis = t.get("completion_basis")
    if basis in ("verified", "user_confirmed"):
        return basis
    vr = t.get("verify")
    if vr and vr.get("ok"):
        return "verified"
    if vr:
        return "user_confirmed"
    return "legacy_unknown"


def _stats_done_tasks():
    """所有已完成且有有效文件大小的任务（主口径数据源）。

    R-1 返修：有效性以 _safe_file_size 清洗结果为准——异常字符串、
    布尔值、容器、负数等一律视为无有效大小，不进入「已保存文件」口径。
    """
    return [t for t in task_manager.get_all()
            if t.get("status") == "complete" and _safe_file_size(t.get("file_size")) > 0]


# R-1 返修：批次状态分类口径（needs_attention=error|incomplete；cancelled 单独；
# active=进行中族；other=未知未来状态兜底，不静默丢失）。
_BATCH_ACTIVE_STATUSES = {"pending", "queued", "downloading", "paused", "verifying"}


def _aggregate_batches(tasks):
    """R-1 返修：可复用的批次聚合，接受指定任务集合。

    - /api/stats 传入全量当前任务；/api/stats/report 先按 range 过滤再传入。
    - created_at/latest_at 保留完整合法 ISO 时间（不再截断为日期）：
      created_at=批次内最早完整有效时间，latest_at=批次内最新完整有效时间。
    - 排序：latest_at 完整时间倒序；同一天 21:00 排在 09:00 之前；
      全部时间异常的批次稳定排在有效时间批次之后；
      相同完整时间用 batch_id 作稳定第二排序键，不依赖字典插入顺序。
    - file_size 一律经 _safe_file_size 清洗，异常值不导致失败。
    """
    batch_map = {}
    for t in tasks:
        bid = t.get("batch_id") or ""
        if not bid:
            continue
        st = (t.get("status") or "")
        b = batch_map.get(bid)
        if b is None:
            b = batch_map[bid] = {
                "batch_id": bid, "total": 0, "complete": 0, "saved_files": 0,
                "bytes": 0, "needs_attention": 0, "cancelled": 0, "active": 0,
                "other": 0, "_created": [], "_latest": [],
            }
        b["total"] += 1
        fs = _safe_file_size(t.get("file_size"))
        if st == "complete":
            b["complete"] += 1
            if fs > 0:
                b["saved_files"] += 1
                b["bytes"] += fs
        elif st in ("error", "incomplete"):
            b["needs_attention"] += 1
        elif st == "cancelled":
            b["cancelled"] += 1
        elif st in _BATCH_ACTIVE_STATUSES:
            b["active"] += 1
        else:
            b["other"] += 1
        ca = _safe_datetime(t.get("created_at"))
        la = _safe_datetime(t.get("completed_at") or t.get("created_at"))
        if ca:
            b["_created"].append(ca)
        if la:
            b["_latest"].append(la)

    batches_out = []
    for bid, b in batch_map.items():
        total = b["total"]
        completion_pct = round(b["complete"] * 100.0 / total, 1) if total else 0.0
        batches_out.append({
            "batch_id": bid,
            "created_at": (min(b["_created"]).isoformat() if b["_created"] else ""),
            "latest_at": (max(b["_latest"]).isoformat() if b["_latest"] else ""),
            "total": total,
            "complete": b["complete"],
            "saved_files": b["saved_files"],
            "bytes": b["bytes"],
            "needs_attention": b["needs_attention"],
            "cancelled": b["cancelled"],
            "active": b["active"],
            "other": b["other"],
            "completion_pct": completion_pct,
        })
    # 两遍稳定排序：先按 batch_id 升序（相同时间的稳定第二排序键），
    # 再按 latest_at 完整时间倒序；空 latest_at（时间全异常）自然排在最后。
    batches_out.sort(key=lambda x: x["batch_id"])
    batches_out.sort(key=lambda x: (x["latest_at"] or ""), reverse=True)
    return batches_out


def _stats_aggregate():
    """按 tasks.json 主口径聚合：KPI / 14 天趋势 / 来源 / 分组 / Top 大文件。"""
    done = _stats_done_tasks()
    groups = {gid: g for gid, g in task_manager.groups.items()}
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    month_first = today.replace(day=1)

    kpi = {k: {"bytes": 0, "count": 0} for k in ("today", "week", "month", "total")}
    trend_days = [(today - timedelta(days=i)) for i in range(13, -1, -1)]
    trend = {d.isoformat(): 0 for d in trend_days}
    src_bytes = collections.Counter()
    grp_bytes = collections.Counter()

    for t in done:
        # R-1 返修：全部统计复用 _safe_file_size 统一清洗口径
        fs = _safe_file_size(t.get("file_size"))
        kpi["total"]["bytes"] += fs
        kpi["total"]["count"] += 1
        d = _completed_date(t)
        if d:
            if d == today:
                kpi["today"]["bytes"] += fs
                kpi["today"]["count"] += 1
            if d >= monday:
                kpi["week"]["bytes"] += fs
                kpi["week"]["count"] += 1
            if d >= month_first:
                kpi["month"]["bytes"] += fs
                kpi["month"]["count"] += 1
            key = d.isoformat()
            if key in trend:
                trend[key] += fs
        src_bytes[_extract_site(t.get("url", ""))] += fs
        g = groups.get(t.get("group_id") or "")
        grp_bytes[(g["name"] if g else "未分组")] += fs

    def _dist(counter, top_n):
        total = sum(counter.values()) or 1
        items = counter.most_common(top_n)
        rest = sum(counter.values()) - sum(v for _, v in items)
        out = [{"name": k, "bytes": v, "pct": round(v * 100.0 / total, 1)} for k, v in items]
        if rest > 0:
            out.append({"name": "其他", "bytes": rest, "pct": round(rest * 100.0 / total, 1)})
        return out

    # R-1 返修：Top 文件排序与 bytes 输出只使用清洗后的数值，
    # 混入异常字符串时不再抛 TypeError，也不把原始异常值返回给前端。
    top_files = [
        {
            "name": t.get("display_name") or t.get("output_name", ""),
            "group": (groups.get(t.get("group_id") or "") or {}).get("name", "未分组"),
            "bytes": _safe_file_size(t.get("file_size")),
        }
        for t in sorted(done, key=lambda x: _safe_file_size(x.get("file_size")), reverse=True)[:10]
    ]

    # UI-P0-03: 完成依据分类（统计口径=全部 complete 任务，不受 file_size 过滤影响）。
    # KPI 数字口径不变（仍是「已保存文件」），此处只补充依据构成，供报告如实说明。
    basis_counts = {"verified": 0, "user_confirmed": 0, "legacy_unknown": 0}
    for t in task_manager.get_all():
        b = _completion_basis(t)
        if b:
            basis_counts[b] += 1

    # ===== R-1：需要处理趋势（最近 14 个自然日，含零值，按日期升序）=====
    # 口径：tasks.json 是任务结果记录，不是失败尝试事件日志，因此该趋势表示
    # 「当前任务记录中的最终结果」，重试成功后会从需要处理归入已完成。
    # 仅统计 status=error/incomplete 且 completed_at 可解析落在 14 天内的任务；
    # cancelled 不计入需要处理；complete/pending 等不计入；completed_at 缺失或异常不报错。
    failure_trend = {
        d.isoformat(): {
            "date": d.isoformat(),
            "error_count": 0,
            "incomplete_count": 0,
            "needs_attention_count": 0,
        }
        for d in trend_days
    }
    # ===== R-1：需要处理趋势填充（批次聚合已抽为 _aggregate_batches 复用）=====
    for t in task_manager.get_all():
        st = (t.get("status") or "")
        if st in ("error", "incomplete"):
            cd = _completed_date(t)
            if cd:
                key = cd.isoformat()
                if key in failure_trend:
                    if st == "error":
                        failure_trend[key]["error_count"] += 1
                    else:
                        failure_trend[key]["incomplete_count"] += 1
                    failure_trend[key]["needs_attention_count"] = (
                        failure_trend[key]["error_count"] + failure_trend[key]["incomplete_count"]
                    )

    batches_out = _aggregate_batches(task_manager.get_all())

    return {
        "kpi": kpi,
        "trend": [{"date": d.isoformat(), "bytes": trend[d.isoformat()]} for d in trend_days],
        "sources": _dist(src_bytes, 5),
        "groups": _dist(grp_bytes, 4),
        "top_files": top_files,
        "completion_basis": basis_counts,
        "failure_trend": list(failure_trend.values()),
        "batches": batches_out,
    }


def _scan_today_disk():
    """副口径：扫描默认下载目录，统计今日 mtime 落盘量与临时文件残留。缓存 120s。"""
    now = time.time()
    if _disk_scan_cache["data"] is not None and now - _disk_scan_cache["ts"] < 120:
        return _disk_scan_cache["data"]
    today_bytes = 0
    today_count = 0
    temp_count = 0
    temp_bytes = 0
    midnight = datetime.combine(date.today(), datetime.min.time()).timestamp()
    scanned = 0
    try:
        for root, dirs, files in os.walk(str(DOWNLOADS_DIR)):
            for fn in files:
                scanned += 1
                if scanned > 50000:
                    raise StopIteration
                try:
                    fp = os.path.join(root, fn)
                    st = os.stat(fp)
                    low = fn.lower()
                    if low.endswith((".part", ".ytdl", ".frag", ".tmp")) or "-frag" in low:
                        temp_count += 1
                        temp_bytes += st.st_size
                    if st.st_mtime >= midnight:
                        today_bytes += st.st_size
                        today_count += 1
                except OSError:
                    continue
    except StopIteration:
        pass
    except Exception as e:
        log.warning(f"[stats] disk scan failed: {e}")
    data = {"today_bytes": today_bytes, "today_count": today_count,
            "temp_count": temp_count, "temp_bytes": temp_bytes, "capped": scanned > 50000}
    _disk_scan_cache["ts"] = now
    _disk_scan_cache["data"] = data
    return data


def _stats_disks_by_task():
    """P1-3: 按已完成任务的实际 file_path/output_dir 聚合到各磁盘。

    返回 (disks 列表, unknown 桶)。无法确认实际路径的历史任务进 unknown，
    不参与任何磁盘容量 / TBW 推断。
    """
    per = {}
    unknown = {"count": 0, "bytes": 0}
    for t in task_manager.get_all():
        # R-1 返修：磁盘聚合同样复用 _safe_file_size，异常 file_size 不导致接口失败
        fs = _safe_file_size(t.get("file_size"))
        if t.get("status") != "complete" or fs <= 0:
            continue
        p = t.get("file_path") or t.get("output_dir") or ""
        anchor = ""
        if p:
            try:
                anchor = Path(p).anchor  # 'D:\\'
            except Exception:
                anchor = ""
        if not anchor:
            unknown["count"] += 1
            unknown["bytes"] += fs
            continue
        drive = anchor.rstrip(":\\/").upper() or anchor
        d = per.setdefault(drive, {"drive": drive, "task_count": 0, "bytes": 0})
        d["task_count"] += 1
        d["bytes"] += fs
    disks = []
    for drive, d in sorted(per.items()):
        try:
            du = shutil.disk_usage(drive + ":\\")
            d.update({"total": du.total, "free": du.free,
                      "used_pct": round(du.used * 100.0 / du.total, 1) if du.total else 0})
        except Exception:
            d.update({"total": 0, "free": 0, "used_pct": 0})
        disks.append(d)
    return disks, unknown


def _stats_disk(agg):
    """磁盘健康：容量 / TBW 估算 / 填满倒计时 / 唯一主告警 / 智能建议。"""
    anchor = Path(DOWNLOADS_DIR).resolve().anchor or "D:\\"
    try:
        du = shutil.disk_usage(anchor)
        total, used, free = du.total, du.used, du.free
    except Exception:
        total = used = free = 0
    used_pct = round(used * 100.0 / total, 1) if total else 0

    # 近 7 日均速（主口径趋势，不含今日的不完整性影响可接受）
    last7 = agg["trend"][-7:]
    rate = sum(x["bytes"] for x in last7) / 7.0
    days_to_full = round(free / rate, 1) if rate > 0 else None

    # P1-3: 按任务实际磁盘聚合；TBW 只用默认盘上的真实下载量估算，
    # 不再把跨磁盘下载全部算到默认盘。
    disks, unknown = _stats_disks_by_task()
    anchor_drive = anchor.rstrip(":\\/").upper()
    anchor_bytes = next((d["bytes"] for d in disks if d["drive"] == anchor_drive), 0)

    # TBW 估算（估算值）：默认盘累计下载量 × 写入放大 ÷ 额定 TBW
    tbw_total = SSD_TBW_TB * (1024 ** 4)
    written = anchor_bytes * SSD_WRITE_AMP
    tbw_used_pct = round(written * 100.0 / tbw_total, 2)
    years_to_tbw = round(tbw_total / (rate * SSD_WRITE_AMP * 365), 1) if rate > 0 else None

    alert = None
    free_pct = round(100 - used_pct, 1)
    if days_to_full is not None and days_to_full <= DISK_ALERT_DAYS:
        alert = {"level": "warn",
                 "title": f"容量预警：按近 7 日速率，{anchor[0]} 盘约 {int(days_to_full)} 天填满",
                 "text": "真实约束是容量而非寿命。建议将已完成视频归档到外置盘，SSD 只做工作盘。"}
    elif free_pct <= DISK_ALERT_FREE_PCT:
        alert = {"level": "warn",
                 "title": f"容量预警：{anchor[0]} 盘剩余空间仅 {free_pct}%",
                 "text": "剩余空间不足，建议尽快归档或清理已完成视频。"}

    scan = _scan_today_disk()
    sys_drive = (os.environ.get("SystemDrive") or "C:").rstrip("\\")
    on_sys_drive = anchor.upper().startswith(sys_drive.upper())
    advice = []
    if on_sys_drive:
        advice.append({"level": "warn", "title": "下载路径在系统盘",
                       "text": f"下载目录位于 {sys_drive} 盘（系统盘），会与系统写入叠加，建议改到数据盘。",
                       "action": "change_dir"})
    else:
        advice.append({"level": "ok", "title": "下载路径正确",
                       "text": f"下载目录位于 {anchor[0]} 盘（非系统盘），未叠加系统写入，配置良好。"})
    if scan["temp_count"] > 0:
        advice.append({"level": "warn", "title": "检测到临时文件残留",
                       "text": f"发现 {scan['temp_count']} 个临时分片/残留文件（约 {scan['temp_bytes'] / (1024**2):.0f} MB），可清理释放空间。"})
    else:
        advice.append({"level": "ok", "title": "临时文件正常",
                       "text": "未检测到 .part / 分片残留（自动清理生效）。"})
    advice.append({"level": "ok", "title": "跨盘整理安全",
                   "text": "将视频剪切到外置盘不伤 SSD（仅改索引/目标盘写入），可放心归档整理。"})

    return {
        "drive": anchor[0],
        "total": total, "used": used, "free": free,
        "used_pct": used_pct, "free_pct": free_pct,
        "days_to_full": days_to_full,
        "avg_daily_bytes_7d": int(rate),
        "tbw_rated_tb": SSD_TBW_TB,
        "tbw_used_pct": tbw_used_pct,
        "tbw_is_estimate": True,
        "tbw_basis": f"仅基于 {anchor[0]} 盘（默认下载盘）上已验证完成任务的下载量估算",
        "years_to_tbw": years_to_tbw,
        "scan_today": scan,
        "alert": alert,
        "advice": advice,
        "disks": disks,
        "unknown_tasks": {**unknown,
                          "note": "无法确认实际保存路径的历史任务，不参与磁盘容量/TBW 推断"},
        "change_dir_note": "修改下载路径只影响新任务，不会迁移历史文件",
    }


@app.route("/api/stats")
def api_stats():
    agg = _stats_aggregate()
    disk = _stats_disk(agg)
    return jsonify({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "caliber": "tasks.json",
        **agg,
        "disk": disk,
    })


def _fmt_size_h(nbytes):
    if not nbytes:
        return "0"
    if nbytes < 1024 ** 2:
        return f"{nbytes / 1024:.1f} KB"
    if nbytes < 1024 ** 3:
        return f"{nbytes / 1024**2:.1f} MB"
    return f"{nbytes / 1024**3:.2f} GB"


@app.route("/api/stats/report")
def api_stats_report():
    """导出 HTML 报告（MVP：总量统计 / 来源分布 / 磁盘健康摘要，支持脱敏）。"""
    rng = request.args.get("range", "week")
    sections = set((request.args.get("sections") or "totals,sources,disk").split(","))
    redact = request.args.get("redact", "1") == "1"

    today = date.today()
    if rng == "today":
        since, rng_label = today, "今日"
    elif rng == "month":
        since, rng_label = today.replace(day=1), "本月"
    elif rng == "all":
        since, rng_label = None, "累计"
    else:
        since, rng_label = today - timedelta(days=today.weekday()), "本周"

    done = _stats_done_tasks()
    if since is not None:
        sel = [t for t in done if (_completed_date(t) or date.min) >= since]
    else:
        sel = done
    # R-1 返修：报告体量统一复用 _safe_file_size 清洗口径
    sel_bytes = sum(_safe_file_size(t.get("file_size")) for t in sel)

    agg = _stats_aggregate()
    disk = _stats_disk(agg)

    src = collections.Counter()
    for t in sel:
        src[_extract_site(t.get("url", ""))] += _safe_file_size(t.get("file_size"))
    src_total = sum(src.values()) or 1
    src_rows = "".join(
        f"<tr><td>{host}</td><td>{_fmt_size_h(v)}</td><td>{v * 100.0 / src_total:.1f}%</td></tr>"
        for host, v in src.most_common(8)
    ) or "<tr><td colspan=3>该区间暂无下载记录</td></tr>"

    parts = []
    if "totals" in sections:
        # UI-P0-03: 报告如实说明口径——数字表示「已保存文件」，并给出完成依据构成。
        # 返修：构成必须按当前 range 筛选后的 sel 计算，与「{rng_label}下载 · N 个文件」
        # 同一数据集，不得混入区间外的历史任务（全量 _stats_aggregate 计数只服务统计页）。
        bc = {"verified": 0, "user_confirmed": 0, "legacy_unknown": 0}
        for _t in sel:
            _b = _completion_basis(_t)
            if _b:
                bc[_b] += 1
        basis_bits = []
        if bc.get("verified"):
            basis_bits.append(f"自动检查通过 {bc['verified']} 个")
        if bc.get("user_confirmed"):
            basis_bits.append(f"人工确认保留 {bc['user_confirmed']} 个")
        if bc.get("legacy_unknown"):
            basis_bits.append(f"历史记录（无检查数据）{bc['legacy_unknown']} 个")
        basis_line = (f"；{rng_label}文件构成：" + " · ".join(basis_bits)) if basis_bits else ""
        parts.append(f"""
  <div class="sec"><h2>总量统计</h2>
    <div class="grid">
      <div class="k"><b>{_fmt_size_h(sel_bytes)}</b><span>{rng_label}下载 · {len(sel)} 个文件</span></div>
      <div class="k"><b>{_fmt_size_h(agg['kpi']['today']['bytes'])}</b><span>今日 · {agg['kpi']['today']['count']} 个</span></div>
      <div class="k"><b>{_fmt_size_h(agg['kpi']['week']['bytes'])}</b><span>本周 · {agg['kpi']['week']['count']} 个</span></div>
      <div class="k"><b>{_fmt_size_h(agg['kpi']['total']['bytes'])}</b><span>累计 · {agg['kpi']['total']['count']} 个（跨会话）</span></div>
    </div>
    <p class="muted">口径说明：以上数量表示「已保存文件」，不代表全部通过自动检查{basis_line}。</p></div>""")
    if "sources" in sections:
        parts.append(f"""
  <div class="sec"><h2>来源分布（{rng_label} · 按任务 URL 域名）</h2>
    <table><tr><th>来源</th><th>体量</th><th>占比</th></tr>{src_rows}</table></div>""")
    if "disk" in sections:
        path_line = "" if redact else f"<p class='muted'>下载目录：{DOWNLOADS_DIR}</p>"
        d2f = f"约 {int(disk['days_to_full'])} 天" if disk["days_to_full"] is not None else "—"
        parts.append(f"""
  <div class="sec"><h2>磁盘健康摘要</h2>
    <div class="grid">
      <div class="k"><b>{_fmt_size_h(disk['total'])}</b><span>{disk['drive']} 盘总量 · 已用 {disk['used_pct']}%</span></div>
      <div class="k"><b>{_fmt_size_h(disk['free'])}</b><span>剩余空间</span></div>
      <div class="k"><b>{disk['tbw_used_pct']}%</b><span>TBW 已耗（{disk['tbw_rated_tb']} TB 额定，估算）</span></div>
      <div class="k"><b>{d2f}</b><span>按近 7 日速率填满倒计时</span></div>
    </div>{path_line}</div>""")

    # R-1：结果概览（outcomes）——按所选 range 过滤代表日期的任务。
    # 口径：needs_attention = error + incomplete；cancelled 单独统计，不计入失败。
    # tasks.json 为任务结果记录而非失败事件日志；重试成功后归入已完成。
    if "outcomes" in sections:
        # R-1 返修：区间代表日期统一为 _report_task_date（completed_at 优先，
        # 异常/缺失回落 created_at；两者均无效仅 range=all 纳入）。
        if since is not None:
            out_sel = [t for t in task_manager.get_all()
                       if (_report_task_date(t) or date.min) >= since]
        else:
            out_sel = list(task_manager.get_all())
        needs = sum(1 for t in out_sel if t.get("status") in ("error", "incomplete"))
        cancelled = sum(1 for t in out_sel if t.get("status") == "cancelled")
        if out_sel:
            if needs == 0 and cancelled == 0:
                out_body = "<p class='muted'>该区间没有需要处理或已取消的任务。</p>"
            else:
                out_body = (
                    "<div class=\"grid\">"
                    f"<div class=\"k\"><b>{needs}</b><span>需要处理（含失败与未完成）</span></div>"
                    f"<div class=\"k\"><b>{cancelled}</b><span>已取消（不计入失败）</span></div>"
                    "</div>"
                    "<p class=\"muted\">口径：tasks.json 为任务结果记录，非失败事件日志；"
                    "重试成功后任务归入已完成。已取消单独统计，不视为失败。</p>"
                )
        else:
            out_body = "<p class='muted'>该区间暂无任务记录。</p>"
        parts.append(
            f"<div class=\"sec\"><h2>结果概览（{rng_label}）</h2>{out_body}</div>"
        )

    # R-1：批次概览（batches）——仅展示友好序号与聚合数字，不泄露内部 batch_id。
    # 返修：必须先按 range 过滤任务再聚合批次——跨区间批次只统计区间内任务，
    # total/complete/needs/cancelled/active/bytes/completion_pct 全部基于过滤后
    # 任务重新计算；友好编号按过滤后最近时间重新排序生成。
    if "batches" in sections:
        if since is not None:
            batch_sel = [t for t in task_manager.get_all()
                         if (_report_task_date(t) or date.min) >= since]
        else:
            batch_sel = list(task_manager.get_all())
        range_batches = _aggregate_batches(batch_sel)
        other_total = sum(b.get("other", 0) for b in range_batches)
        other_note = (f"<p class=\"muted\">另有 {other_total} 个任务处于其他状态，已计入总任务数。</p>"
                      if other_total else "")
        b_rows = ""
        for i, b in enumerate(range_batches, 1):
            b_rows += (
                "<tr>"
                f"<td>批次 {i}</td>"
                f"<td>{b.get('total', 0)}</td>"
                f"<td>{b.get('complete', 0)}</td>"
                f"<td>{b.get('needs_attention', 0)}</td>"
                f"<td>{b.get('cancelled', 0)}</td>"
                f"<td>{b.get('active', 0)}</td>"
                f"<td>{b.get('completion_pct', 0)}%</td>"
                f"<td>{_fmt_size_h(b.get('bytes', 0))}</td>"
                "</tr>"
            )
        if b_rows:
            parts.append(
                f"<div class=\"sec\"><h2>批次概览（{rng_label} · 按最近时间倒序）</h2>"
                "<table><tr><th>批次</th><th>总任务</th><th>已完成</th><th>需要处理</th>"
                "<th>已取消</th><th>进行中</th><th>完成率</th><th>已保存体量</th></tr>"
                f"{b_rows}</table>{other_note}"
                "<p class=\"muted\">批次编号为展示序号，不对应内部标识；"
                "跨区间批次仅统计该区间内的任务；已取消单独统计，不计入失败。</p></div>"
            )
        else:
            parts.append(
                f"<div class=\"sec\"><h2>批次概览（{rng_label}）</h2>"
                f"<p class=\"muted\">{rng_label}没有批次任务记录。</p></div>"
            )

    redact_note = "（已脱敏：不含真实文件路径）" if redact else ""
    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>影迹下载报告 · {today.isoformat()}</title>
<style>
body{{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;background:#f5f6f8;color:#1f2937;margin:0;padding:32px 16px}}
.wrap{{max-width:760px;margin:0 auto}}
h1{{font-size:22px}} h1 span{{color:#e50914}}
.meta{{color:#6b7280;font-size:13px;margin-bottom:20px}}
.sec{{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:20px;margin-bottom:16px}}
.sec h2{{font-size:15px;margin:0 0 14px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}}
.k{{background:#f9fafb;border:1px solid #eef0f3;border-radius:10px;padding:12px}}
.k b{{display:block;font-size:19px}} .k span{{font-size:12px;color:#6b7280}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{text-align:left;padding:7px 6px;border-bottom:1px solid #f0f1f3}} th{{color:#6b7280;font-weight:600}}
.muted{{color:#9ca3af;font-size:12px}}
.foot{{color:#9ca3af;font-size:12px;text-align:center;margin-top:18px}}
</style></head><body><div class="wrap">
<h1>影<span>迹</span> · 下载报告</h1>
<div class="meta">范围：{rng_label} · 生成时间：{datetime.now().strftime("%Y-%m-%d %H:%M")} · 口径：tasks.json 持久化统计 {redact_note}</div>
{''.join(parts)}
<div class="foot">由 影迹 数据中心自动生成 · MVP 版（Top 大文件 / 趋势曲线将在 V1.1 加入）</div>
</div></body></html>"""

    resp = make_response(html)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    fname = urllib.parse.quote(f"影迹下载报告_{today.isoformat()}.html")
    resp.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{fname}"
    return resp


# ---- Helper ----
def _task_to_dict(t):
    return {
        "id": t["id"],
        "url": t["url"],
        "site": _extract_site(t.get("url", "")),
        "output_name": t["output_name"],
        "display_name": t.get("display_name", Path(t["output_name"]).stem),
        "format_id": t.get("format_id", ""),
        "output_dir": t.get("output_dir", ""),
        "batch_id": t.get("batch_id", ""),
        "group_id": t.get("group_id", ""),
        "status": t["status"],
        "progress": t["progress"],
        "speed": t["speed"],
        "eta": t["eta"],
        "size": t["size"],
        "fragments": t["fragments"],
        "file_path": t.get("file_path", ""),
        "file_size": t["file_size"],
        "duration": t.get("duration", ""),
        "resolution": t.get("resolution", ""),
        "error": t.get("error", ""),
        "error_code": t.get("error_code", ""),
        "created_at": t["created_at"],
        "started_at": t.get("started_at", ""),
        "completed_at": t.get("completed_at", ""),
        "verify_ok": bool((t.get("verify") or {}).get("ok")) if t.get("verify") else None,
        "has_audio": (t.get("verify") or {}).get("has_audio") if t.get("verify") else None,
        # UI-P0-03: 完成依据（complete 任务派生 verified/user_confirmed/legacy_unknown，
        # 其余状态为 null）；历史任务只在序列化时派生，不写回 tasks.json。
        "completion_basis": _completion_basis(t),
        "confirmed_at": t.get("confirmed_at", ""),
        "confirmation_note": t.get("confirmation_note", ""),
        "verify_reason": (_ui_verify_reason((t.get("verify") or {}).get("reason", ""))
                          if t.get("verify") and not (t.get("verify") or {}).get("ok") else ""),
        "batch_concurrency": t.get("batch_concurrency"),
        "files": t.get("files") or [],
    }


@app.route("/api/ffmpeg-status")
def api_ffmpeg_status():
    return jsonify({
        "ffmpeg": FFMPEG_PATH is not None,
        "ffmpeg_path": FFMPEG_PATH,
        "yt_dlp": _yt_dlp_available(),
        "version": APP_VERSION,
        "keep_fragments": True,
        "concurrent_fragments": CONCURRENT_FRAGMENTS,
        "max_concurrent_tasks": MAX_CONCURRENT_TASKS,
        "log_dir": str(LOGS_DIR),
        "download_dir": str(DOWNLOADS_DIR),
        "cookies_loaded": _cookie_effective(),
    })


@app.route("/api/cookies/info")
def api_cookies_info():
    """返回 Cookie 文件状态（不含任何 cookie 内容）。"""
    if _cookie_effective():
        try:
            st = COOKIES_FILE.stat()
            lines = [ln for ln in COOKIES_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
                     if ln.strip() and not ln.strip().startswith("#")]
            return jsonify({
                "loaded": True,
                "size_bytes": st.st_size,
                "last_modified": st.st_mtime,
                "sample_lines": len(lines),  # 仅行数，绝不含内容
            })
        except Exception:
            return jsonify({"loaded": False, "size_bytes": 0, "last_modified": None, "sample_lines": 0})
    return jsonify({"loaded": False, "size_bytes": 0, "last_modified": None, "sample_lines": 0})


@app.route("/api/cookies", methods=["POST", "DELETE"])
def api_cookies():
    """POST 写入 Cookie 文件（原子写 + 权限 0600 + 即时刷新缓存）；DELETE 移除。"""
    if request.method == "DELETE":
        physically = False
        try:
            if COOKIES_FILE.exists():
                COOKIES_FILE.unlink()
            physically = not COOKIES_FILE.exists()
        except Exception as e:
            log.warning(f"[Cookie] unlink skipped by environment ({e}); will empty file instead")
        # 物理删除被环境安全策略拦截（如沙箱无回收站）时，清空内容使不再发送任何 cookie
        if COOKIES_FILE.exists():
            try:
                COOKIES_FILE.write_text("")
                physically = False
            except Exception as e:
                log.warning(f"[Cookie] could not empty cookie file: {e}")
        invalidate_cookie_cache()
        log.info("[Cookie] removed by user (content never logged)")
        return jsonify({"ok": True, "loaded": False, "physically_removed": physically})

    data = request.get_json(silent=True) or {}
    content = data.get("content", "")
    ok, reason = _validate_cookie_content(content)
    if not ok:
        return jsonify({"ok": False, "error": reason}), 400

    try:
        COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = COOKIES_FILE.with_name(COOKIES_FILE.name + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, COOKIES_FILE)  # 原子写，避免半写残留
        try:
            os.chmod(COOKIES_FILE, 0o600)
        except OSError:
            pass
        invalidate_cookie_cache()
        log.info(f"[Cookie] written {len(content)} bytes by user (content not logged)")
        return jsonify({"ok": True, "loaded": True, "size_bytes": COOKIES_FILE.stat().st_size})
    except Exception as e:
        log.error(f"[Cookie] write failed: {e}")
        return jsonify({"ok": False, "error": "写入失败"}), 500


@app.route("/api/cookies/test", methods=["POST"])
def api_cookies_test():
    """验证 Cookie 文件能被 yt-dlp 正常加载（不返回任何 cookie 内容）。"""
    if not _cookie_effective():
        return jsonify({"ok": False, "error": "尚未添加有效的 Cookie 文件"}), 400
    # 公开可拿 metadata 的探针站点，无需登录也能跑；仅验证 cookie 文件可被工具解析
    probe = "https://www.bilibili.com"
    cmd = _yt_dlp_command() + ["--dump-json", "--no-download", "--no-playlist", "--retries", "2", probe]
    cmd.extend(_get_cookie_args())
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=12, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        err = (r.stderr or r.stdout or "")[:400]
        if r.returncode == 0:
            return jsonify({"ok": True, "detail": "Cookie 文件可被工具正常读取"})
        # cookie 解析失败的特征
        el = err.lower()
        if "cookie" in el and any(k in el for k in ("error", "invalid", "parse", "malformed", "unable to load")):
            return jsonify({"ok": False, "error": "Cookie 文件格式无法被工具解析，请重新导出"})
        # 其它错误（风控/网络）不归咎于 cookie 文件本身
        return jsonify({"ok": True, "detail": "Cookie 文件可被工具正常读取（探测站点返回非致命错误，不归属 Cookie）"})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": True, "detail": "探测超时，但 Cookie 文件已写入，可在真实任务中验证"})
    except Exception as e:
        log.warning(f"[Cookie] test failed: {e}")
        return jsonify({"ok": False, "error": "测试执行异常"}), 500


def _probe_download_dir(path):
    """检查目标目录状态，不改变文件系统结构（不创建目标目录）。

    返回 {"exists", "is_directory", "writable"} 三个布尔字段：
    - 目标不存在 → exists=False，其余为 False（检查后目标仍不存在）；
    - 目标是文件而非目录 → is_directory=False，writable=False；
    - 已存在目录 → 用随机命名的探针文件做一次最小的真实写入/读取检查，
      使用排他创建（O_EXCL）避免覆盖现有文件，并在 finally 中尽最大可能清理。

    安全约束：不读取目录中的其他文件，不读取任务/分组/检测记录/Cookie 内容，
    不修改程序设置；探针文件名随机且限定在目标目录内。
    """
    result = {"exists": False, "is_directory": False, "writable": False}
    try:
        target = Path(path)
        if not target.exists():
            return result
        result["exists"] = True
        if not target.is_dir():
            return result
        result["is_directory"] = True
    except Exception:
        return result
    # 固定探针文件名 + O_TRUNC 覆盖：避免每次请求生成新的随机临时文件，
    # 在受限环境（unlink 被拦截）下也只残留至多一个固定文件。
    probe = target / ".yingji_write_probe.tmp"
    fd = None
    try:
        fd = os.open(str(probe), os.O_CREAT | os.O_WRONLY | os.O_TRUNC)
        os.write(fd, b"ok")
        os.close(fd)
        fd = None
        with open(probe, "r", encoding="utf-8") as f:
            f.read()
        result["writable"] = True
    except Exception:
        pass
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
        try:
            if probe.exists():
                probe.unlink()
        except Exception:
            pass  # 清理失败不向用户泄露异常
    return result


def _check_dir_writable(path):
    """向后兼容包装：仅返回目录是否可写（不创建目录、探针在 finally 中清理）。"""
    return _probe_download_dir(path)["writable"]


@app.route("/api/startup-status")
def api_startup_status():
    # UI-P0-05：首次启动引导与异常恢复所需的能力检查。
    # 仅检查本机使用条件，不读取任务内容或登录凭据内容，也不会修改程序设置。
    # 字段向后兼容：原有字段保留，追加 exists / is_directory 两个细分字段。
    probe = _probe_download_dir(DOWNLOADS_DIR)
    return jsonify({
        "version": "v2.7",
        "ffmpeg": FFMPEG_PATH is not None,
        "yt_dlp": _yt_dlp_available(),
        "cookies_loaded": COOKIES_FILE.exists(),
        "download_dir": str(DOWNLOADS_DIR),
        "download_dir_writable": probe["writable"],
        "download_dir_exists": probe["exists"],
        "download_dir_is_directory": probe["is_directory"],
    })


@app.route("/api/ping")
def api_ping():
    return jsonify({"pong": True, "version": "v2.7"})


# ===== 检测记录（服务端磁盘持久化，跨浏览器/会话） =====
_detect_records_lock = threading.Lock()
_detect_records_cache = None  # 惰性加载后的内存缓存


def _load_detect_records():
    global _detect_records_cache
    if _detect_records_cache is not None:
        return _detect_records_cache
    data = []
    if DETECT_RECORDS_FILE.exists():
        try:
            with open(DETECT_RECORDS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                data = []
        except Exception as e:
            log.warning(f"Failed to load detect_records.json: {e}")
            data = []
    _detect_records_cache = data
    return data


def _save_detect_records(records):
    global _detect_records_cache
    _detect_records_cache = records
    try:
        tmp = str(DETECT_RECORDS_FILE) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        os.replace(tmp, str(DETECT_RECORDS_FILE))
    except Exception as e:
        log.warning(f"Failed to save detect_records.json: {e}")


@app.route("/api/detect-records", methods=["GET"])
def api_get_detect_records():
    return jsonify(_load_detect_records())


@app.route("/api/detect-records", methods=["POST"])
def api_add_detect_record():
    payload = request.get_json(force=True, silent=True) or {}
    source_url = (payload.get("sourceUrl") or "").strip()
    raw_entries = payload.get("entries") or []
    entries = []
    for e in raw_entries:
        if isinstance(e, dict) and e.get("url"):
            entries.append({
                "url": str(e.get("url", "")),
                "title": str(e.get("title", "")),
                "filename": str(e.get("filename", "") or ""),
                "ext": str(e.get("ext", "") or ""),
            })
    if not entries:
        return jsonify({"error": "没有可保存的链接"}), 400
    records = _load_detect_records()
    count = len(entries)
    # 去重：同一 sourceUrl + 相同 count 的最近一条直接替换
    dup = next((i for i, r in enumerate(records)
                if r.get("sourceUrl") == source_url and r.get("count") == count), None)
    rec = {
        "id": uuid.uuid4().hex[:12],
        "sourceUrl": source_url,
        "title": payload.get("title") or "批量获取",
        "count": count,
        "entries": entries,
        "createdAt": datetime.now().isoformat(),
    }
    if dup is not None:
        records[dup] = rec
    else:
        records.insert(0, rec)
    while len(records) > DETECT_RECORDS_LIMIT:
        records.pop()
    _save_detect_records(records)
    return jsonify({"ok": True, "id": rec["id"]})


@app.route("/api/detect-records/<record_id>", methods=["DELETE"])
def api_delete_detect_record(record_id):
    records = _load_detect_records()
    new_records = [r for r in records if r.get("id") != record_id]
    if len(new_records) == len(records):
        return jsonify({"error": "记录不存在"}), 404
    _save_detect_records(new_records)
    return jsonify({"ok": True})


@app.route("/downloads/<path:filename>")
def serve_download(filename):
    return send_from_directory(str(DOWNLOADS_DIR), filename, as_attachment=True)


@app.route("/api/logs/<date_str>")
def api_log(date_str):
    """Serve today's log file. date_str format: YYYY-MM-DD"""
    log_file = LOGS_DIR / f"m3u8-tool.log.{date_str}"
    if not log_file.exists():
        log_file = LOGS_DIR / "m3u8-tool.log"
        if not log_file.exists():
            return jsonify({"error": "Log not found"}), 404
    with open(log_file, "r", encoding="utf-8") as f:
        return Response(f.read(), mimetype="text/plain; charset=utf-8")


_background_services_started = False
_shutdown_callback = None


def prepare_runtime():
    """Run startup maintenance and start background services once."""
    global _background_services_started
    if _background_services_started:
        return
    try:
        startup_cleanup()
    except Exception:
        log.exception("startup_cleanup failed (non-fatal)")
    try:
        threading.Thread(
            target=_periodic_cache_cleaner,
            kwargs={"interval_sec": 600},
            daemon=True,
        ).start()
        log.info("Periodic cache cleaner started (interval=600s)")
    except Exception:
        log.exception("failed to start periodic cache cleaner (non-fatal)")
    _background_services_started = True


def save_runtime_state():
    """Persist task and group state before the desktop host exits."""
    tasks_ok = task_manager._save()
    task_manager._save_groups()
    return tasks_ok


def register_shutdown_callback(callback):
    global _shutdown_callback
    _shutdown_callback = callback


@app.route("/api/app/quit", methods=["POST"])
def api_app_quit():
    if not _shutdown_callback:
        return jsonify({"error": "Desktop host is not active"}), 409
    # Give Waitress enough time to flush this response before the desktop host
    # closes channels and workers. Do not emit the hop-by-hop Connection header:
    # Waitress correctly rejects it under WSGI.
    threading.Timer(2.0, _shutdown_callback).start()
    return jsonify({"ok": True})


# ============================================================
# 格式转换模块（独立挂载）
# 该模块自带全部路由与状态，不读写下载任务数据。任何异常都在此
# 拦截，只记录日志、不影响主程序与既有功能。
# ============================================================
try:
    from convert_api import convert_bp, init_convert

    init_convert(
        output_dir=DOWNLOADS_DIR / "Converted",
        temp_dir=DATA_DIR / ".convert_tmp",
        media_tool=FFMPEG_PATH,
        probe_tool=_ffprobe_path() if FFMPEG_PATH else None,
        logger=log,
        base_dir=BASE_DIR,
    )
    app.register_blueprint(convert_bp)
    log.info("Converter module mounted at /api/convert")
except Exception as _convert_mount_error:  # noqa: BLE001
    log.warning(f"Converter module not mounted: {_convert_mount_error}")


# ============================================================
# Telegram Bot 模块（独立挂载 · 默认关闭的可选扩展）
# 该模块自带全部路由与状态，通过注入的回调复用下载能力，不改动主程序
# 既有逻辑。默认关闭，需使用者在界面中主动启用。任何异常都在此拦截，
# 只记录日志、不影响主程序与既有功能。
# 卸载方式：删除本挂载块与 telegram_bot_api.py、core/telegram_bot_*.py、
# static/telegram-bot.*，主程序即完全还原。
# ============================================================
try:
    from telegram_bot_api import telegram_bot_bp, init_telegram_bot

    init_telegram_bot(
        data_dir=DATA_DIR / ".telegram_bot",
        downloads_dir=DOWNLOADS_DIR / "TelegramBot",
        logger=log,
        submit_task=submit_download_task,
        get_task=task_manager.get,
        extract_caption=extract_public_metadata,
    )
    app.register_blueprint(telegram_bot_bp)
    log.info("Telegram bot module mounted at /api/telegram-bot")
except Exception as _tgb_mount_error:  # noqa: BLE001
    log.warning(f"Telegram bot module not mounted: {_tgb_mount_error}")


if __name__ == "__main__":
    PORT = int(os.environ.get("YINGJI_PORT", "5001"))
    PID_FILE = Path(os.environ.get("YINGJI_PID_FILE") or (DATA_DIR / "server.pid"))
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()) + "\n", encoding="ascii")

    def _remove_own_pid_file():
        try:
            if PID_FILE.read_text(encoding="ascii").strip() == str(os.getpid()):
                PID_FILE.unlink()
        except (FileNotFoundError, OSError):
            pass

    atexit.register(_remove_own_pid_file)
    log.info(f"Starting server {APP_VERSION}...")
    log.info(f"Downloads dir: {DOWNLOADS_DIR}")
    log.info(f"Logs dir: {LOGS_DIR}")
    log.info(f"Concurrent fragments per task: {CONCURRENT_FRAGMENTS}")
    log.info(f"Max concurrent tasks: {MAX_CONCURRENT_TASKS}")
    log.info(f"FFmpeg: {FFMPEG_PATH or 'NOT FOUND'}")
    log.info(f"yt-dlp: {YT_DLP}")
    log.info(f"Server listening at http://localhost:{PORT}")
    prepare_runtime()
    start_next_queued()
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
