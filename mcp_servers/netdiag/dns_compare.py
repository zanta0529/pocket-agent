"""多解析器 DNS 比對與郵件／CAA 安全紀錄編排器。"""

import asyncio
from datetime import datetime, timezone

from mcp_servers._shared import CONSTANTS, t
from mcp_servers.netdiag.dns_adapter import ALLOWED_DNS_RECORD_TYPES, DnsResolverAdapter
from mcp_servers.netdiag.models import DnsCompareReport, ResolverQueryResult
from mcp_servers.netdiag.policy import NetworkAccessPolicy, PolicyError
from mcp_servers.netdiag.redaction import NETDIAG_REDACTION, NetdiagRedactionPolicy


def _utc_and_local_iso() -> tuple[str, str]:
    now_local = datetime.now().astimezone()
    now_utc = now_local.astimezone(timezone.utc)
    return now_local.isoformat(timespec="seconds"), now_utc.isoformat(timespec="seconds")


def _extract_dmarc_policy(txt_val: str) -> str:
    for part in txt_val.split(";"):
        clean = part.strip()
        if clean.lower().startswith("p="):
            return clean
    return "p=unknown"


class DnsCompareOrchestrator:
    """協調多解析器 DNS 比對與 SPF／_dmarc／CAA 治理訊號分析。"""

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
        from mcp_servers.netdiag.pipeline import AUDIT

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
        fallback_used = self.dns_adapter.is_fallback
        coros = [self.dns_adapter.query_records(clean_domain, parsed_types, ns, effective_timeout) for ns in targets]

        query_dmarc = "TXT" in parsed_types and not fallback_used and not clean_domain.startswith("_dmarc.")
        dmarc_coros = (
            [
                self.dns_adapter.query_records(f"_dmarc.{clean_domain}", ("TXT",), ns, effective_timeout)
                for ns in targets
            ]
            if query_dmarc
            else []
        )

        all_res = await asyncio.gather(*coros, *dmarc_coros)
        results: tuple[ResolverQueryResult, ...] = tuple(all_res[: len(coros)])
        dmarc_results: tuple[ResolverQueryResult, ...] = tuple(all_res[len(coros) :])

        answer_sets: dict[str, set[str]] = {}
        mail_signals: list[str] = []
        failed_resolvers: list[str] = []
        for res in results:
            if res.status != "ok":
                failed_resolvers.append(f"{res.resolver_label}({res.status})")
                continue
            vals = {f"{rec.record_type}:{rec.value}" for rec in res.records}
            answer_sets[res.resolver_label] = vals
            caa_vals: list[str] = []
            for rec in res.records:
                if rec.record_type == "TXT" and "v=spf1" in rec.value.lower():
                    mail_signals.append(t("netdiag_mcp.dns_sig_spf", resolver=res.resolver_label))
                if rec.record_type == "TXT" and "v=dmarc1" in rec.value.lower():
                    mail_signals.append(
                        t(
                            "netdiag_mcp.dns_sig_dmarc",
                            resolver=res.resolver_label,
                            policy=_extract_dmarc_policy(rec.value),
                        )
                    )
                if rec.record_type == "CAA":
                    caa_vals.append(rec.value)
            if caa_vals:
                mail_signals.append(
                    t("netdiag_mcp.dns_sig_caa", resolver=res.resolver_label, values=", ".join(caa_vals))
                )

        if dmarc_results:
            dmarc_found = False
            for d_res in dmarc_results:
                if d_res.status != "ok":
                    continue
                for rec in d_res.records:
                    if rec.record_type == "TXT" and "v=dmarc1" in rec.value.lower():
                        dmarc_found = True
                        mail_signals.append(
                            t(
                                "netdiag_mcp.dns_sig_dmarc",
                                resolver=d_res.resolver_label,
                                policy=_extract_dmarc_policy(rec.value),
                            )
                        )
            if not dmarc_found:
                mail_signals.append(t("netdiag_mcp.dns_sig_dmarc_missing", domain=clean_domain))

        unique_sets = {frozenset(v) for v in answer_sets.values()}
        if len(answer_sets) < 2:
            suffix = f" ({', '.join(failed_resolvers)})." if failed_resolvers else "."
            discrepancy = t("netdiag_mcp.dns_compare_insufficient", suffix=suffix)
        elif len(unique_sets) > 1:
            discrepancy = t("netdiag_mcp.dns_compare_discrepancy")
        else:
            discrepancy = t("netdiag_mcp.dns_compare_consistent")

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
