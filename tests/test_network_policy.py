import socket
import urllib.request

import pytest

import server
from core.network_policy import (
    OutboundURLRejected,
    validate_outbound_http_url,
    validate_public_dns,
    validate_public_http_url,
)
from core.translate import OpenAICompatibleTranslator


def _answers(*addresses):
    return [
        (socket.AF_INET6 if ":" in address else socket.AF_INET,
         socket.SOCK_STREAM, 6, "", (address, 443))
        for address in addresses
    ]


@pytest.mark.parametrize("url", [
    "http://localhost/",
    "http://service.local/path",
    "http://127.0.0.1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/",
    "http://user:pass@example.test/",
])
def test_public_url_policy_rejects_local_and_credentialed_targets(url):
    with pytest.raises(OutboundURLRejected):
        validate_public_http_url(url)


def test_dns_policy_rejects_any_non_public_answer():
    def resolver(host, port, **kwargs):
        return _answers("93.184.216.34", "10.0.0.8")

    with pytest.raises(OutboundURLRejected, match="局域网"):
        validate_public_dns("public.example", 443, resolver)


def test_dns_policy_accepts_only_public_answers():
    def resolver(host, port, **kwargs):
        return _answers("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946")

    validate_public_dns("public.example", 443, resolver)


def test_explicit_proxy_delegates_public_domain_dns(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)

    def resolver(*args, **kwargs):
        raise AssertionError("target DNS must be delegated to the proxy")

    assert validate_outbound_http_url(
        "https://public.example/video", resolver=resolver
    ) == "https://public.example/video"
    with pytest.raises(OutboundURLRejected):
        validate_outbound_http_url("https://127.0.0.1/private", resolver=resolver)


def test_proxy_bypass_still_requires_public_dns(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: True)

    def resolver(host, port, **kwargs):
        return _answers("10.0.0.8")

    with pytest.raises(OutboundURLRejected, match="局域网"):
        validate_outbound_http_url("https://public.example/video", resolver=resolver)


def test_source_mode_uses_guarded_yt_dlp_worker():
    command = server._yt_dlp_command()

    assert command[0]
    assert command[1].endswith("yt_dlp_worker.py")


def test_translation_custom_endpoint_rejects_loopback_before_request():
    translator = OpenAICompatibleTranslator({
        "provider": "openai",
        "api_key": "TEST_ONLY_KEY",
        "base_url": "http://127.0.0.1:8080/v1",
    })

    with pytest.raises(OutboundURLRejected):
        translator.translate("hello", "zh")
