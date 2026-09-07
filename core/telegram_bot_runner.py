"""Telegram Bot 扩展模块 · Bot 本体。

弱绑定约束（务必保持）：
- 不 import server / flask，主程序能力全部通过注入的函数使用：
    submit_task(url, output_name, format_id, output_dir) -> task dict
    get_task(task_id) -> task dict or None
- 线程模型：python-telegram-bot 是异步框架，Flask 是同步框架。
  Bot 运行在专用线程 + 自建 event loop 中，用 asyncio.Event 控制停止，
  不使用 Application.run_polling()（它自管 loop，无法优雅停止）。

回传策略（融合方案）：
- 文件 <= max_direct_send_mb（默认 50MB）：走官方通道直发。
- 文件 > 50MB：走本地加速服务（local_api_url）直发，上限 local_max_send_mb。
- 本地加速服务不可用：仅提示文件已保存在影迹，不向聊天泄露本机绝对路径。
"""

from __future__ import annotations

import asyncio
import collections
import json
import os
import re
import threading
import time
import urllib.parse
from pathlib import Path

from telegram import Bot
from telegram.constants import ChatAction
from telegram.ext import Application, MessageHandler, CommandHandler, filters
from telegram.request import HTTPXRequest

from .telegram_bot_store import TelegramBotStore

URL_RE = re.compile(r"https?://[^\s<>\[\]()\"']+", re.IGNORECASE)
SENSITIVE_QUERY_KEYS = {
    "access_token", "api_key", "auth", "authorization", "credential",
    "expires", "key", "policy", "signature", "sig", "token",
}


def _safe_public_url(url: str) -> str:
    """Keep useful public query fields while dropping credentials/signatures."""
    try:
        parsed = urllib.parse.urlsplit(str(url or ""))
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        safe_query = [
            (key, value) for key, value in query
            if key.lower() not in SENSITIVE_QUERY_KEYS
            and not key.lower().startswith(("x-amz-", "x-goog-"))
        ]
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urllib.parse.urlunsplit((
            parsed.scheme, host, parsed.path,
            urllib.parse.urlencode(safe_query, doseq=True), "",
        ))
    except (TypeError, ValueError):
        return ""


VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".m4v", ".ts", ".wmv"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".wav", ".opus"}

# 本地加速服务可用性探测缓存时长（秒）
LOCAL_PROBE_TTL = 60
# 任务完成轮询间隔（秒）
WATCH_INTERVAL = 3.0
# 大文件上传超时（秒）：2GB 在普通上行带宽下需 6-10 分钟
BIG_UPLOAD_TIMEOUT = 900
# 常规请求超时（秒）
NORMAL_TIMEOUT = 120
# 每个允许名单用户最多每分钟提交 6 次，同时最多保留 2 个未完成任务。
SUBMISSION_WINDOW_SECONDS = 60
MAX_SUBMISSIONS_PER_WINDOW = 6
MAX_PENDING_PER_CHAT = 2


