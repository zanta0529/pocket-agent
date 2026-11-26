"""跨單位舉證報告格式化器（Strategy Pattern）。"""

import json
import statistics
from abc import ABC, abstractmethod
from dataclasses import asdict

from mcp_servers._shared import CONSTANTS, t
from mcp_servers.netdiag.models import DiagnosticReport, DnsCompareReport, SampleRun, TlsCertInfo
from mcp_servers.netdiag.redaction import NETDIAG_REDACTION, NetdiagRedactionPolicy


class ReportFormatter(ABC):
    """診斷報告格式化抽象策略。"""

    @abstractmethod
    def format_diagnostic(self, report: DiagnosticReport) -> str:
        """格式化全階段診斷報告。"""

    @abstractmethod
    def format_dns_compare(self, report: DnsCompareReport) -> str:
        """格式化多解析器 DNS 比對報告。"""


class EscalationMarkdownFormatter(ReportFormatter):
    """產出直接觀察／推論訊號／觀測限制／協查指引分層的 Markdown 報告。"""

    def __init__(self, redaction: NetdiagRedactionPolicy = NETDIAG_REDACTION) -> None:
        self.redaction = redaction

    def format_diagnostic(self, report: DiagnosticReport) -> str:
        if report.preflight_error:
            return self.redaction.redact(f"[ERROR] {report.preflight_error}")

        options_line = f"- **Options**: `max_stage={report.max_stage}`, `method={report.method}`, "
        if report.ip_family != "auto":
            options_line += f"`ip_family={report.ip_family}`, "
        options_line += f"`samples={report.samples_requested}`"

        lines: list[str] = [
            "# NetDiag Escalation Report",
            "",
            "## Observation Metadata",
            f"- **Observed At (Local)**: {report.observed_at_local}",
            f"- **Observed At (UTC)**: {report.observed_at_utc}",
            f"- **Target**: {report.target_url} (`scope={report.target_scope}`)",
            options_line,
        ]
        if report.host_header:
            lines.append(f"- **Host / SNI Override**: `{report.host_header}`")
        proxy_line = (
            f"- **Proxy**: `{report.proxy_info.display_proxy}`"
            if report.proxy_info.used
            else "- **Proxy**: `none (direct)`"
        )
        lines.append(proxy_line)
        lines.extend(["", f"## First Failure Stage: `{report.first_failure_stage}`", ""])

        if report.runs:
            primary_run = self._select_primary_run(report.runs)
            lines.extend(self._format_run_details(primary_run))
            if len(report.runs) > 1:
                lines.extend(self._format_sample_stats(report.runs))

        if report.signals:
            lines.extend(["", "## Diagnostic Signals (Heuristic — Not Proof)"])
            lines.extend(f"- {sig}" for sig in report.signals)

        lines.extend(
            [
                "",
                "## Limitations",
                f"- {t('netdiag_mcp.limit_vantage')}",
                f"- {t('netdiag_mcp.limit_head_vs_get')}",
                f"- {t('netdiag_mcp.limit_no_internal_access')}",
            ]
        )
        if report.proxy_info.used:
            lines.append(f"- {t('netdiag_mcp.limit_proxy_mode')}")
        if report.escalation_hint:
            lines.extend(["", "## Escalation Hints", f"- {report.escalation_hint}"])

        raw_out = self.redaction.redact("\n".join(lines))
        clipped, _ = self.redaction.truncate_output(
            raw_out,
            max_chars=CONSTANTS.max_output_chars,
            hint=t("netdiag_mcp.truncate_hint"),
        )
        return clipped

    @staticmethod
    def _select_primary_run(runs: tuple[SampleRun, ...]) -> SampleRun:
        """優先挑選第一筆失敗採樣作為主明細，避免多採樣時頂部失敗與明細矛盾。"""
        for run in runs:
            if run.first_failure_stage != "none":
                return run
        return runs[0]

    @staticmethod
    def _append_cert_chain(lines: list[str], title: str, cert_chain: tuple[TlsCertInfo, ...]) -> None:
        lines.append(title)
        for idx, cert in enumerate(cert_chain):
            lines.append(
                f"- **[{idx}]** Subject: `{cert.subject}` | Issuer: `{cert.issuer}` | "
                f"Valid: `{cert.not_before}` .. `{cert.not_after}` (`days_remaining={cert.days_remaining}`) | "
                f"Serial: `{cert.serial_number}` | SHA-256: `{cert.sha256_fingerprint}`"
            )
            if cert.san:
                lines.append(f"  - SAN: {', '.join(cert.san)}")

    def _format_run_details(self, run: SampleRun) -> list[str]:
        lines = [
            "| Stage | Status | Detail |",
            "| :--- | :--- | :--- |",
            f"| DNS | `{run.dns.status}` | {self._dns_summary(run)} |",
            f"| TCP | `{run.tcp.status}` | {self._tcp_summary(run)} |",
            f"| TLS | `{run.tls.status}` | {self._tls_summary(run)} |",
            f"| HTTP | `{run.http.status}` | {self._http_summary(run)} |",
            "",
            "## Observed Facts",
        ]
        if run.tls.cert_chain:
            self._append_cert_chain(lines, "### TLS Certificate Chain", run.tls.cert_chain)
        elif run.http.redirect_tls is not None and run.http.redirect_tls.cert_chain:
            self._append_cert_chain(
                lines,
                "### Redirect-Upgraded HTTPS Certificate Chain",
                run.http.redirect_tls.cert_chain,
            )

        if run.http.hops:
            lines.append("### HTTP Hops & Filtered Headers")
            for hop in run.http.hops:
                lines.append(
                    f"- **Hop {hop.hop_index}**: `{hop.status_code}` `{hop.http_version}` -> `{hop.url}` "
                    f"(ip=`{hop.connect_ip}`, ttfb=`{hop.ttfb_ms}ms`, "
                    f"total=`{hop.total_ms}ms`, bytes=`{hop.bytes_read}`)"
                )
                if hop.headers:
                    hdr_str = ", ".join(f"`{k}: {v}`" for k, v in sorted(hop.headers.items()))
                    lines.append(f"  - Headers: {hdr_str}")
                if hop.set_cookies:
                    lines.append(f"  - Set-Cookie Flags: {'; '.join(hop.set_cookies)}")
        return lines

    def _format_sample_stats(self, runs: tuple[SampleRun, ...]) -> list[str]:
        ok_count = sum(1 for r in runs if r.first_failure_stage == "none")
        totals = [r.total_ms for r in runs if r.total_ms is not None]
        ttfbs = [r.http.ttfb_ms for r in runs if r.http.ttfb_ms is not None]
        lines = [
            "",
            "### Multi-Sample Summary",
            f"- **Success Rate**: {ok_count}/{len(runs)}",
        ]
        for run in runs:
            if run.first_failure_stage == "none":
                lines.append(f"  - Sample #{run.sample_index}: `ok` (total=`{run.total_ms}ms`)")
            else:
                lines.append(f"  - Sample #{run.sample_index}: `failed` at `{run.first_failure_stage}`")
        if totals:
            lines.append(
                f"- **Total Time (ms)**: min={min(totals):.2f}, "
                f"median={statistics.median(totals):.2f}, max={max(totals):.2f}"
            )
        if ttfbs:
            lines.append(
                f"- **TTFB (ms)**: min={min(ttfbs):.2f}, median={statistics.median(ttfbs):.2f}, max={max(ttfbs):.2f}"
            )
        return lines

    @staticmethod
    def _dns_summary(run: SampleRun) -> str:
        if run.dns.status == "skipped":
            return f"skipped ({run.dns.error})"
        if run.dns.status == "failed":
            return run.dns.error
        cname = f" cname={' -> '.join(run.dns.cname_chain)}" if run.dns.cname_chain else ""
        ttl = f" ttl={run.dns.ttl}s" if run.dns.ttl is not None else ""
        return f"ips={list(run.dns.ips)}, pinned={run.dns.pinned_ip}{ttl}{cname} ({run.dns.latency_ms}ms)"

    @staticmethod
    def _tcp_summary(run: SampleRun) -> str:
        if run.tcp.status in ("skipped", "not_executed"):
            return run.tcp.error or run.tcp.status
        if run.tcp.status == "failed":
            return f"{run.tcp.error_kind}: {run.tcp.error}"
        return f"connected {run.tcp.connect_ip}:{run.tcp.port} ({run.tcp.rtt_ms}ms)"

    @staticmethod
    def _tls_summary(run: SampleRun) -> str:
        if run.tls.status in ("skipped", "not_executed"):
            return run.tls.error or run.tls.status
        trust = f"truststore={run.tls.truststore_verified}, certifi={run.tls.certifi_verified}"
        if not run.tls.verified:
            inspected = run.tls.inspected_without_verification
            return f"tls_verified=false ({trust}, inspected_without_verification={inspected}): {run.tls.error}"
        return (
            f"tls_verified=true ({run.tls.tls_version}, {run.tls.cipher_suite}, "
            f"alpn={run.tls.alpn_protocol or 'none'}, {trust}, {run.tls.latency_ms}ms)"
        )

    @staticmethod
    def _http_summary(run: SampleRun) -> str:
        if run.http.status in ("skipped", "not_executed"):
            return run.http.status
        if run.http.status == "failed":
            return run.http.error or "failed"
        return f"HTTP {run.http.final_status_code} ({len(run.http.hops)} hop(s), ttfb={run.http.ttfb_ms}ms)"

    def format_dns_compare(self, report: DnsCompareReport) -> str:
        if report.preflight_error:
            return self.redaction.redact(f"[ERROR] {report.preflight_error}")
        lines: list[str] = [
            "# NetDiag Multi-Resolver DNS Comparison",
            "",
            f"- **Observed At (Local)**: {report.observed_at_local}",
            f"- **Observed At (UTC)**: {report.observed_at_utc}",
            f"- **Domain**: `{report.domain}` | **Types**: `{','.join(report.record_types)}`",
            f"- **Comparison**: {report.discrepancy_summary}",
        ]
        if report.fallback_used:
            lines.append(f"- **Notice**: {t('netdiag_mcp.dnspython_missing_fallback')}")

        lines.extend(["", "## Resolver Results"])
        for res in report.results:
            lines.append(f"### Resolver `{res.resolver_label}` (`status={res.status}`, `{res.latency_ms}ms`)")
            if res.error:
                lines.append(f"- Errors: `{res.error}`")
            if res.cname_chain:
                lines.append(f"- CNAME Chain: {' -> '.join(res.cname_chain)}")
            for rec in res.records:
                ttl_str = f"TTL={rec.ttl}s" if rec.ttl is not None else "TTL=n/a"
                lines.append(f"- `{rec.record_type}` ({ttl_str}): `{rec.value}`")

        if report.mail_security_signals:
            lines.extend(["", "## Mail Security Signals"])
            lines.extend(f"- {sig}" for sig in report.mail_security_signals)

        raw_out = self.redaction.redact("\n".join(lines))
        clipped, _ = self.redaction.truncate_output(
            raw_out,
            max_chars=CONSTANTS.max_output_chars,
            hint=t("netdiag_mcp.truncate_hint"),
        )
        return clipped


class JsonReportFormatter(ReportFormatter):
    """產出結構化 JSON 格式診斷報告（便於自動化比對與多端點彙整）。"""

    def __init__(self, redaction: NetdiagRedactionPolicy = NETDIAG_REDACTION) -> None:
        self.redaction = redaction

    def _serialize(self, payload: dict) -> str:
        raw_out = self.redaction.redact(json.dumps(payload, ensure_ascii=False, indent=2))
        clipped, _ = self.redaction.truncate_output(
            raw_out,
            max_chars=CONSTANTS.max_output_chars,
            hint=t("netdiag_mcp.truncate_hint"),
        )
        return clipped

    def format_diagnostic(self, report: DiagnosticReport) -> str:
        return self._serialize(asdict(report))

    def format_dns_compare(self, report: DnsCompareReport) -> str:
        return self._serialize(asdict(report))
