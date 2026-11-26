"""網站與網路診斷編排管線（Stage 0→4 短路執行、雙棧 IP 過濾、採樣統計與稽核記錄）。"""

import asyncio
import time
from datetime import datetime, timezone

from mcp_servers._shared import CONSTANTS, AuditLogger, t
from mcp_servers.netdiag.dns_adapter import DnsResolverAdapter
from mcp_servers.netdiag.http_probe import probe_http
from mcp_servers.netdiag.models import (
    DiagnosticReport,
    DnsStageData,
    HttpStageData,
    ProxyInfo,
    SampleRun,
    TcpStageData,
    TlsStageData,
    ValidatedTarget,
)
from mcp_servers.netdiag.policy import NetworkAccessPolicy, PolicyError
from mcp_servers.netdiag.redaction import NETDIAG_REDACTION, NetdiagRedactionPolicy
from mcp_servers.netdiag.signals import derive_escalation_hint, derive_signals
from mcp_servers.netdiag.tls_probe import probe_tls

AUDIT = AuditLogger("netdiag", redaction_policy=NETDIAG_REDACTION)
_VALID_STAGES = frozenset({"dns", "tcp", "tls", "http"})
_VALID_METHODS = frozenset({"HEAD", "GET"})
_VALID_IP_FAMILIES = frozenset({"auto", "ipv4", "ipv6"})


def _utc_and_local_iso() -> tuple[str, str]:
    now_local = datetime.now().astimezone()
    now_utc = now_local.astimezone(timezone.utc)
    return now_local.isoformat(timespec="seconds"), now_utc.isoformat(timespec="seconds")


