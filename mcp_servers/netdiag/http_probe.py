"""Stage 4 HTTP/HTTPS 唯讀探測與手動逐跳轉址重審轉接器。"""

import asyncio
import ssl
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

import certifi
import httpx

from mcp_servers._shared import CONSTANTS, t
from mcp_servers.netdiag.dns_adapter import DnsResolverAdapter
from mcp_servers.netdiag.models import HttpHop, HttpStageData, ProxyInfo, ValidatedTarget
from mcp_servers.netdiag.policy import NetworkAccessPolicy, PolicyError
from mcp_servers.netdiag.redaction import NetdiagRedactionPolicy

_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_USER_AGENT = "PocketAgent-NetDiag-MCP/1.0"


def _build_httpx_ssl_context(verify_source: str | None) -> ssl.SSLContext | str:
    """嚴格依據 Stage 3 驗證來源建置 SSL 上下文，絕不關閉憑證驗證。"""
    if verify_source == "certifi":
        return certifi.where()
    try:
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        return ssl.create_default_context()


def _format_pinned_request(
    current_url: str,
    pinned_ip: str,
    port: int,
    host_override: str,
) -> tuple[str, dict[str, str], dict[str, str]]:
    """將請求鎖定至已驗證 IP 並保留正確 Host 與 SNI 標頭。"""
    parts = urlsplit(current_url)
    scheme = parts.scheme.lower()
    default_port = 443 if scheme == "https" else 80
    ip_host = f"[{pinned_ip}]" if ":" in pinned_ip else pinned_ip
    pinned_netloc = f"{ip_host}:{port}"
    pinned_url = urlunsplit((scheme, pinned_netloc, parts.path or "/", parts.query, ""))

    orig_host = host_override or (parts.hostname or "")
    host_header = f"[{orig_host}]" if ":" in orig_host and not orig_host.startswith("[") else orig_host
    if port != default_port and not host_override:
        host_header = f"{host_header}:{port}"

    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "*/*",
        "Host": host_header,
    }
    extensions = {"sni_hostname": orig_host} if scheme == "https" and orig_host else {}
    return pinned_url, headers, extensions


async def _resolve_hop_ip(
    hop_target: ValidatedTarget,
    policy: NetworkAccessPolicy,
    dns_adapter: DnsResolverAdapter,
    timeout_sec: float,
) -> str:
    if hop_target.is_ip_literal:
        return hop_target.hostname
    dns_res = await dns_adapter.resolve_host_ips(hop_target.hostname, timeout_sec)
    if dns_res.status != "ok" or not dns_res.ips:
        raise PolicyError(dns_res.error or "DNS resolution failed on redirect hop")
    valid_ips = [policy.validate_ip(ip) for ip in dns_res.ips]
    return valid_ips[0]


