"""離線單元與安全門檻測試：NetDiag MCP 伺服器與 CLI 結構化確認整合。"""

import asyncio
import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from rich.console import Console

from mcp_servers import netdiag_mcp
from mcp_servers.netdiag import dns_adapter as nd_dns
from mcp_servers.netdiag import dns_compare as nd_dns_compare
from mcp_servers.netdiag import http_probe as nd_http
from mcp_servers.netdiag import pipeline as nd_pipeline
from mcp_servers.netdiag import policy as nd_policy
from mcp_servers.netdiag import tls_probe as nd_tls
from mcp_servers.netdiag.models import (
    DnsRecordItem,
    DnsStageData,
    NetdiagConfig,
    ProxyInfo,
    ResolverQueryResult,
    TlsCertInfo,
    TlsStageData,
)
from mcp_servers.netdiag.redaction import NETDIAG_REDACTION
from mcp_servers.netdiag.report import EscalationMarkdownFormatter
from pocket_agent.extensions.mcp import MCPManager
from pocket_agent.frontend.cli.confirmation import ConfirmationBridge


@pytest.fixture(autouse=True)
def _clean_netdiag_env(monkeypatch, tmp_path):
    monkeypatch.setenv("POCKET_AGENT_LOG_FILE", str(tmp_path / "agent.log"))
    for key in (
        "NETDIAG_ALLOW_PRIVATE_NETWORKS",
        "NETDIAG_ALLOW_LOOPBACK",
        "NETDIAG_ALLOW_PROXY",
        "NETDIAG_ALLOWED_CIDRS",
        "NETDIAG_ALLOWED_DOMAINS",
        "NETDIAG_ALLOWED_PORTS",
        "NETDIAG_TIMEOUT",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    ):
        monkeypatch.delenv(key, raising=False)


class _FakeDnsAdapter(nd_dns.DnsResolverAdapter):
    @property
    def is_fallback(self) -> bool:
        return False

    def __init__(self, ips=("93.184.216.34",), resolver_map=None) -> None:
        self.call_count = 0
        self.ips = ips
        self.resolver_map = resolver_map or {}

    async def resolve_host_ips(self, hostname: str, timeout_sec: float) -> DnsStageData:
        self.call_count += 1
        return DnsStageData(
            status="ok",
            ips=self.ips,
            pinned_ip=self.ips[0] if self.ips else "",
            cname_chain=("edge.example.net",),
            ttl=60,
            latency_ms=4.2,
        )

    async def query_records(
        self,
        domain: str,
        record_types: tuple[str, ...],
        nameserver: str | None,
        timeout_sec: float,
    ) -> ResolverQueryResult:
        self.call_count += 1
        label = nameserver or "system_default"
        if label in self.resolver_map:
            return self.resolver_map[label]
        return ResolverQueryResult(
            resolver_label=label,
            status="ok",
            records=(DnsRecordItem(record_type="A", ttl=120, value="93.184.216.34"),),
            latency_ms=3.1,
        )


def _generate_test_cert_der(*, days_offset: int, dns_name: str = "portal.corp.local") -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns_name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    not_before = now - datetime.timedelta(days=30)
    not_after = now + datetime.timedelta(days=days_offset)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(dns_name)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.DER)


def test_ac_sec_1_authorization_before_dns(monkeypatch, tmp_path):
    """AC-SEC-1: 未授權網域、埠口、內嵌帳密或雲端 Metadata 於 Stage 0 攔截且零 DNS 呼叫。"""
    monkeypatch.setenv("NETDIAG_ALLOWED_DOMAINS", "allowed.example.com")
    fake_dns = _FakeDnsAdapter()
    orch = nd_pipeline.DiagnosticOrchestrator(nd_policy.DefaultNetdiagPolicy(), fake_dns)
    dns_orch = nd_dns_compare.DnsCompareOrchestrator(nd_policy.DefaultNetdiagPolicy(), fake_dns)

    for bad_target in (
        "https://evil.example.org/",
        "https://allowed.example.com:8888/",
        "https://user:pass@allowed.example.com/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[fd00:ec2::254]/",
    ):
        report = asyncio.run(orch.run(target=bad_target))
        assert report.first_failure_stage == "preflight"
        assert report.preflight_error

    dns_report = asyncio.run(dns_orch.run(domain="evil.example.org"))
    assert dns_report.preflight_error
    assert fake_dns.call_count == 0

    audit_log = (tmp_path / "agent.log").read_text(encoding="utf-8")
    assert "[AUDIT] [MCP:netdiag]" in audit_log
    assert "status=BLOCKED" in audit_log