class TelegramBotRunner:
    """Bot 生命周期与消息处理。"""

    def __init__(self, store: TelegramBotStore, submit_task, get_task, logger=None,
                 extract_caption=None):
        self.store = store
        self._submit_task = submit_task
        self._get_task = get_task
        self.logger = logger
        self._extract_caption = extract_caption

        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_ready = threading.Event()
        self._async_stop: asyncio.Event | None = None
        self._stop_evt = threading.Event()
        self._lock = threading.RLock()

        self.app = None            # 官方通道 Application（收消息 + 小文件发送）
        self.local_bot: Bot | None = None  # 本地加速服务 Bot（仅发送大文件）
        self._local_ok = False
        self._local_checked_at = 0.0
        self._me = None            # get_me() 结果缓存
        self._last_error = ""
        self._pending = 0          # 当前跟踪中的任务数
        self._submission_lock = threading.Lock()
        self._recent_submissions = collections.defaultdict(collections.deque)
        self._submissions_in_flight = collections.Counter()

        # 翻译器缓存：配置指纹变化时重建，避免每次翻译都 new（翻译调用不频繁）
        self._translator = None
        self._translator_fp = ""

    # ---------- 内部工具 ----------

    def _log(self, msg: str, level: str = "info") -> None:
        if self.logger:
            safe = self.store.redact_text(msg)
            getattr(self.logger, level, self.logger.info)(f"[telegram-bot] {safe}")

    def _reserve_submission(self, chat_id) -> tuple[bool, str]:
        """Apply a per-chat rate limit and pending-task quota."""
        key = str(chat_id)
        now = time.monotonic()
        with self._submission_lock:
            recent = self._recent_submissions[key]
            cutoff = now - SUBMISSION_WINDOW_SECONDS
            while recent and recent[0] <= cutoff:
                recent.popleft()
            pending = sum(
                1 for item in self.store.all_mappings().values()
                if isinstance(item, dict) and str(item.get("chat_id")) == key
            ) + self._submissions_in_flight[key]
            if pending >= MAX_PENDING_PER_CHAT:
                return False, "你已有两个任务等待处理，请完成后再提交。"
            if len(recent) >= MAX_SUBMISSIONS_PER_WINDOW:
                return False, "提交过于频繁，请一分钟后再试。"
            recent.append(now)
            self._submissions_in_flight[key] += 1
            return True, ""

    def _release_submission(self, chat_id) -> None:
        key = str(chat_id)
        with self._submission_lock:
            self._submissions_in_flight[key] = max(
                0, self._submissions_in_flight[key] - 1
            )

    # ---------- 生命周期 ----------

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> tuple:
        """启动 Bot 线程。返回 (ok: bool, message: str)。"""
        with self._lock:
            if self.is_running:
                return True, "已在运行"

            token = self.store.raw_token().strip()
            if not token:
                return False, "请先填写 Bot 令牌"

            self._stop_evt = threading.Event()
            self._loop_ready = threading.Event()
            self._last_error = ""
            self._thread = threading.Thread(
                target=self._thread_main, name="telegram-bot", daemon=True
            )
            self._thread.start()
            # 等待 event loop 建立，便于后续 call_soon_threadsafe
            if not self._loop_ready.wait(timeout=15):
                return False, "启动超时"
            if self._last_error:
                return False, self._last_error
            return True, "已启动"

    def stop(self, timeout: float = 20.0) -> tuple:
        """停止 Bot 线程。返回 (ok: bool, message: str)。"""
        with self._lock:
            if not self.is_running:
                self.app = None
                self.local_bot = None
                return True, "未运行"

            self._stop_evt.set()
            loop, stop_ev = self._loop, self._async_stop
            if loop is not None and stop_ev is not None:
                try:
                    loop.call_soon_threadsafe(stop_ev.set)
                except RuntimeError:
                    pass
            thread = self._thread
            self._thread = None

        if thread:
            thread.join(timeout=timeout)
            if thread.is_alive():
                return False, "停止超时（线程仍在运行）"

        with self._lock:
            self.app = None
            self.local_bot = None
            self._async_stop = None
            self._loop = None
        return True, "已停止"

    def _thread_main(self) -> None:
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._run())
        except Exception as e:  # noqa: BLE001 - 扩展异常不得外溢
            self._last_error = "Bot 运行异常，请检查令牌和网络设置"
            self._log(f"运行线程异常: {self.store.redact_text(e)}", "error")
        finally:
            try:
                if self._loop and not self._loop.is_closed():
                    self._loop.close()
            except Exception:  # noqa: BLE001
                pass
            self._loop_ready.set()

    # ---------- 异步主体 ----------

    async def _run(self) -> None:
        token = self.store.raw_token().strip()
        cfg = self.store.get_config()

        builder = Application.builder().token(token)
        # 放大超时，避免大文件上传被判定超时
        try:
            builder = (builder
                       .read_timeout(NORMAL_TIMEOUT)
                       .write_timeout(NORMAL_TIMEOUT)
                       .connect_timeout(30)
                       .pool_timeout(30))
        except Exception:  # noqa: BLE001 - 版本差异时降级为默认超时
            pass
        try:
            builder = builder.get_updates_read_timeout(15)
        except Exception:  # noqa: BLE001
            pass
        proxy = (cfg.get("proxy") or "").strip()
        if proxy:
            try:
                builder = builder.proxy(proxy).get_updates_proxy(proxy)
            except Exception:  # noqa: BLE001
                self._log("代理参数不被当前版本支持，已忽略", "warning")

        self.app = builder.build()
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("help", self._cmd_help))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_text))

        self._async_stop = asyncio.Event()
        self._local_ok = False
        self._local_checked_at = 0.0
        self._build_local_bot(token, cfg)

        await self.app.initialize()
        await self.app.start()
        try:
            await self.app.updater.start_polling()
        except Exception as e:  # noqa: BLE001
            self._last_error = "无法连接 Telegram，请检查令牌和网络设置"
            self._log(f"无法连接 Telegram: {self.store.redact_text(e)}", "error")
            self._loop_ready.set()
            await self._safe_shutdown()
            return

        # 缓存自身信息，供界面展示
        try:
            self._me = await self.app.bot.get_me()
        except Exception as e:  # noqa: BLE001
            self._log(f"获取 Bot 信息失败: {e}", "warning")

        self._log(f"Bot 已启动: @{getattr(self._me, 'username', 'unknown')}")
        self._loop_ready.set()

        watcher = asyncio.create_task(self._watch_tasks())
        try:
            await self._async_stop.wait()
        finally:
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                pass
            await self._safe_shutdown()
            self._log("Bot 已停止")

    def _build_local_bot(self, token: str, cfg: dict) -> None:
        """构建本地加速服务 Bot（仅用于发送大文件）。未配置则为 None。"""
        url = (cfg.get("local_api_url") or "").strip()
        if not url:
            self.local_bot = None
            return
        base = url.rstrip("/")
        if not base.endswith("/bot"):
            base += "/bot"
        try:
            request = HTTPXRequest(
                connection_pool_size=4,
                connect_timeout=30,
                read_timeout=BIG_UPLOAD_TIMEOUT,
                write_timeout=BIG_UPLOAD_TIMEOUT,
                pool_timeout=60,
            )
            proxy = (cfg.get("proxy") or "").strip() or None
            self.local_bot = Bot(token=token, base_url=base, request=request,
                                 get_updates_request=request if not proxy else None)
        except Exception as e:  # noqa: BLE001
            self.local_bot = None
            self._log(f"本地加速服务初始化失败: {e}", "warning")

    async def _safe_shutdown(self) -> None:
        for coro_name in ("stop", "shutdown"):
            try:
                if coro_name == "stop" and self.app.updater:
                    await self.app.updater.stop()
                elif coro_name == "stop":
                    await self.app.stop()
                else:
                    await self.app.shutdown()
            except Exception:  # noqa: BLE001
                pass

    # ---------- 消息处理 ----------

    async def _require_allowed(self, update) -> bool:
        chat_id = update.effective_chat.id
        if self.store.is_allowed(chat_id):
            return True
        self.store.add_log("拒绝未授权使用者", "warning", chat_id=str(chat_id))
        await update.message.reply_text("当前未对你开放使用权限。")
        return False

    async def _cmd_start(self, update, context):
        chat_id = update.effective_chat.id
        if not self.store.is_allowed(chat_id):
            if not self.store.has_access_policy():
                await update.message.reply_text(
                    "助手尚未配置允许名单。\n\n"
                    f"你的 Chat ID 是：{chat_id}\n\n"
                    "请回到本机影迹的 Telegram 助手页面，把这个编号加入允许名单。"
                    "在加入前不会创建任何下载任务。"
                )
            else:
                self.store.add_log("拒绝未授权使用者", "warning", chat_id=str(chat_id))
                await update.message.reply_text("当前未对你开放使用权限。")
            return
        name = update.effective_user.first_name if update.effective_user else ""
        await update.message.reply_text(
            f"你好{('，' + name) if name else ''}。\n\n"
            "把视频链接发给我，我会帮你保存下来，完成后直接回传给你。\n\n"
            "支持一次一个链接。发送 /help 查看详细说明。"
        )

    async def _cmd_help(self, update, context):
        if not await self._require_allowed(update):
            return
        cfg = self.store.get_config()
        limit = int(cfg.get("max_direct_send_mb", 50))
        big = "已启用" if self.local_bot else "未启用"
        await update.message.reply_text(
            "使用说明\n\n"
            "1. 直接发送视频链接即可开始下载。\n"
            "2. 下载完成后会自动把文件回传给你。\n"
            f"3. 较小的文件（约 {limit}MB 以内）可直接回传。\n"
            f"4. 大文件直传状态：{big}。未启用时，大文件会保存在本机并告知你位置。\n\n"
            "命令：/start 开始 · /status 查看状态 · /help 本说明"
        )

    async def _cmd_status(self, update, context):
        if not await self._require_allowed(update):
            return
        cfg = self.store.get_config()
        local_state = "可用" if await self._local_available() else (
            "未启用" if not self.local_bot else "不可用")
        await update.message.reply_text(
            "运行状态\n\n"
            f"· 大文件直传：{local_state}\n"
            f"· 进行中的任务：{self._pending}\n"
            f"· 大文件上限：{cfg.get('local_max_send_mb', 2000)}MB"
        )

    async def _on_text(self, update, context):
        chat_id = update.effective_chat.id
        text = (update.message.text or "").strip()

        if not await self._require_allowed(update):
            return

        urls = URL_RE.findall(text)
        if not urls:
            await update.message.reply_text("没有识别到链接，请发送完整的视频地址。")
            return

        # 与首页边界一致：单条消息只取第一个链接
        url = urls[0]
        allowed, reason = self._reserve_submission(chat_id)
        if not allowed:
            self.store.add_log("提交受限", "warning", chat_id=str(chat_id))
            await update.message.reply_text(reason)
            return
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:  # noqa: BLE001
            pass

        # 提交下载（主程序函数是同步的，放到线程池避免阻塞 event loop）
        try:
            task = await asyncio.to_thread(self._submit_task, url)
        except Exception as e:  # noqa: BLE001
            safe_error = self.store.redact_text(e)
            user_message = getattr(e, "user_message", "")
            self._log(f"创建任务失败: {safe_error}", "error")
            self.store.add_log("创建任务失败", "error", reason=safe_error)
            await update.message.reply_text(
                user_message or "这个链接暂时无法处理，请稍后再试。"
            )
            return
        finally:
            self._release_submission(chat_id)

        if not task or not task.get("id"):
            await update.message.reply_text("任务创建失败，请稍后再试。")
            return

        task_id = task["id"]
        public_url = _safe_public_url(url)
        self.store.add_mapping(task_id, chat_id, update.message.message_id, public_url)
        self.store.add_log("收到下载请求", "info", task_id=task_id, chat_id=str(chat_id))

        # 异步提取原帖标题/描述，供下载完成后回传时拼接 caption
        asyncio.create_task(self._fetch_and_store_caption(task_id, url))

        queued = task.get("status") == "queued"
        await update.message.reply_text(
            f"已加入队列，任务编号 #{task_id}\n"
            + ("前面还有任务在排队，完成后会立即发给你。" if queued else "完成后会立即发给你。")
        )

    async def _fetch_and_store_caption(self, task_id: str, url: str) -> None:
        """后台提取页面元数据并更新映射，失败不影响主流程。"""
        if not self._extract_caption:
            return
        try:
            caption = await asyncio.to_thread(self._extract_caption, url)
            if caption:
                self.store.update_mapping_caption(task_id, caption)
        except Exception as e:  # noqa: BLE001
            self._log(f"提取原帖文案失败: {e}", "warning")

    # ---------- 任务完成回传 ----------

    async def _watch_tasks(self) -> None:
        """轮询跟踪中的任务，完成后回传结果。"""
        while True:
            try:
                await asyncio.sleep(WATCH_INTERVAL)
                if self._stop_evt.is_set():
                    return
                for task_id, info in list(self.store.all_mappings().items()):
                    if self._stop_evt.is_set():
                        return
                    await self._check_task(task_id, info)
                self._pending = len(self.store.all_mappings())
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001 - 单次异常不得中断看护循环
                self._log(f"任务看护异常: {e}", "warning")
                await asyncio.sleep(WATCH_INTERVAL)

    async def _check_task(self, task_id: str, info: dict) -> None:
        task = self._get_task(task_id) if self._get_task else None
        if not task:
            # 任务已不在（重启后被清理），丢弃映射避免无限等待
            self.store.remove_mapping(task_id)
            return

        status = task.get("status")
        if status in ("downloading", "pending", "queued", "paused", "verifying", "incomplete"):
            return
        if status == "cancelled":
            self.store.remove_mapping(task_id)
            return

        chat_id = (info or {}).get("chat_id")
        if not chat_id:
            self.store.remove_mapping(task_id)
            return

        if status == "failed":
            reason = self.store.redact_text(task.get("error") or "下载未完成")
            await self._safe_reply(
                chat_id,
                f"下载未完成，请在本机影迹的任务列表中查看详情。\n任务编号 #{task_id}",
            )
            self.store.add_log("下载失败", "warning", task_id=task_id, reason=reason)
            self.store.remove_mapping(task_id)
            return

        if status != "complete":
            return

        file_path = task.get("file_path") or ""
        if not file_path or not os.path.exists(file_path):
            await self._safe_reply(
                chat_id,
                f"已完成，但没找到文件，请在影迹的任务列表中查看。\n任务编号 #{task_id}")
            self.store.add_log("完成但文件缺失", "warning", task_id=task_id)
            self.store.remove_mapping(task_id)
            return

        await self._deliver(chat_id, file_path, task_id)
        self.store.remove_mapping(task_id)

    def _build_caption(self, task_id: str, info: dict | None, translated_body: str | None = None) -> str:
        """根据保存的元数据构建回传 caption（含原链接、文案、任务编号）。
        translated_body 非空时优先使用（已翻译正文，不含 uploader/url/编号）。
        """
        info = info or {}
        caption_meta = info.get("caption") or {}
        uploader = caption_meta.get("uploader", "")
        url = info.get("url", "")

        if translated_body and translated_body.strip():
            body = translated_body.strip()
        else:
            title = caption_meta.get("title", "")
            description = caption_meta.get("description", "")
            parts = []
            if title:
                parts.append(title)
            if description and description != title:
                parts.append(description)
            body = "\n\n".join(p for p in parts if p)

        if uploader:
            body = f"{body}\n\n@{uploader}"

        # Telegram media caption 上限为 1024；预留格式余量在 1000 截断，
        # 并始终优先保留任务编号和已脱敏的原链接。
        suffix = f"原链接：{url}\n\n#{task_id}"
        if len(suffix) >= 1000:
            return f"任务编号 #{task_id}"[:1000]
        body_limit = 1000 - len(suffix) - 2
        if len(body) > body_limit:
            body = body[:max(0, body_limit - 1)] + "…"
        return f"{body}\n\n{suffix}" if body else suffix

    # ---------- 翻译接入 ----------

    @staticmethod
    def _build_source_text(caption_meta: dict) -> str:
        """拼出待翻译正文：标题 + 描述（不含 uploader / url / 任务编号）。"""
        title = (caption_meta or {}).get("title", "")
        description = (caption_meta or {}).get("description", "")
        parts = []
        if title:
            parts.append(title)
        if description and description != title:
            parts.append(description)
        return "\n\n".join(p for p in parts if p)

    def _get_translator(self):
        """返回当前生效的翻译器，带配置指纹缓存；未启用或不支持时返回 None。"""
        cfg = self.store.get_config()
        t = cfg.get("translation") or {}
        if not t.get("enabled"):
            self._translator = None
            return None
        fp = json.dumps(
            {k: t.get(k) for k in ("provider", "api_key", "secret_id",
                                   "secret_key", "base_url", "model", "target_lang", "region")},
            sort_keys=True, ensure_ascii=False)
        if self._translator and self._translator_fp == fp:
            return self._translator
        try:
            from core.translate import build_translator
            tr = build_translator(t)
        except Exception as e:  # noqa: BLE001
            self._log(f"构建翻译器失败: {e}", "warning")
            tr = None
        self._translator = tr
        self._translator_fp = fp
        return tr

    async def _deliver(self, chat_id, file_path: str, task_id: str) -> None:
        cfg = self.store.get_config()
        direct_mb = int(cfg.get("max_direct_send_mb", 50))
        local_mb = int(cfg.get("local_max_send_mb", 2000))
        size_mb = os.path.getsize(file_path) / (1024 * 1024)

        # 若开启回传翻译：翻译正文（标题/描述），保留 uploader/url/编号不译；失败回退原文
        info = self.store.get_mapping(task_id) or {}
        caption_meta = info.get("caption") or {}
        translated_body = None
        tr = self._get_translator()
        if tr:
            try:
                src = self._build_source_text(caption_meta)
                if src:
                    target_lang = (cfg.get("translation") or {}).get("target_lang") or "zh"
                    translated_body = await asyncio.to_thread(tr.translate, src, target_lang)
            except Exception as e:  # noqa: BLE001
                safe_error = self.store.redact_text(e)
                self._log(f"翻译失败，回退原文: {safe_error}", "warning")
                self.store.add_log("翻译失败，回退原文", "warning", task_id=task_id, reason=safe_error)
        caption = self._build_caption(task_id, info, translated_body=translated_body)

        if size_mb <= direct_mb:
            ok = await self._send_file(self.app.bot, chat_id, file_path, caption)
            if ok:
                return
            # 官方通道失败时，尝试本地加速服务兜底
            if self.local_bot and await self._local_available():
                if await self._send_file(self.local_bot, chat_id, file_path, caption):
                    return
        elif self.local_bot and size_mb <= local_mb and await self._local_available():
            await self._safe_reply(chat_id, "文件较大，正在上传，请稍候…")
            if await self._send_file(self.local_bot, chat_id, file_path, caption):
                return

        # 兜底：告知本地保存位置
        name = Path(file_path).name
        await self._safe_reply(
            chat_id,
            f"{caption}\n\n"
            "文件较大，已保存在本机影迹的 TelegramBot 下载目录。\n"
            f"文件名：{name}（{size_mb:.1f}MB）")
        self.store.add_log("文件保留在本机", "info", task_id=task_id, size=f"{size_mb:.1f}MB")

    async def _send_file(self, bot, chat_id, file_path: str, caption: str) -> bool:
        ext = Path(file_path).suffix.lower()
        try:
            with open(file_path, "rb") as fh:
                if ext in VIDEO_EXTS:
                    await bot.send_video(chat_id=chat_id, video=fh, caption=caption,
                                         supports_streaming=True)
                elif ext in IMAGE_EXTS:
                    await bot.send_photo(chat_id=chat_id, photo=fh, caption=caption)
                elif ext in AUDIO_EXTS:
                    await bot.send_audio(chat_id=chat_id, audio=fh, caption=caption)
                else:
                    await bot.send_document(chat_id=chat_id, document=fh, caption=caption)
            self.store.add_log("文件已回传", "info", task_id=task_id,
                               file=Path(file_path).name)
            return True
        except Exception as e:  # noqa: BLE001
            safe_error = self.store.redact_text(e)
            self.store.add_log("回传失败", "warning", task_id=task_id, reason=safe_error)
            self._log(f"发送文件失败: {safe_error}", "warning")
            # 发送失败立即复检本地加速服务可用性
            self._local_checked_at = 0.0
            return False

    # ---------- 本地加速服务探测 ----------

    async def _local_available(self) -> bool:
        if not self.local_bot:
            return False
        now = time.time()
        if now - self._local_checked_at < LOCAL_PROBE_TTL:
            return self._local_ok
        try:
            await asyncio.wait_for(self.local_bot.get_me(), timeout=3)
            self._local_ok = True
        except Exception:  # noqa: BLE001
            self._local_ok = False
        self._local_checked_at = now
        return self._local_ok

    # ---------- 对外 ----------

    async def _safe_reply(self, chat_id, text: str) -> None:
        try:
            await self.app.bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:  # noqa: BLE001
            self._log(f"回复失败: {e}", "warning")

    def status(self) -> dict:
        me = self._me
        return {
            "running": self.is_running,
            "username": f"@{me.username}" if me and getattr(me, "username", None) else "",
            "local_available": bool(self.local_bot) and self._local_ok,
            "local_configured": bool(self.local_bot),
            "pending": self._pending,
            "last_error": self._last_error,
        }
