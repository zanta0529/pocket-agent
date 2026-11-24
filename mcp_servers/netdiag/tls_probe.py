"""Stage 3 TLS/SSL 雙軌驗證與零應用層資料憑證鏈擷取轉接器。"""

import asyncio
import hashlib
import ssl
import time
from datetime import datetime, timezone
from typing import Any

import certifi
from cryptography import x509

from mcp_servers.netdiag.models import TlsCertInfo, TlsStageData


def _build_truststore_context() -> ssl.SSLContext:
    """建立系統憑證庫 SSLContext（含企業 Root CA），缺 truststore 時退回預設上下文。"""
    try:
        import truststore

        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        ctx = ssl.create_default_context()
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    _set_alpn(ctx)
    return ctx


def _build_certifi_context() -> ssl.SSLContext:
    """建立公開 Mozilla CA (certifi) 驗證上下文。"""
    ctx = ssl.create_default_context(cafile=certifi.where())
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    _set_alpn(ctx)
    return ctx


def _build_inspect_only_context() -> ssl.SSLContext:
    """建立純讀取憑證鏈上下文（僅於嚴格驗證失敗後做零 HTTP 資料純握手舉證）。"""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    _set_alpn(ctx)
    return ctx


def _set_alpn(ctx: ssl.SSLContext) -> None:
    try:
        ctx.set_alpn_protocols(["h2", "http/1.1"])
    except NotImplementedError, ssl.SSLError:
        pass


def parse_der_certificate(der_bytes: bytes) -> TlsCertInfo:
    """使用 cryptography 解析 DER 格式 X.509 憑證之公開中繼資料。"""
    cert = x509.load_der_x509_certificate(der_bytes)
    now = datetime.now(timezone.utc)
    not_before = cert.not_valid_before_utc
    not_after = cert.not_valid_after_utc
    days_remaining = int((not_after - now).total_seconds() // 86400)

    sans: list[str] = []
    try:
        ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        sans.extend(ext.value.get_values_for_type(x509.DNSName))
        for ip_val in ext.value.get_values_for_type(x509.IPAddress):
            sans.append(str(ip_val))
    except x509.ExtensionNotFound:
        pass

    sha256_fp = hashlib.sha256(der_bytes).hexdigest().upper()
    formatted_fp = ":".join(sha256_fp[i : i + 2] for i in range(0, len(sha256_fp), 2))
    serial_hex = format(cert.serial_number, "X")

    return TlsCertInfo(
        subject=cert.subject.rfc4514_string(),
        issuer=cert.issuer.rfc4514_string(),
        not_before=not_before.isoformat(),
        not_after=not_after.isoformat(),
        days_remaining=days_remaining,
        serial_number=serial_hex,
        sha256_fingerprint=formatted_fp,
        san=tuple(sans[:20]),
    )


def _extract_chain_from_ssl_object(ssl_obj: Any, *, verified: bool) -> tuple[TlsCertInfo, ...]:
    if ssl_obj is None:
        return ()
    der_list: list[bytes] = []
    chain_method = "get_verified_chain" if verified else "get_unverified_chain"
    getter = getattr(ssl_obj, chain_method, None)
    if callable(getter):
        try:
            raw_chain = getter() or []
            for item in raw_chain:
                if isinstance(item, bytes):
                    der_list.append(item)
        except Exception:
            der_list = []

    if not der_list:
        try:
            leaf = ssl_obj.getpeercert(binary_form=True)
            if isinstance(leaf, bytes) and leaf:
                der_list.append(leaf)
        except Exception:
            pass

    parsed: list[TlsCertInfo] = []
    for der in der_list:
        try:
            parsed.append(parse_der_certificate(der))
        except Exception:
            continue
    return tuple(parsed)


async def _handshake_once(
    connect_ip: str,
    port: int,
    sni_host: str,
    ctx: ssl.SSLContext,
    timeout_sec: float,
    *,
    verified_mode: bool,
) -> dict[str, Any]:
    """執行純 TLS 握手並立即關閉連線（絕不傳送任何應用層 HTTP 位元組）。"""
    writer: asyncio.StreamWriter | None = None
    start = time.perf_counter()
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(
                host=connect_ip,
                port=port,
                ssl=ctx,
                server_hostname=sni_host or None,
            ),
            timeout=timeout_sec,
        )
        ssl_obj = writer.get_extra_info("ssl_object")
        cipher_info = ssl_obj.cipher() if ssl_obj else None
        cipher_name = str(cipher_info[0]) if cipher_info else ""
        alpn = (ssl_obj.selected_alpn_protocol() or "") if ssl_obj else ""
        tls_ver = (ssl_obj.version() or "") if ssl_obj else ""
        chain = _extract_chain_from_ssl_object(ssl_obj, verified=verified_mode)
        return {
            "ok": True,
            "latency_ms": round((time.perf_counter() - start) * 1000.0, 2),
            "tls_version": tls_ver,
            "cipher_suite": cipher_name,
            "alpn_protocol": alpn,
            "cert_chain": chain,
            "error": "",
        }
    except asyncio.CancelledError:
        raise
    except (ssl.SSLError, OSError, asyncio.TimeoutError) as exc:
        return {
            "ok": False,
            "latency_ms": round((time.perf_counter() - start) * 1000.0, 2),
            "tls_version": "",
            "cipher_suite": "",
            "alpn_protocol": "",
            "cert_chain": (),
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if writer is not None:
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
            except Exception:
                pass


async def probe_tls(connect_ip: str, port: int, sni_host: str, timeout_sec: float) -> TlsStageData:
    """執行嚴格雙軌 CA（truststore vs certifi）握手；驗證失敗時補做純 TLS 憑證舉證。"""
    ts_res = await _handshake_once(
        connect_ip,
        port,
        sni_host,
        _build_truststore_context(),
        timeout_sec,
        verified_mode=True,
    )
    cf_res = await _handshake_once(
        connect_ip,
        port,
        sni_host,
        _build_certifi_context(),
        timeout_sec,
        verified_mode=True,
    )

    ts_ok = bool(ts_res["ok"])
    cf_ok = bool(cf_res["ok"])

    if ts_ok or cf_ok:
        primary = ts_res if ts_ok else cf_res
        preferred = "truststore" if ts_ok else "certifi"
        return TlsStageData(
            status="ok",
            verified=True,
            inspected_without_verification=False,
            truststore_verified=ts_ok,
            certifi_verified=cf_ok,
            preferred_verify_source=preferred,
            tls_version=primary["tls_version"],
            cipher_suite=primary["cipher_suite"],
            alpn_protocol=primary["alpn_protocol"],
            sni_host=sni_host,
            latency_ms=primary["latency_ms"],
            cert_chain=primary["cert_chain"],
        )

    inspect_res = await _handshake_once(
        connect_ip,
        port,
        sni_host,
        _build_inspect_only_context(),
        timeout_sec,
        verified_mode=False,
    )
    return TlsStageData(
        status="failed",
        verified=False,
        inspected_without_verification=bool(inspect_res["ok"]),
        truststore_verified=False,
        certifi_verified=False,
        preferred_verify_source=None,
        tls_version=inspect_res["tls_version"],
        cipher_suite=inspect_res["cipher_suite"],
        alpn_protocol=inspect_res["alpn_protocol"],
        sni_host=sni_host,
        latency_ms=ts_res["latency_ms"],
        cert_chain=inspect_res["cert_chain"],
        error=ts_res["error"] or cf_res["error"],
    )