def test_ac_sec_2_private_ip_and_cidr_boundaries(monkeypatch):
    """AC-SEC-2: 私有 IP 預設阻斷；開啟後允許 RFC 1918 / ULA，仍硬阻斷保留網段與越界 CIDR。"""
    default_pol = nd_policy.DefaultNetdiagPolicy()
    with pytest.raises(nd_policy.PolicyError):
        default_pol.validate_ip("10.1.2.3")

    monkeypatch.setenv("NETDIAG_ALLOW_PRIVATE_NETWORKS", "true")
    priv_pol = nd_policy.DefaultNetdiagPolicy()
    assert priv_pol.validate_ip("10.1.2.3") == "10.1.2.3"
    assert priv_pol.validate_ip("fd12:3456::1") == "fd12:3456::1"

    for blocked in ("169.254.169.254", "127.0.0.1", "0.0.0.0", "224.0.0.1", "::ffff:169.254.169.254"):
        with pytest.raises(nd_policy.PolicyError):
            priv_pol.validate_ip(blocked)

    monkeypatch.setenv("NETDIAG_ALLOWED_CIDRS", "10.20.0.0/16")
    cidr_pol = nd_policy.DefaultNetdiagPolicy()
    assert cidr_pol.validate_ip("10.20.5.6") == "10.20.5.6"
    with pytest.raises(nd_policy.PolicyError):
        cidr_pol.validate_ip("10.99.1.1")


def test_ac_sec_3_redirect_revalidation_blocks_private_and_loops():
    """AC-SEC-3: 轉址至私有 IP／未授權埠口或超過上限時立即中止。"""
    pol = nd_policy.DefaultNetdiagPolicy(NetdiagConfig(allow_private_networks=False))
    fake_dns = _FakeDnsAdapter()
    target = pol.validate_target_preflight("https://public.example.com/")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "http://10.0.0.1/admin"})

    real_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with patch("mcp_servers.netdiag.http_probe.httpx.AsyncClient", return_value=real_client):
        http_res = asyncio.run(
            nd_http.probe_http(
                target,
                "93.184.216.34",
                "HEAD",
                pol,
                fake_dns,
                NETDIAG_REDACTION,
                ProxyInfo(),
                "certifi",
                5.0,
            )
        )

    assert http_res.status == "failed"
    assert http_res.redirect_blocked_reason
    assert len(http_res.hops) == 1

    def loop_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://public.example.com/next"})

    loop_client = httpx.AsyncClient(transport=httpx.MockTransport(loop_handler))
    with patch("mcp_servers.netdiag.http_probe.httpx.AsyncClient", return_value=loop_client):
        loop_res = asyncio.run(
            nd_http.probe_http(
                target,
                "93.184.216.34",
                "HEAD",
                pol,
                fake_dns,
                NETDIAG_REDACTION,
                ProxyInfo(),
                "certifi",
                5.0,
            )
        )

    assert loop_res.status == "failed"
    assert len(loop_res.hops) == 6
    assert "5" in loop_res.error


def test_ac_sec_4_tls_never_downgrades_to_http():
    """AC-SEC-4: max_stage=tls 或憑證過期失敗時，零 HTTP 請求並擷取過期憑證公開欄位。"""
    expired_der = _generate_test_cert_der(days_offset=-5, dns_name="expired.example.com")
    cert_info = nd_tls.parse_der_certificate(expired_der)
    assert cert_info.days_remaining < 0
    assert cert_info.san == ("expired.example.com",)

    pol = nd_policy.DefaultNetdiagPolicy()
    fake_dns = _FakeDnsAdapter()
    orch = nd_pipeline.DiagnosticOrchestrator(pol, fake_dns)

    expired_tls = TlsStageData(
        status="failed",
        verified=False,
        inspected_without_verification=True,
        truststore_verified=False,
        certifi_verified=False,
        cert_chain=(cert_info,),
        error="SSLCertVerificationError: certificate has expired",
    )

    fake_tcp = nd_pipeline.TcpStageData(status="ok", connect_ip="93.184.216.34", port=443, rtt_ms=2.5)
    with (
        patch("mcp_servers.netdiag.pipeline._probe_tcp", new=AsyncMock(return_value=fake_tcp)),
        patch("mcp_servers.netdiag.pipeline.probe_tls", new=AsyncMock(return_value=expired_tls)),
        patch("mcp_servers.netdiag.pipeline.probe_http", new=AsyncMock()) as mock_http,
    ):
        report = asyncio.run(orch.run(target="https://expired.example.com/", max_stage="http"))

    assert mock_http.await_count == 0
    assert report.first_failure_stage == "tls"
    assert report.runs[0].http.status == "not_executed"
    md = EscalationMarkdownFormatter().format_diagnostic(report)
    assert "tls_verified=false" in md
    assert cert_info.sha256_fingerprint in md


