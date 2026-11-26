"""網站與網路診斷啟發式訊號分析與協查指引策略（Strategy Pattern）。"""

from abc import ABC, abstractmethod
from collections.abc import Sequence

from mcp_servers._shared import CONSTANTS, t
from mcp_servers.netdiag.models import ProxyInfo, SampleRun, TlsStageData

_WEAK_TLS_VERSIONS = frozenset({"SSLv2", "SSLv3", "TLSv1", "TLSv1.0", "TLSv1.1"})


class SignalAnalyzer(ABC):
    """診斷啟發式訊號分析抽象策略。"""

    @abstractmethod
    def analyze(self, runs: tuple[SampleRun, ...], proxy_info: ProxyInfo) -> list[str]:
        """從採樣結果萃取推論訊號（非證明）。"""


class ProxyAndDualStackSignalAnalyzer(SignalAnalyzer):
    """分析企業 Proxy 路徑與雙棧 IPv4／IPv6 DNS 紀錄分佈。"""

    def analyze(self, runs: tuple[SampleRun, ...], proxy_info: ProxyInfo) -> list[str]:
        signals: list[str] = []
        if proxy_info.used:
            signals.append(t("netdiag_mcp.sig_proxy", proxy=proxy_info.display_proxy))
        for run in runs:
            if run.dns.ipv4_ips and run.dns.ipv6_ips:
                signals.append(
                    t(
                        "netdiag_mcp.sig_dual_stack",
                        v4_count=len(run.dns.ipv4_ips),
                        v6_count=len(run.dns.ipv6_ips),
                    )
                )
        return signals


class TlsSecuritySignalAnalyzer(SignalAnalyzer):
    """分析雙軌 CA 差異、憑證過期／臨期與弱 TLS 協定版本。"""

    def _inspect_tls(self, tls: TlsStageData, *, from_redirect: bool = False) -> list[str]:
        signals: list[str] = []
        if from_redirect and tls.status != "not_executed":
            signals.append(t("netdiag_mcp.sig_redirect_tls_upgraded"))
        if tls.status == "ok" and tls.truststore_verified and tls.certifi_verified is False:
            signals.append(t("netdiag_mcp.sig_enterprise_ca"))
        if tls.tls_version and tls.tls_version in _WEAK_TLS_VERSIONS:
            signals.append(t("netdiag_mcp.sig_weak_tls", version=tls.tls_version))
        if tls.cert_chain:
            leaf = tls.cert_chain[0]
            if leaf.days_remaining < 0:
                signals.append(t("netdiag_mcp.sig_cert_expired", days=leaf.days_remaining))
            elif leaf.days_remaining <= CONSTANTS.netdiag_cert_expiry_warn_days:
                signals.append(
                    t(
                        "netdiag_mcp.sig_cert_expiring_soon",
                        days=leaf.days_remaining,
                        warn_days=CONSTANTS.netdiag_cert_expiry_warn_days,
                    )
                )
        return signals

    def analyze(self, runs: tuple[SampleRun, ...], proxy_info: ProxyInfo) -> list[str]:
        signals: list[str] = []
        for run in runs:
            signals.extend(self._inspect_tls(run.tls, from_redirect=False))
            if run.http.redirect_tls is not None:
                signals.extend(self._inspect_tls(run.http.redirect_tls, from_redirect=True))
        return signals


