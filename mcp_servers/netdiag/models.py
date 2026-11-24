"""網站與網路診斷 MCP 不可變資料模型。"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class NetdiagConfig:
    """執行期安全政策與限流設定。"""

    allow_private_networks: bool = False
    allow_loopback: bool = False
    allow_proxy: bool = False
    allowed_cidrs: tuple[str, ...] = ()
    allowed_domains: tuple[str, ...] = ()
    allowed_ports: tuple[int, ...] = (80, 443, 8080, 8443)
    default_timeout_sec: float = 10.0


@dataclass(frozen=True)
class ProxyInfo:
    """企業 HTTP Proxy 偵測狀態。"""

    used: bool = False
    display_proxy: str = ""
    raw_url: str = ""


@dataclass(frozen=True)
class ValidatedTarget:
    """通過 Stage 0 靜態授權檢查的目標端點。"""

    url: str
    scheme: str
    hostname: str
    port: int
    is_ip_literal: bool
    scope_label: str
    host_header: str = ""


@dataclass(frozen=True)
class DnsRecordItem:
    """單筆 DNS 查詢紀錄。"""

    record_type: str
    ttl: int | None
    value: str


@dataclass(frozen=True)
class DnsStageData:
    """Stage 1 DNS 解析觀測結果。"""

    status: str  # ok / failed / skipped
    ips: tuple[str, ...] = ()
    pinned_ip: str = ""
    cname_chain: tuple[str, ...] = ()
    ttl: int | None = None
    latency_ms: float | None = None
    fallback_used: bool = False
    error: str = ""


@dataclass(frozen=True)
class TcpStageData:
    """Stage 2 TCP 連通性觀測結果。"""

    status: str  # ok / failed / skipped / not_executed
    connect_ip: str = ""
    port: int = 0
    rtt_ms: float | None = None
    error_kind: str = ""  # timeout / connection_refused / os_error / ""
    error: str = ""


@dataclass(frozen=True)
class TlsCertInfo:
    """單張 X.509 憑證公開摘要。"""

    subject: str
    issuer: str
    not_before: str
    not_after: str
    days_remaining: int
    serial_number: str
    sha256_fingerprint: str
    san: tuple[str, ...] = ()


@dataclass(frozen=True)
class TlsStageData:
    """Stage 3 TLS/SSL 握手與雙軌 CA 對照結果。"""

    status: str  # ok / failed / skipped / not_executed
    verified: bool = False
    inspected_without_verification: bool = False
    truststore_verified: bool | None = None
    certifi_verified: bool | None = None
    preferred_verify_source: str | None = None
    tls_version: str = ""
    cipher_suite: str = ""
    alpn_protocol: str = ""
    sni_host: str = ""
    latency_ms: float | None = None
    cert_chain: tuple[TlsCertInfo, ...] = ()
    error: str = ""


@dataclass(frozen=True)
class HttpHop:
    """單次 HTTP 請求或重導向跳轉觀測明細。"""

    hop_index: int
    url: str
    connect_ip: str
    http_version: str
    status_code: int
    ttfb_ms: float
    total_ms: float
    bytes_read: int
    truncated: bool
    headers: dict[str, str] = field(default_factory=dict)
    set_cookies: tuple[str, ...] = ()
    location: str = ""


@dataclass(frozen=True)
class HttpStageData:
    """Stage 4 HTTP/HTTPS 觀測結果。"""

    status: str  # ok / failed / skipped / not_executed
    method: str = "HEAD"
    hops: tuple[HttpHop, ...] = ()
    final_status_code: int | None = None
    ttfb_ms: float | None = None
    total_ms: float | None = None
    redirect_blocked_reason: str = ""
    error: str = ""


@dataclass(frozen=True)
class SampleRun:
    """單次採樣之全階段交易結果。"""

    sample_index: int
    first_failure_stage: str
    dns: DnsStageData
    tcp: TcpStageData
    tls: TlsStageData
    http: HttpStageData
    total_ms: float | None = None


@dataclass(frozen=True)
class DiagnosticReport:
    """`netdiag_diagnose` 完整結構化舉證報告。"""

    observed_at_local: str
    observed_at_utc: str
    target_url: str
    target_scope: str
    host_header: str
    max_stage: str
    method: str
    samples_requested: int
    proxy_info: ProxyInfo
    first_failure_stage: str
    runs: tuple[SampleRun, ...]
    signals: tuple[str, ...] = ()
    escalation_hint: str = ""
    preflight_error: str = ""


@dataclass(frozen=True)
class ResolverQueryResult:
    """單一 DNS 解析器查詢結果。"""

    resolver_label: str
    status: str  # ok / failed / partial
    records: tuple[DnsRecordItem, ...] = ()
    cname_chain: tuple[str, ...] = ()
    latency_ms: float | None = None
    error: str = ""


@dataclass(frozen=True)
class DnsCompareReport:
    """`netdiag_dns_compare` 多解析器比對報告。"""

    observed_at_local: str
    observed_at_utc: str
    domain: str
    record_types: tuple[str, ...]
    fallback_used: bool
    results: tuple[ResolverQueryResult, ...]
    discrepancy_summary: str
    mail_security_signals: tuple[str, ...] = ()
    preflight_error: str = ""