def test_ac_sec_5_redaction_cookie_and_header_sanitization():
    """AC-SEC-5: Set-Cookie 值、URL 機密參數、控制字元與非白名單標頭完全過濾遮罩。"""
    redacted_url = NETDIAG_REDACTION.redact_url("https://user:pw@site.example.com/cb?token=SECRET_TOK&page=1")
    assert "pw" not in redacted_url
    assert "SECRET_TOK" not in redacted_url
    assert "token=[REDACTED]" in redacted_url
    assert "page=1" in redacted_url
    assert NETDIAG_REDACTION.redact(redacted_url) == redacted_url

    headers, cookies = NETDIAG_REDACTION.filter_response_headers(
        [
            ("Set-Cookie", "sessionid=TOP_SECRET_VAL; HttpOnly; Secure; SameSite=Lax"),
            ("X-Custom-Internal-Secret", "leak"),
            ("Server", "nginx/1.24\r\nInjected: evil"),
        ]
    )
    assert "x-custom-internal-secret" not in headers
    assert "\r" not in headers["server"] and "\n" not in headers["server"]
    assert len(cookies) == 1
    assert "TOP_SECRET_VAL" not in cookies[0]
    assert "HttpOnly" in cookies[0] and "Secure" in cookies[0]


def test_ac_sec_6_proxy_fail_closed_and_cancellation(monkeypatch):
    """AC-SEC-6: 不支援的 Proxy 協定 fail-closed，且 CancelledError 不吞例外並關閉連線。"""
    monkeypatch.setenv("HTTPS_PROXY", "socks4://proxy.corp.local:1080")
    pol = nd_policy.DefaultNetdiagPolicy()
    with pytest.raises(nd_policy.PolicyError):
        pol.detect_proxy("https://example.com/")

    monkeypatch.delenv("HTTPS_PROXY")
    fake_dns = _FakeDnsAdapter()
    orch = nd_pipeline.DiagnosticOrchestrator(nd_policy.DefaultNetdiagPolicy(), fake_dns)

    with patch("mcp_servers.netdiag.pipeline._probe_tcp", side_effect=asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(orch.run(target="https://example.com/"))


def test_ac_sec_7_config_example_and_confirmation_bridge(monkeypatch):
    """AC-SEC-7: 範本預設停用且無 safe_read_tools；確認面板結構化預覽並禁用 a/A 寫入白名單。"""
    example_text = Path("mcp_config.example.yaml").read_text(encoding="utf-8")
    assert "# netdiag:" in example_text
    assert "#   enabled: false" in example_text

    # 確認 tool 名稱小於 64 字元
    assert len("mcp__netdiag__netdiag_diagnose") <= 64
    assert len("mcp__netdiag__netdiag_dns_compare") <= 64

    # 測試 enabled: false 不啟動子行程
    cfg_data = {
        "servers": {
            "netdiag": {
                "enabled": False,
                "transport": "stdio",
                "command": "python",
                "args": ["mcp_servers/netdiag_mcp.py"],
            }
        }
    }
    mgr = MCPManager()
    with patch.object(mgr, "_load_config", return_value=cfg_data), patch.object(mgr, "_connect") as mock_conn:
        res = asyncio.run(mgr.start())
    assert res.disabled_servers == 1
    assert mock_conn.await_count == 0

    # 測試 CLI ConfirmationBridge 結構化預覽與禁止自動放行／白名單寫入
    async def _run_confirm():
        state = SimpleNamespace(accept_edits=True, session_approved_commands=set())
        renderer = MagicMock()
        renderer.console = Console(record=True, width=100)
        bridge = ConfirmationBridge(SimpleNamespace(accept_edits=True), state, renderer)

        formatted = bridge._format_command(
            (
                "mcp__netdiag__netdiag_diagnose",
                {
                    "target": "https://10.20.1.5:8443/health",
                    "host_header": "portal.corp.local",
                    "max_stage": "http",
                    "method": "HEAD",
                    "samples": 2,
                    "timeout_sec": 10.0,
                },
            )
        )
        assert "10.20.1.5:8443" in formatted
        assert "portal.corp.local" in formatted
        assert bridge._is_auto_allowed(formatted, False) is False

        with patch("pocket_agent.frontend.cli.confirmation.add_command_to_allowlist") as mock_allow:
            assert bridge._apply_fixed_choice("a", formatted) is True
            assert bridge._apply_fixed_choice("A", formatted) is True
        assert not state.session_approved_commands
        assert mock_allow.call_count == 0

        # 保留既有 github_create_pr 格式
        gh_fmt = bridge._format_command(
            ("mcp__github__github_create_pr", {"owner": "o", "repo": "r", "head": "h", "title": "t", "body": "b"})
        )
        assert "o/r" in gh_fmt

    asyncio.run(_run_confirm())


def test_ac_func_1_ip_literal_host_header_and_dual_ca(monkeypatch):
    """AC-FUNC-1: IP 字面值略過 DNS、host_header 帶入 SNI/Host、雙軌 CA 呈現企業憑證訊號。"""
    monkeypatch.setenv("NETDIAG_ALLOW_PRIVATE_NETWORKS", "true")
    fake_dns = _FakeDnsAdapter()
    orch = nd_pipeline.DiagnosticOrchestrator(nd_policy.DefaultNetdiagPolicy(), fake_dns)

    ok_tls = TlsStageData(
        status="ok",
        verified=True,
        truststore_verified=True,
        certifi_verified=False,
        preferred_verify_source="truststore",
        tls_version="TLSv1.3",
        cipher_suite="TLS_AES_256_GCM_SHA384",
        alpn_protocol="h2",
        sni_host="portal.corp.local",
        latency_ms=8.5,
        cert_chain=(
            TlsCertInfo("CN=portal.corp.local", "CN=Corp Root CA", "2026-01-01", "2027-01-01", 300, "1A2B", "AA:BB"),
        ),
    )
    ok_http = nd_pipeline.HttpStageData(status="ok", method="HEAD", final_status_code=200, ttfb_ms=15.0, total_ms=18.0)

    fake_tcp = nd_pipeline.TcpStageData(status="ok", connect_ip="10.20.1.5", port=8443, rtt_ms=1.2)
    with (
        patch("mcp_servers.netdiag.pipeline._probe_tcp", new=AsyncMock(return_value=fake_tcp)),
        patch("mcp_servers.netdiag.pipeline.probe_tls", new=AsyncMock(return_value=ok_tls)) as mock_tls,
        patch("mcp_servers.netdiag.pipeline.probe_http", new=AsyncMock(return_value=ok_http)),
    ):
        report = asyncio.run(orch.run(target="https://10.20.1.5:8443/health", host_header="portal.corp.local"))

    assert fake_dns.call_count == 0
    assert report.runs[0].dns.status == "skipped"
    assert mock_tls.await_args.args[2] == "portal.corp.local"
    md = EscalationMarkdownFormatter().format_diagnostic(report)
    assert "truststore=True, certifi=False" in md


def test_ac_func_2_tcp_failure_short_circuit_and_samples():
    """AC-FUNC-2: TCP 失敗短路下游並給協查指引；samples=3 統計耗時，samples=4 拒絕。"""
    fake_dns = _FakeDnsAdapter()
    orch = nd_pipeline.DiagnosticOrchestrator(nd_policy.DefaultNetdiagPolicy(), fake_dns)

    with patch(
        "mcp_servers.netdiag.pipeline._probe_tcp",
        new=AsyncMock(
            return_value=nd_pipeline.TcpStageData(
                status="failed",
                connect_ip="93.184.216.34",
                port=443,
                error_kind="connection_refused",
                error="ConnectionRefusedError",
            )
        ),
    ):
        report = asyncio.run(orch.run(target="https://example.com/", samples=3))

    assert report.first_failure_stage == "tcp"
    assert report.runs[0].tls.status == "not_executed"
    assert report.runs[0].http.status == "not_executed"
    assert "IIS" in report.escalation_hint or "OS" in report.escalation_hint

    out_err = asyncio.run(netdiag_mcp.netdiag_diagnose(target="https://example.com/", samples=4))
    assert out_err.startswith("[ERROR]")


def test_ac_func_3_dns_compare_split_horizon_and_fallback():
    """AC-FUNC-3: 多解析器偵測 Split-Horizon 差異與 SPF/DMARC，缺 dnspython 時優雅降級。"""
    fake_dns = _FakeDnsAdapter(
        resolver_map={
            "system_default": ResolverQueryResult(
                resolver_label="system_default",
                status="ok",
                records=(
                    DnsRecordItem("A", 60, "10.10.10.10"),
                    DnsRecordItem("TXT", 300, "v=spf1 include:_spf.example.com ~all"),
                ),
                latency_ms=2.0,
            ),
            "8.8.8.8": ResolverQueryResult(
                resolver_label="8.8.8.8",
                status="ok",
                records=(DnsRecordItem("A", 300, "93.184.216.34"),),
                latency_ms=14.0,
            ),
        }
    )
    orch = nd_dns_compare.DnsCompareOrchestrator(nd_policy.DefaultNetdiagPolicy(), fake_dns)
    report = asyncio.run(orch.run(domain="portal.example.org", record_types="A,TXT", nameservers="8.8.8.8"))
    assert "差異" in report.discrepancy_summary or "Discrepancy detected" in report.discrepancy_summary
    assert any("SPF" in sig for sig in report.mail_security_signals)

    with patch("mcp_servers.netdiag.dns_adapter.importlib.import_module", side_effect=ImportError):
        adapter = nd_dns.create_dns_adapter()
    assert isinstance(adapter, nd_dns.SocketFallbackResolverAdapter)


def test_proxy_policy_default_fail_closed_opt_in_and_redirect_switch(monkeypatch):
    """預設拒用 Proxy（fail-closed）；顯式放行後揭露限制並阻斷跨 Proxy 轉址。"""
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy-a.corp.local:8080")
    pol_default = nd_policy.DefaultNetdiagPolicy()
    with pytest.raises(nd_policy.PolicyError):
        pol_default.detect_proxy("https://example.com/")

    monkeypatch.setenv("NETDIAG_ALLOW_PROXY", "true")
    pol_allowed = nd_policy.DefaultNetdiagPolicy()
    proxy_a = pol_allowed.detect_proxy("https://example.com/")
    assert proxy_a.used is True

    target = pol_allowed.validate_target_preflight("https://public.example.com/")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://other.example.org/next"})

    real_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with (
        patch("mcp_servers.netdiag.http_probe.httpx.AsyncClient") as mock_cls,
        patch.object(
            pol_allowed,
            "detect_proxy",
            return_value=nd_policy.ProxyInfo(
                used=True,
                display_proxy="http://proxy-b.corp.local:8080",
                raw_url="http://proxy-b.corp.local:8080",
            ),
        ),
    ):
        mock_cls.return_value = real_client
        http_res = asyncio.run(
            nd_http.probe_http(
                target,
                None,
                "HEAD",
                pol_allowed,
                _FakeDnsAdapter(),
                NETDIAG_REDACTION,
                proxy_a,
                "certifi",
                5.0,
            )
        )
    assert http_res.status == "failed"
    assert http_res.redirect_blocked_reason

    report = nd_pipeline.DiagnosticReport(
        observed_at_local="2026-11-24T10:00:00+08:00",
        observed_at_utc="2026-11-24T02:00:00+00:00",
        target_url="https://public.example.com/",
        target_scope="domain",
        host_header="",
        max_stage="http",
        method="HEAD",
        samples_requested=1,
        proxy_info=proxy_a,
        first_failure_stage=None,
        runs=(),
    )
    md = EscalationMarkdownFormatter().format_diagnostic(report)
    assert "Proxy" in md


def test_preflight_validation_errors_omit_raw_url_and_host_header():
    """預檢錯誤不回顯原始 URL、netloc 或未驗證 host_header。"""
    pol = nd_policy.DefaultNetdiagPolicy()
    with pytest.raises(nd_policy.PolicyError) as exc_ws:
        pol.validate_target_preflight("https://example.com/ ?sig=TOP_SECRET_SIG")
    assert "TOP_SECRET_SIG" not in str(exc_ws.value)

    with pytest.raises(nd_policy.PolicyError) as exc_port:
        pol.validate_target_preflight("https://example.com:badport/?token=SECRET_TOK")
    assert "SECRET_TOK" not in str(exc_port.value)
    assert "badport" not in str(exc_port.value)

    with pytest.raises(nd_policy.PolicyError) as exc_host:
        pol.validate_target_preflight("https://example.com/", host_header="bad host\r\nX-Inject: 1")
    assert "X-Inject" not in str(exc_host.value)

    orch = nd_pipeline.DiagnosticOrchestrator(pol, _FakeDnsAdapter())
    report = asyncio.run(orch.run(target="https://example.com:badport/?sig=SECRET_SIG"))
    assert report.first_failure_stage == "preflight"
    assert "SECRET_SIG" not in report.preflight_error


def test_stage_timeout_shared_deadline_for_tls_and_http():
    """TLS 與 HTTP 單階段共享單一逾時預算。"""
    from mcp_servers.netdiag import tls_probe as nd_tls

    call_count = 0

    async def slow_handshake(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.08)
        return {
            "ok": False,
            "tls_version": None,
            "cipher_suite": None,
            "alpn_protocol": None,
            "cert_chain": (),
            "latency_ms": 80.0,
            "error": "TimeoutError: timed out",
        }

    with patch("mcp_servers.netdiag.tls_probe._handshake_once", side_effect=slow_handshake):
        tls_res = asyncio.run(nd_tls.probe_tls("93.184.216.34", 443, "example.com", 0.1))

    assert tls_res.status == "failed"
    assert tls_res.inspected_without_verification is False
    assert call_count == 2

    pol = nd_policy.DefaultNetdiagPolicy()
    target = pol.validate_target_preflight("https://public.example.com/")

    class _SlowTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.5)
            return httpx.Response(200)

    real_client = httpx.AsyncClient(transport=_SlowTransport())
    with patch("mcp_servers.netdiag.http_probe.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = real_client
        http_res = asyncio.run(
            nd_http.probe_http(
                target,
                "93.184.216.34",
                "HEAD",
                pol,
                _FakeDnsAdapter(),
                NETDIAG_REDACTION,
                nd_policy.ProxyInfo(),
                "certifi",
                0.08,
            )
        )
    assert http_res.status == "failed"
    assert http_res.error


def test_dns_compare_excludes_failed_resolvers_and_socket_fallback_status():
    """僅比較 status=='ok' 解析器；Socket 降級遇到不支援型別回報 partial/failed。"""
    fake_dns = _FakeDnsAdapter(
        resolver_map={
            "system_default": ResolverQueryResult(
                resolver_label="system_default",
                status="ok",
                records=(DnsRecordItem("A", 60, "93.184.216.34"),),
                latency_ms=2.0,
            ),
            "8.8.8.8": ResolverQueryResult(
                resolver_label="8.8.8.8",
                status="failed",
                latency_ms=100.0,
                error="Timeout",
            ),
        }
    )
    orch = nd_dns_compare.DnsCompareOrchestrator(nd_policy.DefaultNetdiagPolicy(), fake_dns)
    report = asyncio.run(orch.run(domain="example.com", record_types="A", nameservers="8.8.8.8"))
    assert "8.8.8.8(failed)" in report.discrepancy_summary

    fallback = nd_dns.SocketFallbackResolverAdapter()
    with patch.object(
        fallback,
        "resolve_host_ips",
        new=AsyncMock(
            return_value=nd_pipeline.DnsStageData(
                status="ok",
                ips=("93.184.216.34",),
                pinned_ip="93.184.216.34",
            )
        ),
    ):
        res_partial = asyncio.run(fallback.query_records("example.com", ("A", "TXT"), None, 5.0))
        res_failed = asyncio.run(fallback.query_records("example.com", ("TXT",), None, 5.0))
    assert res_partial.status == "partial"
    assert res_failed.status == "failed"


def test_env_timeout_default_and_confirmation_preview_without_none(monkeypatch):
    """未傳 timeout_sec 時套用 NETDIAG_TIMEOUT；確認面板正確顯示預設值而非 None。"""
    monkeypatch.setenv("NETDIAG_TIMEOUT", "6")
    fake_dns = _FakeDnsAdapter()
    seen_timeouts: list[float] = []
    orig_resolve = fake_dns.resolve_host_ips

    async def spy_resolve(hostname: str, timeout_sec: float):
        seen_timeouts.append(timeout_sec)
        return await orig_resolve(hostname, timeout_sec)

    fake_dns.resolve_host_ips = spy_resolve  # type: ignore[method-assign]
    orch = nd_pipeline.DiagnosticOrchestrator(nd_policy.DefaultNetdiagPolicy(), fake_dns)
    report = asyncio.run(orch.run(target="https://example.com/", max_stage="dns"))
    assert report.runs[0].dns.status == "ok"
    assert seen_timeouts == [6.0]

    async def _check_preview() -> tuple[str, str]:
        state = SimpleNamespace(accept_edits=True, session_approved_commands=set())
        bridge = ConfirmationBridge(SimpleNamespace(accept_edits=True), state, MagicMock())
        preview_diag = bridge._format_command(("mcp__netdiag__netdiag_diagnose", {"target": "https://example.com/"}))
        preview_dns = bridge._format_command(
            ("mcp__netdiag__netdiag_dns_compare", {"domain": "example.com", "timeout_sec": None})
        )
        return preview_diag, preview_dns

    preview_diag, preview_dns = asyncio.run(_check_preview())
    assert "None" not in preview_diag
    assert "None" not in preview_dns


def test_domain_allowlist_exact_vs_wildcard_and_no_proxy_cidr_port(monkeypatch):
    """驗證網域精確白名單不隱性放行子網域，且 NO_PROXY 支援 CIDR 與帶埠號規則。"""
    monkeypatch.setenv("NETDIAG_ALLOWED_DOMAINS", "exact.example.com,*.wild.example.org")
    pol = nd_policy.DefaultNetdiagPolicy()
    assert pol.validate_domain_preflight("exact.example.com") == "exact.example.com"
    with pytest.raises(nd_policy.PolicyError):
        pol.validate_domain_preflight("sub.exact.example.com")
    assert pol.validate_domain_preflight("api.wild.example.org") == "api.wild.example.org"
    with pytest.raises(nd_policy.PolicyError):
        pol.validate_domain_preflight("wild.example.org")

    monkeypatch.delenv("NETDIAG_ALLOWED_DOMAINS", raising=False)
    monkeypatch.setenv("NETDIAG_ALLOW_PRIVATE_NETWORKS", "true")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp.local:8080")
    monkeypatch.setenv("NO_PROXY", "10.0.0.0/8,fc00::/7,direct.example.com:8443")
    pol_proxy = nd_policy.DefaultNetdiagPolicy()
    assert pol_proxy.detect_proxy("https://10.20.30.40/health").used is False
    assert pol_proxy.detect_proxy("https://[fd00:1::1]/health").used is False
    assert pol_proxy.detect_proxy("https://direct.example.com:8443/").used is False
    with pytest.raises(nd_policy.PolicyError):
        pol_proxy.detect_proxy("https://direct.example.com:443/")


def test_multi_sample_report_selects_first_failed_run_and_lists_all_samples():
    """驗證多採樣時優先呈現第一筆失敗採樣明細，避免頂部失敗與明細全 ok 矛盾。"""
    fake_dns = _FakeDnsAdapter()
    orch = nd_pipeline.DiagnosticOrchestrator(nd_policy.DefaultNetdiagPolicy(), fake_dns)

    tcp_ok = nd_pipeline.TcpStageData(status="ok", connect_ip="93.184.216.34", port=80, rtt_ms=1.5)
    tcp_fail = nd_pipeline.TcpStageData(
        status="failed",
        connect_ip="93.184.216.34",
        port=80,
        error_kind="timeout",
        error="TCP connection timed out",
    )
    http_ok = nd_pipeline.HttpStageData(status="ok", method="HEAD", final_status_code=200, ttfb_ms=4.0, total_ms=5.0)

    with (
        patch("mcp_servers.netdiag.pipeline._probe_tcp", new=AsyncMock(side_effect=[tcp_ok, tcp_fail])),
        patch("mcp_servers.netdiag.pipeline.probe_http", new=AsyncMock(return_value=http_ok)),
    ):
        report = asyncio.run(orch.run(target="http://example.com/", samples=2))

    assert report.first_failure_stage == "tcp"
    md = EscalationMarkdownFormatter().format_diagnostic(report)
    assert "| TCP | `failed` |" in md
    assert "Sample #1: `ok`" in md
    assert "Sample #2: `failed` at `tcp`" in md


def test_http_to_https_redirect_runs_tls_probe_and_classifies_tls_failure():
    """驗證 http:// 轉址 https:// 時觸發雙軌 TLS 檢查，失敗時歸類為 tls 並中止下游。"""
    pol = nd_policy.DefaultNetdiagPolicy()
    fake_dns = _FakeDnsAdapter()
    orch = nd_pipeline.DiagnosticOrchestrator(pol, fake_dns)
    tcp_ok = nd_pipeline.TcpStageData(status="ok", connect_ip="93.184.216.34", port=80, rtt_ms=1.2)

    expired_der = _generate_test_cert_der(days_offset=-3, dns_name="secure.example.com")
    expired_cert = nd_tls.parse_der_certificate(expired_der)
    failed_tls = TlsStageData(
        status="failed",
        verified=False,
        inspected_without_verification=True,
        cert_chain=(expired_cert,),
        error="SSLCertVerificationError: certificate has expired",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"Location": "https://secure.example.com/"})

    real_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with (
        patch("mcp_servers.netdiag.pipeline._probe_tcp", new=AsyncMock(return_value=tcp_ok)),
        patch("mcp_servers.netdiag.http_probe.httpx.AsyncClient", return_value=real_client),
        patch("mcp_servers.netdiag.http_probe.probe_tls", new=AsyncMock(return_value=failed_tls)) as mock_tls,
    ):
        report = asyncio.run(orch.run(target="http://example.com/"))

    assert mock_tls.await_count == 1
    assert report.first_failure_stage == "tls"
    md = EscalationMarkdownFormatter().format_diagnostic(report)
    assert expired_cert.sha256_fingerprint in md