class EdgeFingerprintSignalAnalyzer(SignalAnalyzer):
    """可插拔之 CDN／WAF／反向代理指紋辨識與轉址攔截訊號分析。"""

    _EDGE_HEADER_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("Cloudflare", ("cf-ray", "cf-cache-status")),
        ("AWS CloudFront", ("x-amz-cf-id", "x-amz-cf-pop")),
        ("Akamai", ("x-akamai-transformed", "akamai-grn")),
        ("Fastly", ("x-fastly-request-id", "fastly-restarts")),
        ("Azure Front Door", ("x-azure-ref", "x-msedge-ref", "x-fd-healthprobe")),
        ("Vercel Edge", ("x-vercel-id",)),
        ("Envoy Proxy", ("x-envoy-upstream-service-time",)),
        ("ASP.NET / IIS", ("x-aspnet-version",)),
    )

    _SERVER_TOKEN_RULES: tuple[tuple[str, str], ...] = (
        ("microsoft-iis", "Microsoft IIS"),
        ("nginx", "Nginx"),
        ("cloudflare", "Cloudflare"),
        ("bigip", "F5 BIG-IP"),
    )

    def analyze(self, runs: tuple[SampleRun, ...], proxy_info: ProxyInfo) -> list[str]:
        signals: list[str] = []
        for run in runs:
            for hop in run.http.hops:
                hdrs = hop.headers
                for provider, keys in self._EDGE_HEADER_RULES:
                    matched = [f"{k}={hdrs[k]}" for k in keys if k in hdrs]
                    if matched:
                        signals.append(t("netdiag_mcp.sig_edge_detected", provider=provider, detail=", ".join(matched)))
                server_val = hdrs.get("server", "")
                if server_val:
                    signals.append(t("netdiag_mcp.sig_server_header", server=server_val))
                    server_lower = server_val.lower()
                    for token, provider in self._SERVER_TOKEN_RULES:
                        if token in server_lower:
                            signals.append(
                                t("netdiag_mcp.sig_edge_detected", provider=provider, detail=f"server={server_val}")
                            )
                if "x-cache" in hdrs:
                    signals.append(t("netdiag_mcp.sig_cache_header", value=hdrs["x-cache"]))
            if run.http.redirect_blocked_reason:
                signals.append(t("netdiag_mcp.sig_redirect_blocked", reason=run.http.redirect_blocked_reason))
        return signals


class HttpSecurityBaselineSignalAnalyzer(SignalAnalyzer):
    """分析 HTTPS 回應安全標頭缺失與 Set-Cookie 安全旗標缺漏。"""

    _RECOMMENDED_HTTPS_HEADERS = (
        "strict-transport-security",
        "x-content-type-options",
        "x-frame-options",
    )

    def analyze(self, runs: tuple[SampleRun, ...], proxy_info: ProxyInfo) -> list[str]:
        signals: list[str] = []
        for run in runs:
            if not run.http.hops:
                continue
            final_hop = run.http.hops[-1]
            if final_hop.url.startswith("https://") and 200 <= final_hop.status_code < 400:
                missing = [h for h in self._RECOMMENDED_HTTPS_HEADERS if h not in final_hop.headers]
                if missing:
                    signals.append(t("netdiag_mcp.sig_missing_sec_headers", headers=", ".join(missing)))
            for hop in run.http.hops:
                for cookie_summary in hop.set_cookies:
                    lower = cookie_summary.lower()
                    if hop.url.startswith("https://") and ("secure" not in lower or "httponly" not in lower):
                        signals.append(t("netdiag_mcp.sig_insecure_cookie", detail=cookie_summary))
        return signals


DEFAULT_SIGNAL_ANALYZERS: tuple[SignalAnalyzer, ...] = (
    ProxyAndDualStackSignalAnalyzer(),
    TlsSecuritySignalAnalyzer(),
    EdgeFingerprintSignalAnalyzer(),
    HttpSecurityBaselineSignalAnalyzer(),
)


def derive_signals(
    runs: tuple[SampleRun, ...],
    proxy_info: ProxyInfo,
    analyzers: Sequence[SignalAnalyzer] = DEFAULT_SIGNAL_ANALYZERS,
) -> tuple[str, ...]:
    """依序執行所有訊號分析策略並去除重複項目。"""
    collected: list[str] = []
    for analyzer in analyzers:
        collected.extend(analyzer.analyze(runs, proxy_info))
    return tuple(dict.fromkeys(collected))


def derive_escalation_hint(first_failure: str, runs: tuple[SampleRun, ...], proxy_info: ProxyInfo) -> str:
    """依據首個失敗階段產生對應負責單位的協查指引。"""
    if proxy_info.used and first_failure in ("tcp", "tls", "http"):
        return t("netdiag_mcp.hint_proxy", proxy=proxy_info.display_proxy)
    if first_failure == "dns":
        return t("netdiag_mcp.hint_dns")
    if first_failure == "tcp":
        for run in runs:
            if run.tcp.status == "failed":
                if run.tcp.error_kind == "connection_refused":
                    return t("netdiag_mcp.hint_tcp_refused")
                return t("netdiag_mcp.hint_tcp_timeout", port=run.tcp.port)
    if first_failure == "tls":
        return t("netdiag_mcp.hint_tls")
    if first_failure == "http":
        return t("netdiag_mcp.hint_http")
    return t("netdiag_mcp.hint_ok")
