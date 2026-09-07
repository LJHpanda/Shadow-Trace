"""影迹 Telegram Bot 扩展 · 多源翻译引擎。

弱绑定约束（务必保持）：
- 仅使用标准库 + 可选 certifi，不引入任何新依赖（影迹依赖面已极简）。
- 不 import server / flask / 任何主程序模块。
- 失败一律抛异常，由调用方决定降级策略（本模块不静默吞错）。

可插拔 provider（用户在前端选择其一，仅需填写对应凭证）：
- deepseek           : OpenAI 兼容协议，填 API Key（默认地址/模型已内置）
- openai             : OpenAI 兼容协议，填 API Key（可改地址/模型，兼容通义等）
- tencent            : 腾讯云机器翻译，填 SecretId + SecretKey
- google             : Google 官方 Cloud Translation，填 API Key

统一接口：Translator.translate(text, to="zh") -> str
"""

from __future__ import annotations

import json
import ssl
import time
import hmac
import hashlib
import datetime
import urllib.request
import urllib.error
import urllib.parse
from collections import OrderedDict

from .network_policy import validate_outbound_http_url

try:
    import certifi
    _SSL = ssl.create_default_context(cafile=certifi.where())
except Exception:  # noqa: BLE001
    _SSL = ssl.create_default_context()


class _PublicOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_outbound_http_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _public_urlopen(request, timeout: int):
    validate_outbound_http_url(request.full_url)
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=_SSL),
        _PublicOnlyRedirectHandler(),
    )
    return opener.open(request, timeout=timeout)

# ---------------------------------------------------------------------------
# provider 元数据：前端据此动态渲染字段，后端据此构造翻译器
# ---------------------------------------------------------------------------

PROVIDERS = {
    "deepseek": {
        "label": "DeepSeek",
        "desc": "OpenAI 兼容协议，填写 API Key 即可，地址与模型已预置。",
        "base_url_default": "https://api.deepseek.com",
        "model_default": "deepseek-chat",
        "fields": [
            {"key": "api_key", "label": "API Key", "type": "password", "required": True},
        ],
    },
    "openai": {
        "label": "OpenAI 兼容",
        "desc": "兼容 OpenAI 协议的任意服务（含通义、月之暗面等），可改地址与模型。",
        "base_url_default": "https://api.openai.com/v1",
        "model_default": "gpt-4o-mini",
        "fields": [
            {"key": "api_key", "label": "API Key", "type": "password", "required": True},
            {"key": "base_url", "label": "接口地址", "type": "text", "required": False},
            {"key": "model", "label": "模型", "type": "text", "required": False},
        ],
    },
    "tencent": {
        "label": "腾讯云翻译",
        "desc": "官方机器翻译，填写 SecretId 与 SecretKey。",
        "region_default": "ap-guangzhou",
        "fields": [
            {"key": "secret_id", "label": "SecretId", "type": "password", "required": True},
            {"key": "secret_key", "label": "SecretKey", "type": "password", "required": True},
            {"key": "region", "label": "地域", "type": "text", "required": False},
        ],
    },
    "google": {
        "label": "Google 翻译（官方）",
        "desc": "Google Cloud Translation v2，填写 API Key（需绑卡，合规稳定）。",
        "fields": [
            {"key": "api_key", "label": "API Key", "type": "password", "required": True},
        ],
    },
}


# ---------------------------------------------------------------------------
# 语言码映射
# ---------------------------------------------------------------------------

# 给 LLM 的自然语言名
_LANG_NAMES = {
    "zh": "Chinese", "zh-cn": "Chinese", "zh-tw": "Traditional Chinese",
    "en": "English", "ja": "Japanese", "ko": "Korean", "fr": "French",
    "de": "German", "ru": "Russian", "es": "Spanish", "pt": "Portuguese",
}
# 腾讯云 TMT 语言码（ISO 简化）
_TMT_LANG = {
    "zh": "zh", "zh-cn": "zh", "zh-tw": "zh-TW",
    "en": "en", "ja": "ja", "ko": "ko", "fr": "fr", "de": "de", "ru": "ru", "es": "es",
}
# Google Translate 语言码
_GOOGLE_LANG = {
    "zh": "zh-CN", "zh-cn": "zh-CN", "zh-tw": "zh-TW",
    "en": "en", "ja": "ja", "ko": "ko", "fr": "fr", "de": "de", "ru": "ru", "es": "es",
}