async def probe_http(
    target: ValidatedTarget,
    pinned_ip: str | None,
    method: str,
    policy: NetworkAccessPolicy,
    dns_adapter: DnsResolverAdapter,
    redaction: NetdiagRedactionPolicy,
    proxy_info: ProxyInfo,
    verify_source: str | None,
    timeout_sec: float,
) -> HttpStageData:
    """以唯讀 HEAD/GET 發送請求，手動逐跳驗證轉址目標與網段安全。"""
    verify_ctx = _build_httpx_ssl_context(verify_source)
    hops: list[HttpHop] = []
    current_url = target.url
    current_target = target
    current_ip = pinned_ip or target.hostname
    current_proxy = proxy_info
    host_override = target.host_header

    client_kwargs: dict = {
        "timeout": httpx.Timeout(timeout_sec),
        "follow_redirects": False,
        "trust_env": False,
        "verify": verify_ctx,
    }
    if current_proxy.used and current_proxy.raw_url:
        client_kwargs["proxy"] = current_proxy.raw_url

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            for hop_idx in range(CONSTANTS.max_redirects + 1):
                if current_proxy.used:
                    req_url = current_url
                    req_headers = {"User-Agent": _USER_AGENT, "Accept": "*/*"}
                    if host_override:
                        req_headers["Host"] = host_override
                    extensions: dict[str, str] = {}
                    connect_ip_label = f"proxy:{current_proxy.display_proxy}"
                else:
                    req_url, req_headers, extensions = _format_pinned_request(
                        current_url,
                        current_ip,
                        current_target.port,
                        host_override,
                    )
                    connect_ip_label = current_ip

                start = time.perf_counter()
                ttfb_ms = 0.0
                bytes_read = 0
                truncated = False

                async with client.stream(
                    method,
                    req_url,
                    headers=req_headers,
                    extensions=extensions,
                ) as resp:
                    ttfb_ms = round((time.perf_counter() - start) * 1000.0, 2)
                    async for chunk in resp.aiter_bytes():
                        bytes_read += len(chunk)
                        if bytes_read >= CONSTANTS.max_response_bytes:
                            bytes_read = CONSTANTS.max_response_bytes
                            truncated = True
                            break
                    total_ms = round((time.perf_counter() - start) * 1000.0, 2)
                    filtered_headers, cookies = redaction.filter_response_headers(resp.headers.multi_items())
                    raw_location = resp.headers.get("location") or ""
                    safe_location = redaction.redact_url(raw_location) if raw_location else ""
                    if "location" in filtered_headers and safe_location:
                        filtered_headers["location"] = redaction.clip_text(
                            safe_location,
                            CONSTANTS.netdiag_max_header_value_chars,
                        )
                    hops.append(
                        HttpHop(
                            hop_index=hop_idx,
                            url=redaction.redact_url(current_url),
                            connect_ip=connect_ip_label,
                            http_version=resp.http_version,
                            status_code=resp.status_code,
                            ttfb_ms=ttfb_ms,
                            total_ms=total_ms,
                            bytes_read=bytes_read,
                            truncated=truncated,
                            headers=filtered_headers,
                            set_cookies=cookies,
                            location=safe_location,
                        )
                    )

                    if resp.status_code not in _REDIRECT_CODES or not raw_location:
                        return HttpStageData(
                            status="ok" if resp.status_code < 400 else "failed",
                            method=method,
                            hops=tuple(hops),
                            final_status_code=resp.status_code,
                            ttfb_ms=ttfb_ms,
                            total_ms=total_ms,
                            error="" if resp.status_code < 400 else f"HTTP {resp.status_code}",
                        )

                    if hop_idx >= CONSTANTS.max_redirects:
                        msg = t("netdiag_mcp.redirect_limit_exceeded", max_redirects=CONSTANTS.max_redirects)
                        return HttpStageData(
                            status="failed",
                            method=method,
                            hops=tuple(hops),
                            final_status_code=resp.status_code,
                            ttfb_ms=ttfb_ms,
                            total_ms=total_ms,
                            error=msg,
                        )

                    next_url = urljoin(current_url, raw_location)
                    try:
                        next_target = policy.validate_target_preflight(next_url)
                        next_proxy = policy.detect_proxy(next_url)
                        if next_proxy.used != current_proxy.used:
                            raise PolicyError("Proxy policy mismatch across redirect hop")
                        next_ip = (
                            next_target.hostname
                            if next_proxy.used
                            else await _resolve_hop_ip(next_target, policy, dns_adapter, timeout_sec)
                        )
                    except PolicyError as exc:
                        reason = redaction.redact(str(exc))
                        msg = t("netdiag_mcp.redirect_blocked", hop=hop_idx + 1, reason=reason)
                        return HttpStageData(
                            status="failed",
                            method=method,
                            hops=tuple(hops),
                            final_status_code=resp.status_code,
                            ttfb_ms=ttfb_ms,
                            total_ms=total_ms,
                            redirect_blocked_reason=reason,
                            error=msg,
                        )

                    current_url = next_url
                    current_target = next_target
                    current_ip = next_ip
                    host_override = ""

    except asyncio.CancelledError:
        raise
    except (httpx.HTTPError, OSError, ssl.SSLError) as exc:
        err_prefix = (
            t("netdiag_mcp.proxy_failed", error=str(exc)) if current_proxy.used else f"{type(exc).__name__}: {exc}"
        )
        return HttpStageData(
            status="failed",
            method=method,
            hops=tuple(hops),
            error=redaction.redact(err_prefix),
        )

    return HttpStageData(status="failed", method=method, hops=tuple(hops), error="Unexpected redirect loop exit")
