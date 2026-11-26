"""NetDiag 服務組裝工廠與統一參數稽核輔助（Factory Pattern）。"""

from mcp_servers._shared import t
from mcp_servers.netdiag.dns_adapter import DnsResolverAdapter, create_dns_adapter
from mcp_servers.netdiag.dns_compare import DnsCompareOrchestrator
from mcp_servers.netdiag.pipeline import AUDIT, DiagnosticOrchestrator
from mcp_servers.netdiag.policy import DefaultNetdiagPolicy, NetworkAccessPolicy
from mcp_servers.netdiag.redaction import NETDIAG_REDACTION
from mcp_servers.netdiag.report import EscalationMarkdownFormatter, JsonReportFormatter, ReportFormatter

VALID_OUTPUT_FORMATS = frozenset({"markdown", "json"})


class NetdiagServiceFactory:
    """集中組裝策略、DNS 轉接器、編排器與報告格式化器。"""

    @staticmethod
    def create_policy() -> NetworkAccessPolicy:
        return DefaultNetdiagPolicy()

    @staticmethod
    def create_dns_adapter() -> DnsResolverAdapter:
        return create_dns_adapter()

    @classmethod
    def create_diagnostic_orchestrator(cls) -> DiagnosticOrchestrator:
        return DiagnosticOrchestrator(cls.create_policy(), cls.create_dns_adapter(), NETDIAG_REDACTION)

    @classmethod
    def create_dns_compare_orchestrator(cls) -> DnsCompareOrchestrator:
        return DnsCompareOrchestrator(cls.create_policy(), cls.create_dns_adapter(), NETDIAG_REDACTION)

    @staticmethod
    def create_formatter(output_format: str = "markdown") -> ReportFormatter:
        fmt = (output_format or "markdown").strip().lower()
        if fmt == "markdown":
            return EscalationMarkdownFormatter(NETDIAG_REDACTION)
        if fmt == "json":
            return JsonReportFormatter(NETDIAG_REDACTION)
        raise ValueError(t("netdiag_mcp.invalid_output_format", output_format=output_format))


def audit_invalid_args(tool_name: str, target_display: str, exc: ValueError) -> str:
    """統一記錄無效參數攔截稽核日誌並回傳遮罩後錯誤訊息。"""
    safe_err = NETDIAG_REDACTION.redact(str(exc))
    AUDIT.log(
        tool_name,
        "invalid_args",
        target=target_display,
        status="BLOCKED",
        detail=safe_err,
    )
    return f"[ERROR] {safe_err}"
