"""網站與網路診斷編排管線（Stage 0→4 短路執行、採樣統計與稽核記錄）。"""

import asyncio
import time
from datetime import datetime, timezone

from mcp_servers._shared import CONSTANTS, AuditLogger, t
from mcp_servers.netdiag.dns_adapter import ALLOWED_DNS_RECORD_TYPES, DnsResolverAdapter
from mcp_servers.netdiag.http_probe import probe_http
from mcp_servers.netdiag.models import (
    DiagnosticReport,
    DnsCompareReport,
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
from mcp_servers.netdiag.tls_probe import probe_tls

AUDIT = AuditLogger("netdiag", redaction_policy=NETDIAG_REDACTION)
_VALID_STAGES = frozenset({"dns", "tcp", "tls", "http"})
_VALID_METHODS = frozenset({"HEAD", "GET"})


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


def _derive_signals(runs: tuple[SampleRun, ...], proxy_info: ProxyInfo) -> tuple[str, ...]:
    signals: list[str] = []
    if proxy_info.used:
        signals.append(f"Traffic routed through configured proxy ({proxy_info.display_proxy}).")
    for run in runs:
        if run.tls.status == "ok":
            if run.tls.truststore_verified and run.tls.certifi_verified is False:
                signals.append(
                    "Trusted by OS store (truststore) but rejected by public Mozilla CA (certifi): "
                    "likely internal Enterprise CA or corporate SSL inspection."
                )
        for hop in run.http.hops:
            hdrs = hop.headers
            if "cf-ray" in hdrs or "cf-cache-status" in hdrs:
                cf_status = hdrs.get("cf-cache-status", "n/a")
                signals.append(f"Cloudflare edge headers observed (cf-cache-status={cf_status}).")
            if "x-amz-cf-id" in hdrs or "x-amz-cf-pop" in hdrs:
                signals.append(f"AWS CloudFront headers observed (pop={hdrs.get('x-amz-cf-pop', 'n/a')}).")
            if "server" in hdrs:
                signals.append(f"Response Server header observed: {hdrs['server']}.")
            if "x-cache" in hdrs:
                signals.append(f"Cache indicator observed: x-cache={hdrs['x-cache']}.")
        if run.http.redirect_blocked_reason:
            signals.append(f"Redirect target blocked by policy: {run.http.redirect_blocked_reason}.")
    return tuple(dict.fromkeys(signals))


def _derive_escalation_hint(first_failure: str, runs: tuple[SampleRun, ...], proxy_info: ProxyInfo) -> str:
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
        samples: int = 1,
        timeout_sec: float | None = None,
    ) -> DiagnosticReport:
        local_ts, utc_ts = _utc_and_local_iso()
        safe_url = self.redaction.redact_url(target)
        effective_timeout = self.policy.config.default_timeout_sec if timeout_sec is None else float(timeout_sec)

        stage_clean = (max_stage or "http").strip().lower()
        method_clean = (method or "HEAD").strip().upper()
        if stage_clean not in _VALID_STAGES:
            raise ValueError(t("netdiag_mcp.invalid_stage", max_stage=max_stage))
        if method_clean not in _VALID_METHODS:
            raise ValueError(t("netdiag_mcp.invalid_method", method=method))
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
            f"stage={stage_clean} method={method_clean} samples={samples}",
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
            signals=_derive_signals(runs_tuple, proxy_info),
            escalation_hint=_derive_escalation_hint(first_fail, runs_tuple, proxy_info),
        )

    async def _execute_single_sample(
        self,
        sample_index: int,
        target: ValidatedTarget,
        proxy_info: ProxyInfo,
        max_stage: str,
        method: str,
        timeout_sec: float,
    ) -> SampleRun:
        t0 = time.perf_counter()
        not_exec_tcp = TcpStageData(status="not_executed", port=target.port)
        not_exec_tls = TlsStageData(status="not_executed")
        not_exec_http = HttpStageData(status="not_executed", method=method)

        # Stage 1: DNS
        pinned_ip: str | None = None
        if target.is_ip_literal:
            pinned_ip = target.hostname
            dns_data = DnsStageData(
                status="skipped",
                ips=(target.hostname,),
                pinned_ip=target.hostname,
                error="ip_literal",
            )
        elif proxy_info.used:
            dns_data = DnsStageData(status="skipped", error="via_proxy")
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
                    cname_chain=dns_data.cname_chain,
                    ttl=dns_data.ttl,
                    latency_ms=dns_data.latency_ms,
                    fallback_used=dns_data.fallback_used,
                    error=self.redaction.redact(str(exc)),
                )
                return SampleRun(sample_index, "dns", failed_dns, not_exec_tcp, not_exec_tls, not_exec_http)
            pinned_ip = valid_ips[0]
            dns_data = DnsStageData(
                status="ok",
                ips=valid_ips,
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
        return SampleRun(
            sample_index,
            first_fail,
            dns_data,
            tcp_data,
            tls_data,
            http_data,
            round((time.perf_counter() - t0) * 1000.0, 2),
        )


class DnsCompareOrchestrator:
    """協調多解析器 DNS 比對與郵件安全／CAA 訊號分析。"""

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
        domain: str,
        record_types: str = "A,AAAA,CNAME",
        nameservers: str = "",
        timeout_sec: float | None = None,
    ) -> DnsCompareReport:
        local_ts, utc_ts = _utc_and_local_iso()
        effective_timeout = self.policy.config.default_timeout_sec if timeout_sec is None else float(timeout_sec)
        parsed_types = tuple(dict.fromkeys(rt.strip().upper() for rt in record_types.split(",") if rt.strip()))
        if not parsed_types or any(rt not in ALLOWED_DNS_RECORD_TYPES for rt in parsed_types):
            raise ValueError(t("netdiag_mcp.invalid_record_types", record_types=record_types))
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
            clean_domain = self.policy.validate_domain_preflight(domain)
            validated_ns = self.policy.validate_nameservers(nameservers)
        except PolicyError as exc:
            err_msg = self.redaction.redact(str(exc))
            safe_domain = self.redaction.redact(domain.strip()) or "<invalid-domain>"
            AUDIT.log(
                "netdiag_dns_compare",
                f"types={','.join(parsed_types)}",
                target=safe_domain,
                status="BLOCKED",
                detail=err_msg,
            )
            return DnsCompareReport(
                observed_at_local=local_ts,
                observed_at_utc=utc_ts,
                domain=safe_domain,
                record_types=parsed_types,
                fallback_used=False,
                results=(),
                discrepancy_summary="",
                preflight_error=err_msg,
            )

        targets: list[str | None] = [None, *validated_ns]
        coros = [self.dns_adapter.query_records(clean_domain, parsed_types, ns, effective_timeout) for ns in targets]
        results = tuple(await asyncio.gather(*coros))
        fallback_used = type(self.dns_adapter).__name__ == "SocketFallbackResolverAdapter"

        answer_sets: dict[str, set[str]] = {}
        mail_signals: list[str] = []
        failed_resolvers: list[str] = []
        for res in results:
            if res.status != "ok":
                failed_resolvers.append(f"{res.resolver_label}({res.status})")
                continue
            vals = {f"{rec.record_type}:{rec.value}" for rec in res.records}
            answer_sets[res.resolver_label] = vals
            for rec in res.records:
                if rec.record_type == "TXT" and "v=spf1" in rec.value.lower():
                    mail_signals.append(f"SPF record found on {res.resolver_label}")
                if rec.record_type == "TXT" and "v=dmarc1" in rec.value.lower():
                    mail_signals.append(f"DMARC record found on {res.resolver_label}")

        unique_sets = {frozenset(v) for v in answer_sets.values()}
        if len(answer_sets) < 2:
            suffix = f" ({', '.join(failed_resolvers)})." if failed_resolvers else "."
            discrepancy = f"Unable to compare resolvers: fewer than 2 resolvers returned complete 'ok' results{suffix}"
        elif len(unique_sets) > 1:
            discrepancy = "Discrepancy detected across resolvers (possible Split-Horizon DNS or propagation delay)."
        else:
            discrepancy = "All queried resolvers returned consistent answer sets."

        if not answer_sets:
            audit_status = "FAILED"
        elif failed_resolvers:
            audit_status = "PARTIAL"
        else:
            audit_status = "OK"

        AUDIT.log(
            "netdiag_dns_compare",
            f"types={','.join(parsed_types)} ns={len(targets)}",
            target=clean_domain,
            status=audit_status,
        )
        return DnsCompareReport(
            observed_at_local=local_ts,
            observed_at_utc=utc_ts,
            domain=clean_domain,
            record_types=parsed_types,
            fallback_used=fallback_used,
            results=results,
            discrepancy_summary=discrepancy,
            mail_security_signals=tuple(dict.fromkeys(mail_signals)),
        )
