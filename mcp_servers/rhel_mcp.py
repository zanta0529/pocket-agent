"""遠端 RHEL 主機唯讀探查與維運診斷 MCP 伺服器薄入口。"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp_servers._shared import CONSTANTS  # noqa: E402
from mcp_servers.rhel import (  # noqa: E402
    mcp,
    rhel_cert_expiry,
    rhel_container_status,
    rhel_cron_and_timers,
    rhel_hardware_info,
    rhel_health_snapshot,
    rhel_kernel_health,
    rhel_list_directory,
    rhel_network_connections,
    rhel_network_status,
    rhel_package_info,
    rhel_process_inspect,
    rhel_query_journal,
    rhel_read_config,
    rhel_security_status,
    rhel_selinux_context,
    rhel_service_status,
    rhel_storage_and_fs,
    rhel_system_overview,
)

MAX_OUTPUT_CHARS = CONSTANTS.max_output_chars

__all__ = [
    "MAX_OUTPUT_CHARS",
    "mcp",
    "rhel_cert_expiry",
    "rhel_container_status",
    "rhel_cron_and_timers",
    "rhel_hardware_info",
    "rhel_health_snapshot",
    "rhel_kernel_health",
    "rhel_list_directory",
    "rhel_network_connections",
    "rhel_network_status",
    "rhel_package_info",
    "rhel_process_inspect",
    "rhel_query_journal",
    "rhel_read_config",
    "rhel_security_status",
    "rhel_selinux_context",
    "rhel_service_status",
    "rhel_storage_and_fs",
    "rhel_system_overview",
]

if __name__ == "__main__":
    mcp.run()