def _lang_name(code: str) -> str:
    return _LANG_NAMES.get((code or "zh").lower(), code or "Chinese")


def _normalize_lang(code: str, table: dict) -> str:
    return table.get((code or "zh").lower(), (code or "zh"))


# ---------------------------------------------------------------------------
# 基类
# ---------------------------------------------------------------------------

class Translator:
    """翻译器基类：提供缓存与统一入口，子类实现 _do_translate。"""

    _MAX_CACHE = 500

    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.target = (self.cfg.get("target_lang") or "zh").lower()
        self._cache: "OrderedDict[tuple, str]" = OrderedDict()

    def translate(self, text: str, to: str | None = None) -> str:
        to = (to or self.target).lower()
        if not text or not text.strip():
            return text
        key = (text, to)
        if key in self._cache:
            return self._cache[key]
        out = self._do_translate(text, to)
        if out:
            self._cache[key] = out
            if len(self._cache) > self._MAX_CACHE:
                # 丢弃一半，避免长期运行内存膨胀
                for _ in range(self._MAX_CACHE // 2):
                    self._cache.popitem(last=False)
        return out

    def _do_translate(self, text: str, to: str) -> str:
        raise NotImplementedError

    @staticmethod
    def _http_json(url: str, payload: dict | None, headers: dict, timeout: int = 20) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url, data=data, headers=headers, method="POST" if data is not None else "GET")
        with _public_urlopen(req, timeout) as r:
            return json.load(r)

    @staticmethod
    def _http_json_get(url: str, headers: dict, timeout: int = 20) -> dict:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with _public_urlopen(req, timeout) as r:
            return json.load(r)


# ---------------------------------------------------------------------------
# OpenAI 兼容（覆盖 deepseek / openai / 通义等）
# ---------------------------------------------------------------------------

