"""階段式網站與網路舉證診斷工具（Command / Registrar Pattern）。"""

from typing import Any

from mcp_servers.netdiag.factory import NetdiagServiceFactory, audit_invalid_args
from mcp_servers.netdiag.redaction import NETDIAG_REDACTION


async def netdiag_diagnose(
    target: str,
    max_stage: str = "http",
    method: str = "HEAD",
    host_header: str = "",
    ip_family: str = "auto",
    samples: int = 1,
    timeout_sec: float | None = None,
    output_format: str = "markdown",
) -> str:
    """執行階段式唯讀網站與網路診斷（DNS → TCP → TLS 憑證鏈 → HTTP），產出跨單位舉證報告。

    Args:
        target: 目標 URL（http:// 或 https://，支援公開網域或依政策開啟之內網私有 IP）。
        max_stage: 最高執行階段（dns、tcp、tls、http，預設 http）。設為 dns/tls 時不送下游請求。
        method: 唯讀 HTTP 方法（僅允許 HEAD 或 GET，預設 HEAD；不回傳 HTML 正文）。
        host_header: 選填 Host／SNI 覆寫（專供內網私有 IP 虛擬主機除錯使用）。
        ip_family: 指定解析位址族（auto、ipv4、ipv6，預設 auto）。
        samples: 採樣次數（1~3，預設 1）。
        timeout_sec: 單階段逾時秒數（3.0~15.0，未指定時採用 NETDIAG_TIMEOUT 設定）。
        output_format: 報告輸出格式（markdown 或 json，預設 markdown）。
    """
    try:
        formatter = NetdiagServiceFactory.create_formatter(output_format)
        orchestrator = NetdiagServiceFactory.create_diagnostic_orchestrator()
        report = await orchestrator.run(
            target=target,
            max_stage=max_stage,
            method=method,
            host_header=host_header,
            ip_family=ip_family,
            samples=samples,
            timeout_sec=timeout_sec,
        )
        return formatter.format_diagnostic(report)
    except ValueError as exc:
        return audit_invalid_args("netdiag_diagnose", NETDIAG_REDACTION.redact_url(target), exc)


def register(mcp: Any) -> None:
    """註冊 netdiag_diagnose 至 MCPServer。"""
    mcp.tool()(netdiag_diagnose)
