"""Outbound network policy shared by download and translation workers.

The desktop service is intentionally local-only, but user supplied URLs still
leave the machine through yt-dlp and optional translation providers.  This
module rejects loopback, private, link-local, multicast and otherwise
non-public destinations, including redirect targets.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import urllib.parse
import urllib.request
from collections.abc import Iterable


class OutboundURLRejected(ValueError):
    """Raised when an outbound URL can reach a non-public destination."""


def _normalise_host(host: object) -> str:
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="strict")
    return str(host or "").strip().rstrip(".").lower()


def _ip_is_public(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


def _validate_host_syntax(host: str) -> None:
    if not host:
        raise OutboundURLRejected("链接缺少有效主机名")
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise OutboundURLRejected("不允许访问本机或局域网地址")

    try:
        target_ip = ipaddress.ip_address(host)
    except ValueError:
        if host.isdecimal():
            try:
                target_ip = ipaddress.ip_address(int(host))
            except ValueError:
                target_ip = None
        else:
            target_ip = None
    if target_ip is not None and not target_ip.is_global:
        raise OutboundURLRejected("不允许访问本机或局域网地址")


def validate_public_http_url(
    url: object,
    *,
    resolve: bool = False,
    resolver=None,
) -> str:
    """Validate an HTTP(S) URL and optionally require public DNS results."""
    value = str(url or "").strip()
    if not value:
        raise OutboundURLRejected("URL is required")
    if len(value) > 4096:
        raise OutboundURLRejected("链接过长，请检查后重试")

    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise OutboundURLRejected("链接格式不正确") from exc

    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise OutboundURLRejected("仅支持完整的 http/https 链接")
    if parsed.username is not None or parsed.password is not None:
        raise OutboundURLRejected("链接中不能包含账号或密码")
    if port is not None and not 1 <= port <= 65535:
        raise OutboundURLRejected("链接端口不合法")

    host = _normalise_host(parsed.hostname)
    _validate_host_syntax(host)
    if resolve:
        validate_public_dns(host, port or (443 if parsed.scheme == "https" else 80), resolver)
    return value


def validate_public_dns(host: object, port: int, resolver=None) -> None:
    """Require every resolved address for *host* to be globally routable."""
    hostname = _normalise_host(host)
    _validate_host_syntax(hostname)
    resolver = resolver or socket.getaddrinfo
    try:
        answers = resolver(hostname, port, type=socket.SOCK_STREAM)
    except (OSError, ValueError) as exc:
        raise OutboundURLRejected("目标地址暂时无法安全解析") from exc
    if not answers:
        raise OutboundURLRejected("目标地址暂时无法安全解析")
    for answer in answers:
        address = str(answer[4][0]).split("%", 1)[0]
        if not _ip_is_public(address):
            raise OutboundURLRejected("目标地址解析到了本机或局域网")


def _proxy_settings_from_environment() -> tuple[set[str], set[str]]:
    hosts: set[str] = set()
    schemes: set[str] = set()
    variables = {
        "HTTP_PROXY": {"http"}, "http_proxy": {"http"},
        "HTTPS_PROXY": {"https"}, "https_proxy": {"https"},
        "ALL_PROXY": {"http", "https"}, "all_proxy": {"http", "https"},
    }
    for name, supported_schemes in variables.items():
        value = os.environ.get(name, "").strip()
        if not value:
            continue
        try:
            host = urllib.parse.urlsplit(value).hostname
        except ValueError:
            host = None
        if host:
            hosts.add(_normalise_host(host))
            schemes.update(supported_schemes)
    return hosts, schemes


def validate_outbound_http_url(url: object, *, resolver=None) -> str:
    """Validate a URL while respecting an explicitly configured proxy.

    Direct connections require public DNS answers.  If urllib will route the
    URL through a configured proxy, DNS resolution is delegated to that proxy;
    URL syntax and literal private targets are still rejected locally.
    """
    value = validate_public_http_url(url, resolve=False)
    parsed = urllib.parse.urlsplit(value)
    _, proxied_schemes = _proxy_settings_from_environment()
    proxied = (
        parsed.scheme.lower() in proxied_schemes
        and not urllib.request.proxy_bypass(parsed.hostname or "")
    )
    if not proxied:
        validate_public_dns(
            parsed.hostname or "",
            parsed.port or (443 if parsed.scheme.lower() == "https" else 80),
            resolver,
        )
    return value


def install_yt_dlp_network_guard(extra_proxy_hosts: Iterable[str] = ()) -> None:
    """Install a fail-closed guard inside a dedicated yt-dlp child process.

    yt-dlp owns its redirect handling, so the guard covers three layers:
    request creation, redirect targets and DNS resolution.  Explicit proxy
    hosts may be local, but the target URL is still validated before it is sent
    to that proxy.
    """
    import yt_dlp.networking._urllib as urllib_backend
    from yt_dlp.networking.common import RequestDirector
    from yt_dlp.networking.exceptions import RequestError

    if getattr(RequestDirector, "_yingji_public_guard", False):
        return

    original_send = RequestDirector.send
    original_redirect = urllib_backend.RedirectHandler.redirect_request
    original_getaddrinfo = socket.getaddrinfo
    allowed_proxy_hosts, proxied_schemes = _proxy_settings_from_environment()
    allowed_proxy_hosts.update(_normalise_host(host) for host in extra_proxy_hosts if host)
    if proxied_schemes:
        # A configured proxy must remain the only route for those schemes;
        # otherwise NO_PROXY could silently bypass the proxy while DNS checks
        # are intentionally delegated to it (for example Clash Fake-IP mode).
        os.environ.pop("NO_PROXY", None)
        os.environ.pop("no_proxy", None)

    def checked_url(url: object) -> None:
        try:
            validate_outbound_http_url(url, resolver=original_getaddrinfo)
        except OutboundURLRejected as exc:
            raise RequestError("目标地址被本地网络安全策略拒绝", cause=exc) from exc

    def guarded_send(self, request):
        checked_url(request.url)
        return original_send(self, request)

    def guarded_redirect(self, req, fp, code, msg, headers, newurl):
        checked_url(newurl)
        return original_redirect(self, req, fp, code, msg, headers, newurl)

    def guarded_getaddrinfo(host, port, *args, **kwargs):
        hostname = _normalise_host(host)
        answers = original_getaddrinfo(host, port, *args, **kwargs)
        if hostname in allowed_proxy_hosts:
            return answers
        _validate_host_syntax(hostname)
        for answer in answers:
            address = str(answer[4][0]).split("%", 1)[0]
            if not _ip_is_public(address):
                raise OSError("目标地址被本地网络安全策略拒绝")
        return answers

    RequestDirector.send = guarded_send
    RequestDirector._yingji_public_guard = True
    urllib_backend.RedirectHandler.redirect_request = guarded_redirect
    socket.getaddrinfo = guarded_getaddrinfo
