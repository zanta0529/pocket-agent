"""Windows RDP Layer B 結構化唯讀巡檢工具群組（Command / Registrar Pattern）。"""

import base64
from typing import Any

from mcp_servers.win_rdp import security, session


def win_system_overview(
    rdp_host: str = "",
    rdp_port: str = "",
    rdp_user: str = "",
    rdp_password: str = "",
) -> str:
    """取得遠端 Windows 主機的系統概況：OS 版本、Uptime、Hotfix、CPU 負載與記憶體狀態（唯讀）。"""
    cmd = (
        "echo === HOSTNAME & hostname & "
        "echo === SYSTEMINFO & systeminfo & "
        "echo === CPU & wmic cpu get Name,NumberOfCores,LoadPercentage /value"
    )
    return session.run_rdp_clip_command(cmd, rdp_host, rdp_port, rdp_user, rdp_password)


def win_storage_and_fs(
    rdp_host: str = "",
    rdp_port: str = "",
    rdp_user: str = "",
    rdp_password: str = "",
) -> str:
    """取得遠端 Windows 主機的儲存與磁碟空間資訊：邏輯磁碟區容量、剩餘空間、檔案系統與標籤（唯讀）。"""
    cmd = (
        "echo === LOGICAL_DISKS & "
        "wmic logicaldisk get Caption,DriveType,FileSystem,FreeSpace,Size,VolumeName /format:list & "
        "echo === VOLUMES & "
        "wmic volume get DriveLetter,Label,FileSystem,Capacity,FreeSpace /format:list"
    )
    return session.run_rdp_clip_command(cmd, rdp_host, rdp_port, rdp_user, rdp_password)


def win_network_status(
    rdp_host: str = "",
    rdp_port: str = "",
    rdp_user: str = "",
    rdp_password: str = "",
) -> str:
    """取得遠端 Windows 主機的網路狀態：IP 與網卡介面設定、IPv4 路由表與目前監聽連接埠（唯讀）。"""
    cmd = (
        "echo === IPCONFIG & ipconfig /all & "
        "echo === ROUTES & route print -4 & "
        "echo === LISTENING_PORTS & netstat -ano | findstr LISTENING"
    )
    return session.run_rdp_clip_command(cmd, rdp_host, rdp_port, rdp_user, rdp_password)


def win_service_status(
    service_name: str,
    rdp_host: str = "",
    rdp_port: str = "",
    rdp_user: str = "",
    rdp_password: str = "",
) -> str:
    """查詢遠端 Windows 主機特定服務（Windows Service）的執行狀態、PID 與啟動設定（唯讀）。"""
    try:
        clean_svc = security.validate_service_name(service_name)
    except ValueError as exc:
        return f"[ERROR] {exc}"

    cmd = f'echo === SC_QUERYEX & sc.exe queryex "{clean_svc}" & echo === SC_QC & sc.exe qc "{clean_svc}"'
    return session.run_rdp_clip_command(cmd, rdp_host, rdp_port, rdp_user, rdp_password)


def win_query_eventlog(
    log_name: str = "System",
    level: str = "error",
    lines: int = 30,
    rdp_host: str = "",
    rdp_port: str = "",
    rdp_user: str = "",
    rdp_password: str = "",
) -> str:
    """查詢遠端 Windows 主機的事件檢視器日誌（System, Application, Security, Setup，唯讀）。"""
    clean_log = security.VALID_EVENT_LOGS.get(log_name.strip().lower())
    if not clean_log:
        allowed = ", ".join(security.VALID_EVENT_LOGS.values())
        return f"[ERROR] 非法的 log_name 參數：{log_name!r}（僅允許 {allowed}）"

    clean_lvl = security.VALID_EVENT_LEVELS.get(level.strip().lower())
    if not clean_lvl:
        return f"[ERROR] 非法的 level 參數：{level!r}（僅允許 critical, error, warning, info 或 1~4）"

    safe_lines = max(1, min(int(lines), 100))
    cmd = f'wevtutil.exe qe "{clean_log}" "/q:*[System[(Level={clean_lvl})]]" /c:{safe_lines} /rd:true /f:text'
    return session.run_rdp_clip_command(cmd, rdp_host, rdp_port, rdp_user, rdp_password)


def win_security_status(
    rdp_host: str = "",
    rdp_port: str = "",
    rdp_user: str = "",
    rdp_password: str = "",
) -> str:
    """取得遠端 Windows 主機的資安狀態：Windows 防火牆設定檔狀態、當前帳號權限與近期失敗登入事件（唯讀）。"""
    cmd = (
        "echo === FIREWALL & netsh advfirewall show allprofiles & "
        "echo === PRIVILEGES & whoami /priv & "
        'echo === FAILED_LOGONS & wevtutil.exe qe Security "/q:*[System[(EventID=4625)]]" /c:15 /rd:true /f:text'
    )
    return session.run_rdp_clip_command(cmd, rdp_host, rdp_port, rdp_user, rdp_password)


def win_read_config(
    file_path: str,
    max_lines: int = 200,
    rdp_host: str = "",
    rdp_port: str = "",
    rdp_user: str = "",
    rdp_password: str = "",
) -> str:
    """安全讀取遠端 Windows 主機白名單目錄內之設定檔（自動阻擋敏感路徑並遮罩密碼與 Token，唯讀）。"""
    try:
        clean_path = security.validate_win_config_path(file_path)
    except ValueError as exc:
        return f"[ERROR] {exc}"

    safe_lines = max(1, min(int(max_lines), 500))
    b64_path = base64.b64encode(clean_path.encode("utf-16le")).decode("ascii")
    cmd = (
        "powershell -NoProfile -NonInteractive -Command "
        f"\"$p = [System.Text.Encoding]::Unicode.GetString([System.Convert]::FromBase64String('{b64_path}')); "
        f'Get-Content -LiteralPath $p -TotalCount {safe_lines}"'
    )
    return session.run_rdp_clip_command(cmd, rdp_host, rdp_port, rdp_user, rdp_password)


def register(mcp: Any) -> None:
    """註冊 Windows RDP Layer B 唯讀巡檢工具至 MCPServer。"""
    for fn in (
        win_system_overview,
        win_storage_and_fs,
        win_network_status,
        win_service_status,
        win_query_eventlog,
        win_security_status,
        win_read_config,
    ):
        mcp.tool()(fn)