def test_ip_family_filtering_and_dual_stack_signal():
    """驗證 ip_family=ipv4/ipv6 篩選、無相容位址失敗短路與雙棧訊號。"""
    dual_dns = _FakeDnsAdapter(ips=("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"))
    orch = nd_pipeline.DiagnosticOrchestrator(nd_policy.DefaultNetdiagPolicy(), dual_dns)
    tcp_ok = nd_pipeline.TcpStageData(status="ok", connect_ip="2606:2800:220:1:248:1893:25c8:1946", port=80, rtt_ms=2.0)
    http_ok = nd_pipeline.HttpStageData(status="ok", method="HEAD", final_status_code=200, ttfb_ms=5.0, total_ms=6.0)

    with (
        patch("mcp_servers.netdiag.pipeline._probe_tcp", new=AsyncMock(return_value=tcp_ok)) as mock_tcp,
        patch("mcp_servers.netdiag.pipeline.probe_http", new=AsyncMock(return_value=http_ok)),
    ):
        rep_v6 = asyncio.run(orch.run(target="http://example.com/", ip_family="ipv6"))

    assert rep_v6.first_failure_stage == "none"
    assert mock_tcp.await_args.args[0] == "2606:2800:220:1:248:1893:25c8:1946"
    assert any("IPv4=1" in sig and "IPv6=1" in sig for sig in rep_v6.signals)

    v4_only_dns = _FakeDnsAdapter(ips=("93.184.216.34",))
    orch_v4 = nd_pipeline.DiagnosticOrchestrator(nd_policy.DefaultNetdiagPolicy(), v4_only_dns)
    rep_fail = asyncio.run(orch_v4.run(target="http://example.com/", ip_family="ipv6"))
    assert rep_fail.first_failure_stage == "dns"
    assert rep_fail.runs[0].dns.status == "failed"