async def _probe_tcp(connect_ip: str, port: int, timeout_sec: float) -> TcpStageData:
    start = time.perf_counter()
    writer: asyncio.StreamWriter | None = None
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host=connect_ip, port=port),
            timeout=timeout_sec,
        )
        rtt = round((time.perf_counter() - start) * 1000.0, 2)
        return TcpStageData(status="ok", connect_ip=connect_ip, port=port, rtt_ms=rtt)
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        return TcpStageData(
            status="failed",
            connect_ip=connect_ip,
            port=port,
            error_kind="timeout",
            error=f"TCP connection timed out after {timeout_sec}s",
        )
    except ConnectionRefusedError as exc:
        return TcpStageData(
            status="failed",
            connect_ip=connect_ip,
            port=port,
            error_kind="connection_refused",
            error=f"ConnectionRefusedError: {exc}",
        )
    except OSError as exc:
        return TcpStageData(
            status="failed",
            connect_ip=connect_ip,
            port=port,
            error_kind="os_error",
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        if writer is not None:
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
            except Exception:
                pass


class DiagnosticOrchestrator:
    """協調 Stage 0→4 診斷流水線並記錄稽核軌跡。"""

    def __init__(
        self,
        policy: NetworkAccessPolicy,
        dns_adapter: DnsResolverAdapter,
        redaction: NetdiagRedactionPolicy = NETDIAG_REDACTION,
    ) -> None:
        self.policy = policy
        self.dns_adapter = dns_adapter
        self.redaction = redaction

    async def run(
        self,
        *,
        target: str,
        max_stage: str = "http",
        method: str = "HEAD",
        host_header: str = "",
        ip_family: str = "auto",
        samples: int = 1,
        timeout_sec: float | None = None,
    ) -> DiagnosticReport:
        local_ts, utc_ts = _utc_and_local_iso()
        safe_url = self.redaction.redact_url(target)
        effective_timeout = self.policy.config.default_timeout_sec if timeout_sec is None else float(timeout_sec)

        stage_clean = (max_stage or "http").strip().lower()
        method_clean = (method or "HEAD").strip().upper()
        family_clean = (ip_family or "auto").strip().lower()
        if stage_clean not in _VALID_STAGES:
            raise ValueError(t("netdiag_mcp.invalid_stage", max_stage=max_stage))
        if method_clean not in _VALID_METHODS:
            raise ValueError(t("netdiag_mcp.invalid_method", method=method))
        if family_clean not in _VALID_IP_FAMILIES:
            raise ValueError(t("netdiag_mcp.invalid_ip_family", ip_family=ip_family))
        if (
            isinstance(samples, bool)
            or not isinstance(samples, int)
            or not (1 <= samples <= CONSTANTS.netdiag_max_samples)
        ):
            raise ValueError(
                t("netdiag_mcp.invalid_samples", samples=samples, max_samples=CONSTANTS.netdiag_max_samples)
            )
        if not (CONSTANTS.netdiag_min_timeout_sec <= effective_timeout <= CONSTANTS.netdiag_max_timeout_sec):
            raise ValueError(
                t(
                    "netdiag_mcp.invalid_timeout",
                    timeout=effective_timeout,
                    min_sec=CONSTANTS.netdiag_min_timeout_sec,
                    max_sec=CONSTANTS.netdiag_max_timeout_sec,
                )
            )

        try:
            validated = self.policy.validate_target_preflight(target, host_header=host_header)
            proxy_info = self.policy.detect_proxy(validated.url)
        except PolicyError as exc:
            err_msg = self.redaction.redact(str(exc))
            AUDIT.log(
                "netdiag_diagnose",
                f"stage={stage_clean} method={method_clean}",
                target=safe_url,
                status="BLOCKED",
                detail=err_msg,
            )
            return DiagnosticReport(
                observed_at_local=local_ts,
                observed_at_utc=utc_ts,
                target_url=safe_url,
                target_scope="blocked",
                host_header=host_header.strip(),
                max_stage=stage_clean,
                method=method_clean,
                samples_requested=samples,
                proxy_info=ProxyInfo(),
                first_failure_stage="preflight",
                runs=(),
                ip_family=family_clean,
                preflight_error=err_msg,
            )

        runs: list[SampleRun] = []
        for idx in range(1, samples + 1):
            run = await self._execute_single_sample(
                idx,
                validated,
                proxy_info,
                stage_clean,
                method_clean,
                family_clean,
                effective_timeout,
            )
            runs.append(run)

        runs_tuple = tuple(runs)
        first_fail = "none"
        for run in runs_tuple:
            if run.first_failure_stage != "none":
                first_fail = run.first_failure_stage
                break

        audit_status = "OK" if first_fail == "none" else "FAILED"
        AUDIT.log(
            "netdiag_diagnose",
            f"stage={stage_clean} method={method_clean} family={family_clean} samples={samples}",
            target=safe_url,
            status=audit_status,
            detail=f"first_failure_stage={first_fail}",
        )
        return DiagnosticReport(
            observed_at_local=local_ts,
            observed_at_utc=utc_ts,
            target_url=safe_url,
            target_scope=validated.scope_label,
            host_header=validated.host_header,
            max_stage=stage_clean,
            method=method_clean,
            samples_requested=samples,
            proxy_info=proxy_info,
            first_failure_stage=first_fail,
            runs=runs_tuple,
            ip_family=family_clean,
            signals=derive_signals(runs_tuple, proxy_info),
            escalation_hint=derive_escalation_hint(first_fail, runs_tuple, proxy_info),
        )

    async def _execute_single_sample(
        self,
        sample_index: int,
        target: ValidatedTarget,
        proxy_info: ProxyInfo,
        max_stage: str,
        method: str,
        ip_family: str,
        timeout_sec: float,
    ) -> SampleRun:
        t0 = time.perf_counter()
        not_exec_tcp = TcpStageData(status="not_executed", port=target.port)
        not_exec_tls = TlsStageData(status="not_executed")
        not_exec_http = HttpStageData(status="not_executed", method=method)

        # Stage 1: DNS
        pinned_ip: str | None = None
        if target.is_ip_literal:
            is_v6 = ":" in target.hostname
            v4 = () if is_v6 else (target.hostname,)
            v6 = (target.hostname,) if is_v6 else ()
            if (ip_family == "ipv4" and is_v6) or (ip_family == "ipv6" and not is_v6):
                dns_data = DnsStageData(
                    status="failed",
                    ips=(target.hostname,),
                    ipv4_ips=v4,
                    ipv6_ips=v6,
                    ip_family_requested=ip_family,
                    error=t("netdiag_mcp.no_ip_for_family", ip_family=ip_family),
                )
                return SampleRun(sample_index, "dns", dns_data, not_exec_tcp, not_exec_tls, not_exec_http)
            pinned_ip = target.hostname
            dns_data = DnsStageData(
                status="skipped",
                ips=(target.hostname,),
                ipv4_ips=v4,
                ipv6_ips=v6,
                ip_family_requested=ip_family,
                pinned_ip=target.hostname,
                error="ip_literal",
            )
        elif proxy_info.used:
            dns_data = DnsStageData(status="skipped", ip_family_requested=ip_family, error="via_proxy")
        else:
            dns_data = await self.dns_adapter.resolve_host_ips(target.hostname, timeout_sec)
            if dns_data.status != "ok" or not dns_data.ips:
                return SampleRun(sample_index, "dns", dns_data, not_exec_tcp, not_exec_tls, not_exec_http)
            try:
                valid_ips = tuple(self.policy.validate_ip(ip) for ip in dns_data.ips)
            except PolicyError as exc:
                failed_dns = DnsStageData(
                    status="failed",
                    ips=dns_data.ips,
                    ip_family_requested=ip_family,
                    cname_chain=dns_data.cname_chain,
                    ttl=dns_data.ttl,
                    latency_ms=dns_data.latency_ms,
                    fallback_used=dns_data.fallback_used,
                    error=self.redaction.redact(str(exc)),
                )
                return SampleRun(sample_index, "dns", failed_dns, not_exec_tcp, not_exec_tls, not_exec_http)

            v4 = tuple(ip for ip in valid_ips if ":" not in ip)
            v6 = tuple(ip for ip in valid_ips if ":" in ip)
            selected = v4 if ip_family == "ipv4" else v6 if ip_family == "ipv6" else valid_ips
            if not selected:
                no_family_dns = DnsStageData(
                    status="failed",
                    ips=valid_ips,
                    ipv4_ips=v4,
                    ipv6_ips=v6,
                    ip_family_requested=ip_family,
                    cname_chain=dns_data.cname_chain,
                    ttl=dns_data.ttl,
                    latency_ms=dns_data.latency_ms,
                    fallback_used=dns_data.fallback_used,
                    error=t("netdiag_mcp.no_ip_for_family", ip_family=ip_family),
                )
                return SampleRun(sample_index, "dns", no_family_dns, not_exec_tcp, not_exec_tls, not_exec_http)

            pinned_ip = selected[0]
            dns_data = DnsStageData(
                status="ok",
                ips=valid_ips,
                ipv4_ips=v4,
                ipv6_ips=v6,
                ip_family_requested=ip_family,
                pinned_ip=pinned_ip,
                cname_chain=dns_data.cname_chain,
                ttl=dns_data.ttl,
                latency_ms=dns_data.latency_ms,
                fallback_used=dns_data.fallback_used,
            )

        if max_stage == "dns":
            elapsed = round((time.perf_counter() - t0) * 1000.0, 2)
            return SampleRun(sample_index, "none", dns_data, not_exec_tcp, not_exec_tls, not_exec_http, elapsed)

        # Stage 2: TCP
        if proxy_info.used:
            tcp_data = TcpStageData(status="skipped", port=target.port, error="via_proxy")
        else:
            assert pinned_ip is not None
            tcp_data = await _probe_tcp(pinned_ip, target.port, timeout_sec)
            if tcp_data.status != "ok":
                return SampleRun(sample_index, "tcp", dns_data, tcp_data, not_exec_tls, not_exec_http)

        if max_stage == "tcp":
            elapsed = round((time.perf_counter() - t0) * 1000.0, 2)
            return SampleRun(sample_index, "none", dns_data, tcp_data, not_exec_tls, not_exec_http, elapsed)

        # Stage 3: TLS
        if target.scheme != "https":
            tls_data = TlsStageData(status="skipped", error="plaintext_http")
        elif proxy_info.used:
            tls_data = TlsStageData(status="skipped", error="via_proxy")
        else:
            assert pinned_ip is not None
            sni_host = target.host_header or ("" if target.is_ip_literal else target.hostname)
            tls_data = await probe_tls(pinned_ip, target.port, sni_host, timeout_sec)
            if not tls_data.verified:
                return SampleRun(sample_index, "tls", dns_data, tcp_data, tls_data, not_exec_http)

        if max_stage == "tls":
            elapsed = round((time.perf_counter() - t0) * 1000.0, 2)
            return SampleRun(sample_index, "none", dns_data, tcp_data, tls_data, not_exec_http, elapsed)

        # Stage 4: HTTP
        http_data = await probe_http(
            target,
            pinned_ip,
            method,
            self.policy,
            self.dns_adapter,
            self.redaction,
            proxy_info,
            tls_data.preferred_verify_source,
            timeout_sec,
        )
        first_fail = "http" if http_data.status != "ok" else "none"
        if http_data.redirect_tls is not None and not http_data.redirect_tls.verified:
            tls_data = http_data.redirect_tls
            first_fail = "tls"
        return SampleRun(
            sample_index,
            first_fail,
            dns_data,
            tcp_data,
            tls_data,
            http_data,
            round((time.perf_counter() - t0) * 1000.0, 2),
        )
