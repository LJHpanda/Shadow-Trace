"""Telegram Bot 扩展模块 · 配置与映射持久化。

弱绑定约束（务必保持）：
- 不 import server / flask / 任何主程序模块，全部依赖由外部注入。
- 数据落盘目录由调用方传入（应来自 core/runtime_paths 解析结果），本模块不自建目录。
- 删除本文件 + 调用方不引用，即完全卸载，不留残留。

落盘结构（data_dir 下）：
    config.json     扩展配置（token 等）
    task_map.json   task_id -> chat_id 映射（不污染主任务结构）
    logs.json       最近运行日志（环形缓冲）
"""

from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
import urllib.parse
from pathlib import Path

# 默认配置：enabled=False —— 扩展默认关闭，需用户主动启用
DEFAULT_CONFIG = {
    "enabled": False,
    "token": "",
    "whitelist": [],          # 允许使用的 chat_id 列表；为空时仅提供安全配对提示
    "owner_chat_id": "",      # 可选的显式 owner；禁止由远端对话自动认领
    "auto_start": False,      # 主程序启动时不自动拉起 Bot
    "local_api_url": "",      # 本地加速服务地址（空 = 不启用大文件直传）
    "local_max_send_mb": 2000,
    "max_direct_send_mb": 50,  # 官方通道单文件硬限制，不建议上调
    "proxy": "",               # 可选网络代理，如 http://127.0.0.1:7890
    # 回传描述翻译：默认关闭，用户选择 provider 后仅填对应凭证即可
    "translation": {
        "enabled": False,
        "provider": "deepseek",     # deepseek / openai / tencent / google
        "api_key": "",
        "secret_id": "",
        "secret_key": "",
        "base_url": "",             # openai 系可自定义接口地址
        "model": "",                # 留空用 provider 默认值
        "region": "ap-guangzhou",   # 腾讯云地域
        "target_lang": "zh",        # 目标语言
    },
    "created_at": "",
    "updated_at": "",
}

# 数值型配置（反序列化时强转，避免前端传字符串）
_INT_FIELDS = ("local_max_send_mb", "max_direct_send_mb")

MAX_LOG_ENTRIES = 200
# 映射保留时长：超过后视为孤儿清理（单位秒，默认 7 天）
MAPPING_TTL_SECONDS = 7 * 24 * 3600


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def mask_token(token: str) -> str:
    """令牌脱敏：仅保留前 4 位与后 4 位，用于界面展示。"""
    if not token:
        return ""
    if len(token) <= 10:
        return token[:2] + "*" * (len(token) - 2)
    return f"{token[:4]}{'*' * 8}{token[-4:]}"