class OpenAICompatibleTranslator(Translator):
    """Chat Completions 协议：用 prompt 约束只输出译文，保留 @handle/URL/#编号。"""

    def _do_translate(self, text: str, to: str) -> str:
        provider = (self.cfg.get("provider") or "openai").lower()
        meta = PROVIDERS.get(provider, PROVIDERS["openai"])
        base = (self.cfg.get("base_url") or meta.get("base_url_default")
                or "https://api.openai.com/v1").rstrip("/")
        model = self.cfg.get("model") or meta.get("model_default") or "gpt-4o-mini"
        api_key = self.cfg.get("api_key")
        if not api_key:
            raise ValueError("缺少 API Key")

        lang = _lang_name(to)
        sys_prompt = (
            f"You are a translation engine. Translate the user's text into {lang}. "
            "Output ONLY the translation, without any explanation, quotation marks, "
            "or extra formatting. Keep handles like @name, URLs, and #hashtags unchanged."
        )
        payload = {
            "model": model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": text},
            ],
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        resp = self._http_json(f"{base}/chat/completions", payload, headers, timeout=25)
        try:
            return resp["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError) as e:
            raise ValueError(f"响应解析失败: {e} | {str(resp)[:200]}")


# ---------------------------------------------------------------------------
# 腾讯云机器翻译（TC3-HMAC-SHA256 签名，标准库实现）
# ---------------------------------------------------------------------------

class TencentTranslator(Translator):
    def _do_translate(self, text: str, to: str) -> str:
        secret_id = self.cfg.get("secret_id")
        secret_key = self.cfg.get("secret_key")
        if not secret_id or not secret_key:
            raise ValueError("缺少 SecretId / SecretKey")
        region = self.cfg.get("region") or "ap-guangzhou"
        target = _normalize_lang(to, _TMT_LANG)
        action, version, service, host = "TextTranslate", "2018-03-21", "tmt", "tmt.tencentcloudapi.com"
        payload = json.dumps({
            "SourceText": text, "Source": "auto", "Target": target, "ProjectId": 0
        })
        headers, _ = self._tc3_sign(secret_id, secret_key, service, host, region,
                                    action, version, payload)
        resp = self._http_json(f"https://{host}", json.loads(payload), headers, timeout=20)
        try:
            return resp["Response"]["TargetText"]
        except (KeyError, TypeError) as e:
            err = resp.get("Response", {}).get("Error", {}).get("Message", str(resp)[:200])
            raise ValueError(f"腾讯云返回错误: {err}")

    @staticmethod
    def _tc3_sign(secret_id, secret_key, service, host, region, action, version, payload):
        import datetime as _dt
        algorithm = "TC3-HMAC-SHA256"
        now = _dt.datetime.utcnow()
        timestamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        date = now.strftime("%Y-%m-%d")

        payload_bytes = payload.encode("utf-8")
        hashed_payload = hashlib.sha256(payload_bytes).hexdigest()

        canonical_headers = (
            f"content-type:application/json\n"
            f"host:{host}\n"
            f"x-tc-action:{action.lower()}\n"
        )
        signed_headers = "content-type;host;x-tc-action"
        canonical_request = "\n".join([
            "POST", "/", "", canonical_headers, signed_headers, hashed_payload
        ])

        credential_scope = f"{date}/{service}/tc3_request"
        string_to_sign = "\n".join([
            algorithm, timestamp, credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
        ])

        def _hmac(key: bytes, msg: str) -> bytes:
            return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

        secret_date = _hmac(("TC3" + secret_key).encode("utf-8"), date)
        secret_service = _hmac(secret_date, service)
        secret_signing = _hmac(secret_service, "tc3_request")
        signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"),
                             hashlib.sha256).hexdigest()

        authorization = (
            f"{algorithm} Credential={secret_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        headers = {
            "Authorization": authorization,
            "Content-Type": "application/json",
            "Host": host,
            "X-TC-Action": action,
            "X-TC-Timestamp": str(int(_dt.datetime.strptime(
                timestamp, "%Y-%m-%dT%H:%M:%SZ").timestamp())),
            "X-TC-Version": version,
            "X-TC-Region": region,
        }
        return headers, signature


# ---------------------------------------------------------------------------
# Google 官方 Cloud Translation v2
# ---------------------------------------------------------------------------

class GoogleTranslator(Translator):
    def _do_translate(self, text: str, to: str) -> str:
        api_key = self.cfg.get("api_key")
        if not api_key:
            raise ValueError("缺少 API Key")
        target = _normalize_lang(to, _GOOGLE_LANG)
        url = ("https://translation.googleapis.com/language/translate/v2?key="
               + urllib.parse.quote(api_key, safe=""))
        payload = {"q": text, "target": target, "format": "text"}
        headers = {"Content-Type": "application/json"}
        resp = self._http_json(url, payload, headers, timeout=15)
        try:
            return resp["data"]["translations"][0]["translatedText"]
        except (KeyError, IndexError, TypeError) as e:
            raise ValueError(f"响应解析失败: {e} | {str(resp)[:200]}")


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------

_IMPL = {
    "deepseek": OpenAICompatibleTranslator,
    "openai": OpenAICompatibleTranslator,
    "tencent": TencentTranslator,
    "google": GoogleTranslator,
}


def build_translator(cfg: dict) -> Translator | None:
    """根据配置构造翻译器；未启用或不支持的 provider 返回 None。"""
    if not cfg or not cfg.get("enabled"):
        return None
    provider = (cfg.get("provider") or "").lower()
    impl = _IMPL.get(provider)
    if not impl:
        return None
    return impl(cfg)


def list_providers() -> dict:
    """返回 provider 元数据（供前端动态渲染）。"""
    return PROVIDERS