def test_dns_compare_queries_dmarc_subdomain_and_caa_signals():
    """驗證查詢 TXT,CAA 時自動查 _dmarc.<domain> 與彙整 CAA 治理訊號。"""

    class _GovDnsAdapter(_FakeDnsAdapter):
        async def query_records(self, domain, record_types, nameserver, timeout_sec):
            label = nameserver or "system_default"
            if domain == "_dmarc.corp.example.org":
                return ResolverQueryResult(
                    resolver_label=label,
                    status="ok",
                    records=(DnsRecordItem("TXT", 300, "v=DMARC1; p=reject; rua=mailto:sec@corp.example.org"),),
                    latency_ms=2.0,
                )
            return ResolverQueryResult(
                resolver_label=label,
                status="ok",
                records=(
                    DnsRecordItem("TXT", 300, "v=spf1 -all"),
                    DnsRecordItem("CAA", 300, '0 issue "letsencrypt.org"'),
                ),
                latency_ms=2.0,
            )

    from mcp_servers._shared import t

    orch = nd_dns_compare.DnsCompareOrchestrator(nd_policy.DefaultNetdiagPolicy(), _GovDnsAdapter())
    report = asyncio.run(orch.run(domain="corp.example.org", record_types="TXT,CAA", nameservers="1.1.1.1"))
    expected_dmarc = t("netdiag_mcp.dns_sig_dmarc", resolver="system_default", policy="p=reject")
    expected_caa = t(
        "netdiag_mcp.dns_sig_caa",
        resolver="system_default",
        values='0 issue "letsencrypt.org"',
    )
    assert expected_dmarc in report.mail_security_signals
    assert expected_caa in report.mail_security_signals


