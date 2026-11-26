"""多解析器 DNS 比對與郵件／CAA 安全紀錄工具（Command / Registrar Pattern）。"""

from typing import Any

from mcp_servers.netdiag.factory import NetdiagServiceFactory, audit_invalid_args


async def netdiag_dns_compare(
    domain: str,
    record_types: str = "A,AAAA,CNAME",
    nameservers: str = "",
    timeout_sec: float | None = None,
    output_format: str = "markdown",
) -> str:
    """比對系統預設 DNS 與指定解析器之網域紀錄差異、TTL、CNAME 鏈與 SPF／_dmarc／CAA。

    Args:
        domain: 目標網域名稱（不接受 IP 位址）。
        record_types: 逗號分隔之紀錄型別（A, AAAA, CNAME, NS, SOA, MX, TXT, CAA, HTTPS, SRV）。
        nameservers: 逗號分隔之額外 DNS 解析器 IP（最多 4 個，例如 1.1.1.1,8.8.8.8）。
        timeout_sec: 單次查詢逾時秒數（3.0~15.0，未指定時採用 NETDIAG_TIMEOUT 設定）。
        output_format: 報告輸出格式（markdown 或 json，預設 markdown）。
    """
    try:
        formatter = NetdiagServiceFactory.create_formatter(output_format)
        orchestrator = NetdiagServiceFactory.create_dns_compare_orchestrator()
        report = await orchestrator.run(
            domain=domain,
            record_types=record_types,
            nameservers=nameservers,
            timeout_sec=timeout_sec,
        )
        return formatter.format_dns_compare(report)
    except ValueError as exc:
        return audit_invalid_args("netdiag_dns_compare", domain, exc)


def register(mcp: Any) -> None:
    """註冊 netdiag_dns_compare 至 MCPServer。"""
    mcp.tool()(netdiag_dns_compare)
