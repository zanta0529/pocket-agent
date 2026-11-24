"""多解析器 DNS 比對與郵件／CAA 安全紀錄工具（Command / Registrar Pattern）。"""

from typing import Any

from mcp_servers.netdiag.dns_adapter import create_dns_adapter
from mcp_servers.netdiag.pipeline import AUDIT, DnsCompareOrchestrator
from mcp_servers.netdiag.policy import DefaultNetdiagPolicy
from mcp_servers.netdiag.redaction import NETDIAG_REDACTION
from mcp_servers.netdiag.report import EscalationMarkdownFormatter


async def netdiag_dns_compare(
    domain: str,
    record_types: str = "A,AAAA,CNAME",
    nameservers: str = "",
    timeout_sec: float | None = None,
) -> str:
    """比對系統預設 DNS 與指定解析器之網域紀錄差異、TTL、CNAME 鏈與 SPF／DMARC／CAA。

    Args:
        domain: 目標網域名稱（不接受 IP 位址）。
        record_types: 逗號分隔之紀錄型別（A, AAAA, CNAME, NS, SOA, MX, TXT, CAA, HTTPS, SRV）。
        nameservers: 逗號分隔之額外 DNS 解析器 IP（最多 4 個，例如 1.1.1.1,8.8.8.8）。
        timeout_sec: 單次查詢逾時秒數（3.0~15.0，未指定時採用 NETDIAG_TIMEOUT 設定）。
    """
    policy = DefaultNetdiagPolicy()
    dns_adapter = create_dns_adapter()
    orchestrator = DnsCompareOrchestrator(policy, dns_adapter, NETDIAG_REDACTION)
    formatter = EscalationMarkdownFormatter(NETDIAG_REDACTION)
    try:
        report = await orchestrator.run(
            domain=domain,
            record_types=record_types,
            nameservers=nameservers,
            timeout_sec=timeout_sec,
        )
        return formatter.format_dns_compare(report)
    except ValueError as exc:
        safe_err = NETDIAG_REDACTION.redact(str(exc))
        AUDIT.log("netdiag_dns_compare", "invalid_args", target=domain, status="BLOCKED", detail=safe_err)
        return f"[ERROR] {safe_err}"


def register(mcp: Any) -> None:
    """註冊 netdiag_dns_compare 至 MCPServer。"""
    mcp.tool()(netdiag_dns_compare)