def test_signals_cert_expiring_weak_tls_security_headers_and_edge_fingerprints():
    """驗證臨期憑證、弱 TLS、HTTPS 安全標頭缺漏、Cookie 風險與可插拔邊緣指紋訊號。"""
    from mcp_servers.netdiag.models import HttpHop, HttpStageData, SampleRun
    from mcp_servers.netdiag.signals import derive_signals

    soon_cert = TlsCertInfo("CN=app.example.com", "CN=CA", "2026-01-01", "2026-12-10", 10, "AA", "11:22")
    run = SampleRun(
        sample_index=1,
        first_failure_stage="none",
        dns=DnsStageData(status="ok", ips=("93.184.216.34",), ipv4_ips=("93.184.216.34",)),
        tcp=nd_pipeline.TcpStageData(status="ok", connect_ip="93.184.216.34", port=443, rtt_ms=1.0),
        tls=TlsStageData(
            status="ok",
            verified=True,
            tls_version="TLSv1.1",
            cert_chain=(soon_cert,),
        ),
        http=HttpStageData(
            status="ok",
            hops=(
                HttpHop(
                    hop_index=0,
                    url="https://app.example.com/",
                    connect_ip="93.184.216.34",
                    http_version="HTTP/1.1",
                    status_code=200,
                    ttfb_ms=5.0,
                    total_ms=6.0,
                    bytes_read=0,
                    truncated=False,
                    headers={"server": "nginx/1.25", "x-akamai-transformed": "9 - 0 pmb=mRUM,1"},
                    set_cookies=("sid=[REDACTED] (Path=/)",),
                ),
            ),
        ),
    )
    sigs = derive_signals((run,), ProxyInfo())
    joined = "\n".join(sigs)
    assert "10" in joined
    assert "TLSv1.1" in joined
    assert "strict-transport-security" in joined
    assert "sid=[REDACTED]" in joined
    assert "Akamai" in joined
    assert "Nginx" in joined