class TelegramBotStore:
    """配置 / 映射 / 日志的线程安全持久化。"""

    def __init__(self, data_dir, logger=None):
        self.data_dir = Path(data_dir)
        self.logger = logger
        self._lock = threading.RLock()
        self._config = copy.deepcopy(DEFAULT_CONFIG)
        self._task_map = {}
        self._logs = []
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._config_path = self.data_dir / "config.json"
        self._map_path = self.data_dir / "task_map.json"
        self._log_path = self.data_dir / "logs.json"
        self.load()

    # ---------- 内部 ----------

    def _log(self, msg: str, level: str = "info") -> None:
        if self.logger:
            getattr(self.logger, level, self.logger.info)(f"[telegram-bot] {msg}")

    def _read_json(self, path: Path, default):
        if not path.exists():
            return default
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:  # noqa: BLE001 - 配置损坏不应阻断主程序
            self._log(f"读取 {path.name} 失败，使用默认值: {e}", "warning")
            return default

    def _write_json(self, path: Path, payload) -> None:
        """原子写：先写临时文件再替换，避免中断产生半截文件。"""
        tmp = path.with_name(path.name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as e:  # noqa: BLE001
            self._log(f"写入 {path.name} 失败: {e}", "warning")
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:  # noqa: BLE001
                pass

    # ---------- 生命周期 ----------

    def load(self) -> None:
        with self._lock:
            cfg = self._read_json(self._config_path, {})
            if isinstance(cfg, dict):
                merged = copy.deepcopy(DEFAULT_CONFIG)
                merged.update(cfg)
                for key in _INT_FIELDS:
                    try:
                        merged[key] = int(merged[key])
                    except (TypeError, ValueError):
                        merged[key] = DEFAULT_CONFIG[key]
                merged["whitelist"] = self._normalize_whitelist(merged.get("whitelist"))
                try:
                    merged["owner_chat_id"] = self._normalize_chat_id(
                        merged.get("owner_chat_id"), allow_empty=True
                    )
                except ValueError:
                    merged["owner_chat_id"] = ""
                self._config = merged

            mapping = self._read_json(self._map_path, {})
            self._task_map = mapping if isinstance(mapping, dict) else {}

            logs = self._read_json(self._log_path, [])
            self._logs = logs if isinstance(logs, list) else []

    # ---------- 配置 ----------

    def get_config(self) -> dict:
        with self._lock:
            return dict(self._config)

    def get(self, key: str, default=None):
        with self._lock:
            return self._config.get(key, default)

    def update_config(self, patch: dict) -> dict:
        """合并更新配置并落盘，返回脱敏后的对外配置。"""
        with self._lock:
            for key, value in (patch or {}).items():
                if key not in DEFAULT_CONFIG:
                    continue
                if key == "whitelist":
                    self._config[key] = self._normalize_whitelist(value)
                elif key == "owner_chat_id":
                    self._config[key] = self._normalize_chat_id(value, allow_empty=True)
                elif key == "local_api_url":
                    self._config[key] = self._normalize_local_api_url(value)
                elif key == "translation":
                    # 嵌套 dict：以当前配置为基准 merge，保留已保存的其它 provider 凭证
                    base = dict(self._config.get("translation") or DEFAULT_CONFIG["translation"])
                    if isinstance(value, dict):
                        # 空字符串的敏感字段表示「不修改」（前端回显的是脱敏占位），
                        # 避免误清空已保存的凭据；其余字段按传值覆盖
                        value = dict(value)
                        for f in ("api_key", "secret_id", "secret_key"):
                            if f in value and not str(value[f]).strip():
                                value.pop(f)
                        base.update(value)
                    self._config[key] = base
                elif key in _INT_FIELDS:
                    try:
                        self._config[key] = int(value)
                    except (TypeError, ValueError):
                        continue
                else:
                    self._config[key] = value
            if not self._config.get("created_at"):
                self._config["created_at"] = _now()
            self._config["updated_at"] = _now()
            self._write_json(self._config_path, self._config)
        return self.public_config()

    @staticmethod
    def _normalize_whitelist(value) -> list:
        """白名单统一为合法 Telegram chat_id 字符串，去重去空。"""
        if isinstance(value, str):
            items = [v.strip() for v in value.replace("\n", ",").split(",")]
        elif isinstance(value, (list, tuple)):
            items = [str(v).strip() for v in value]
        else:
            items = []
        seen, out = set(), []
        for item in items:
            if re.fullmatch(r"-?\d{1,20}", item) and item not in seen:
                seen.add(item)
                out.append(item)
        return out

    @staticmethod
    def _normalize_chat_id(value, allow_empty: bool = False) -> str:
        text = str(value or "").strip()
        if not text and allow_empty:
            return ""
        if not re.fullmatch(r"-?\d{1,20}", text):
            raise ValueError("使用者编号必须是 Telegram Chat ID（仅数字，可为负数）")
        return text

    @staticmethod
    def _normalize_local_api_url(value) -> str:
        """The large-file Telegram API endpoint must remain on loopback."""
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            parsed = urllib.parse.urlsplit(text)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("本地加速服务地址格式不正确") from exc
        host = (parsed.hostname or "").rstrip(".").lower()
        if (
            parsed.scheme not in {"http", "https"}
            or host not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or (parsed.path not in {"", "/"})
            or (port is not None and not (1 <= port <= 65535))
        ):
            raise ValueError("本地加速服务只能使用本机 127.0.0.1/localhost 地址")
        return text.rstrip("/")

    def has_access_policy(self) -> bool:
        with self._lock:
            return bool(self._config.get("owner_chat_id") or self._config.get("whitelist"))

    def redact_text(self, value) -> str:
        """Remove stored credentials from errors before logs/API/UI exposure."""
        text = str(value or "")
        with self._lock:
            translation = self._config.get("translation") or {}
            secrets = [
                self._config.get("token", ""),
                self._config.get("proxy", ""),
                translation.get("api_key", ""),
                translation.get("secret_id", ""),
                translation.get("secret_key", ""),
            ]
        for secret in secrets:
            if secret:
                text = text.replace(str(secret), "***")
        text = re.sub(r"\b\d{8,10}:[A-Za-z0-9_-]{20,}\b", "***", text)
        text = re.sub(r"([?&](?:key|api_key|token)=)[^&\s]+", r"\1***", text, flags=re.I)
        return text[:1000]

    def public_config(self) -> dict:
        """对外配置：令牌脱敏，避免界面或接口泄漏完整凭据。"""
        with self._lock:
            cfg = dict(self._config)
        # The browser only needs to know whether a token exists. Never return
        # even a masked fragment because it is still derived from a credential.
        cfg["token"] = ""
        cfg["token_configured"] = bool(self._config.get("token"))
        # 翻译凭证脱敏（仅脱敏展示，不改变已保存值）
        t = cfg.get("translation")
        if isinstance(t, dict):
            t = dict(t)
            for f in ("api_key", "secret_id", "secret_key"):
                if t.get(f):
                    t[f + "_configured"] = True
                    # Never send translation credentials back to the browser.
                    # The UI uses the configured flag and an empty input placeholder.
                    t[f] = ""
            cfg["translation"] = t
        return cfg

    def raw_token(self) -> str:
        with self._lock:
            return self._config.get("token", "") or ""

    # ---------- task_id -> chat_id 映射 ----------

    def add_mapping(self, task_id: str, chat_id, message_id=None, url: str = "",
                    caption: dict | None = None) -> None:
        with self._lock:
            self._task_map[str(task_id)] = {
                "chat_id": str(chat_id),
                "message_id": message_id,
                "url": url,
                "caption": caption or {},
                "created_at": time.time(),
            }
            self._write_json(self._map_path, self._task_map)

    def update_mapping_caption(self, task_id: str, caption: dict) -> None:
        """异步提取到 caption 后更新映射，不阻塞主流程。"""
        with self._lock:
            item = self._task_map.get(str(task_id))
            if not item:
                return
            item["caption"] = caption or {}
            item["updated_at"] = time.time()
            self._write_json(self._map_path, self._task_map)

    def get_mapping(self, task_id: str):
        with self._lock:
            return self._task_map.get(str(task_id))

    def remove_mapping(self, task_id: str) -> None:
        with self._lock:
            if str(task_id) in self._task_map:
                self._task_map.pop(str(task_id))
                self._write_json(self._map_path, self._task_map)

    def all_mappings(self) -> dict:
        with self._lock:
            return dict(self._task_map)

    def prune_mappings(self, ttl: int = MAPPING_TTL_SECONDS) -> int:
        """清理过期映射，防止长期运行后文件无限增长。"""
        cutoff = time.time() - ttl
        with self._lock:
            stale = [k for k, v in self._task_map.items()
                     if isinstance(v, dict) and v.get("created_at", 0) < cutoff]
            for key in stale:
                self._task_map.pop(key, None)
            if stale:
                self._write_json(self._map_path, self._task_map)
        return len(stale)

    # ---------- 日志 ----------

    def add_log(self, message: str, level: str = "info", **extra) -> None:
        entry = {"ts": _now(), "level": level, "message": message}
        if extra:
            entry.update(extra)
        with self._lock:
            self._logs.append(entry)
            if len(self._logs) > MAX_LOG_ENTRIES:
                self._logs = self._logs[-MAX_LOG_ENTRIES:]
            self._write_json(self._log_path, self._logs)

    def get_logs(self, limit: int = 50) -> list:
        with self._lock:
            return list(self._logs[-limit:])

    def clear_logs(self) -> None:
        with self._lock:
            self._logs = []
            self._write_json(self._log_path, self._logs)

    # ---------- 白名单 ----------

    def is_allowed(self, chat_id) -> bool:
        chat_id = str(chat_id)
        with self._lock:
            whitelist = [str(x) for x in self._config.get("whitelist", [])]
            owner = str(self._config.get("owner_chat_id", "") or "")
        if chat_id in whitelist:
            return True
        # owner 只能由本机配置，远端消息绝不自动认领。
        return bool(owner) and chat_id == owner

    def adopt_owner(self, chat_id) -> bool:
        """Disabled: remote conversations must never claim local ownership."""
        return False
