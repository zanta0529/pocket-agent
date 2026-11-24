"""階段式網站與網路舉證診斷工具（Command / Registrar Pattern）。"""

from typing import Any

from mcp_servers._shared import CONSTANTS
from mcp_servers.netdiag.dns_adapter import create_dns_adapter
from mcp_servers.netdiag.pipeline import AUDIT, DiagnosticOrchestrator
from mcp_servers.netdiag.policy import DefaultNetdiagPolicy
from mcp_servers.netdiag.redaction import NETDIAG_REDACTION
from mcp_servers.netdiag.report import EscalationMarkdownFormatter


async def netdiag_diagnose(
    target: str,
    max_stage: str = "http",
    method: str = "HEAD",
    host_header: str = "",
    samples: int = 1,
    timeout_sec: float = CONSTANTS.netdiag_default_timeout_sec,
) -> str:
    """執行階段式唯讀網站與網路診斷（DNS → TCP → TLS 憑證鏈 → HTTP），產出跨單位舉證報告。

    Args:
        target: 目標 URL（http:// 或 https://，支援公開網域或依政策開啟之內網私有 IP）。
        max_stage: 最高執行階段（dns、tcp、tls、http，預設 http）。設為 dns/tls 時不送下游請求。
        method: 唯讀 HTTP 方法（僅允許 HEAD 或 GET，預設 HEAD；不回傳 HTML 正文）。
        host_header: 選填 Host／SNI 覆寫（專供內網私有 IP 虛擬主機除錯使用）。
        samples: 採樣次數（1~3，預設 1）。
        timeout_sec: 單階段逾時秒數（3.0~15.0，預設 10.0）。
    """
    policy = DefaultNetdiagPolicy()
    dns_adapter = create_dns_adapter()
    orchestrator = DiagnosticOrchestrator(policy, dns_adapter, NETDIAG_REDACTION)
    formatter = EscalationMarkdownFormatter(NETDIAG_REDACTION)
    try:
        report = await orchestrator.run(
            target=target,
            max_stage=max_stage,
            method=method,
            host_header=host_header,
            samples=samples,
            timeout_sec=timeout_sec,
        )
        return formatter.format_diagnostic(report)
    except ValueError as exc:
        safe_err = NETDIAG_REDACTION.redact(str(exc))
        AUDIT.log(
            "netdiag_diagnose",
            "invalid_args",
            target=NETDIAG_REDACTION.redact_url(target),
            status="BLOCKED",
            detail=safe_err,
        )
        return f"[ERROR] {safe_err}"


def register(mcp: Any) -> None:
    """註冊 netdiag_diagnose 至 MCPServer。"""
    mcp.tool()(netdiag_diagnose)