def test_json_report_formatter_and_locale_switching():
    """驗證 output_format=json 輸出合法 JSON 與機密遮罩，及 en_US 語系切換一致。"""
    import json

    from mcp_servers.netdiag.factory import NetdiagServiceFactory
    from pocket_agent.system.i18n import set_locale

    fake_dns = _FakeDnsAdapter()
    with patch("mcp_servers.netdiag.factory.NetdiagServiceFactory.create_dns_adapter", return_value=fake_dns):
        out_json = asyncio.run(
            netdiag_mcp.netdiag_diagnose(
                target="https://example.com/?token=SECRET_XYZ",
                max_stage="dns",
                output_format="json",
            )
        )
    parsed = json.loads(out_json)
    assert parsed["first_failure_stage"] == "none"
    assert "SECRET_XYZ" not in out_json
    assert "token=[REDACTED]" in parsed["target_url"]

    try:
        set_locale("en_US")
        fmt_en = NetdiagServiceFactory.create_formatter("markdown")
        md_en = fmt_en.format_diagnostic(
            nd_pipeline.DiagnosticReport(
                observed_at_local="2026-11-26T17:00:00+08:00",
                observed_at_utc="2026-11-26T09:00:00+00:00",
                target_url="https://example.com/",
                target_scope="domain",
                host_header="",
                max_stage="dns",
                method="HEAD",
                samples_requested=1,
                proxy_info=ProxyInfo(),
                first_failure_stage="none",
                runs=(),
            )
        )
        assert "Single local vantage point only" in md_en
    finally:
        set_locale("zh_TW")
