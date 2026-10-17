"""遠端 RHEL 主機唯讀探查與維運診斷 MCP 伺服器。

安全設計原則：
1. 採用系統原生 OpenSSH Client (shell=False, BatchMode=yes)，零新增第三方依賴。
2. 單一目標主機由環境變數 RHEL_HOST 等控制，不暴露 host 參數予 LLM。
3. 100% 結構化唯讀探查工具，所有識別字經正規表示式白名單與 shlex.quote 雙重防護。
4. 遠端設定檔讀取受白名單目錄、敏感關鍵字黑名單與 realpath 解析保護，並自動遮罩機密欄位。
"""

import os
import posixpath
import re
import shlex
import subprocess

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer

mcp = MCPServer("rhel-ops")

MAX_OUTPUT_CHARS = 6000
_IDENT_RE = re.compile(r"^[a-zA-Z0-9_.@-]+$")
_SINCE_RE = re.compile(r"^[a-zA-Z0-9_.:+\- ]+$")

_VALID_PRIORITIES = {
    "emerg",
    "alert",
    "crit",
    "err",
    "warning",
    "notice",
    "info",
    "debug",
    "0",
    "1",
    "2",
    "3",
    "4",
    "5",
    "6",
    "7",
}

_ALLOWED_CONFIG_PREFIXES = (
    "/etc/",
    "/usr/lib/systemd/",
    "/proc/",
    "/sys/",
)

_DENIED_PATH_KEYWORDS = (
    "shadow",
    "gshadow",
    "sudoers",
    "private",
    "secret",
    "key",
    "token",
    "password",
    "passwd-",
    ".ssh",
    ".env",
    "kubeconfig",
    "admin.conf",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
)

_REDACT_KV_RE = re.compile(
    r"(?i)(\b(?:password|passwd|secret|token|api_key|apikey|client_secret|passphrase)\b"
    r"\s*[:=]\s*)([^\s#;\"']+|\"[^\"]*\"|'[^']*')"
)
_REDACT_PEM_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----")


def _validate_identifier(value: str, field_name: str) -> str:
    clean = value.strip()
    if not clean or clean.startswith("-") or not _IDENT_RE.match(clean):
        raise ValueError(f"非法的 {field_name} 參數格式：{value!r}（僅允許英數字、點、底線、連字號與 @）")
    return clean


def _validate_config_path(file_path: str) -> str:
    raw = file_path.strip()
    if not raw.startswith("/"):
        raise ValueError("設定檔路徑必須為絕對路徑（以 / 開頭）。")
    if ".." in raw.split("/"):
        raise ValueError("拒絕存取：路徑不得包含 '..' 跳躍符號。")

    norm = posixpath.normpath(raw)
    if not any(norm.startswith(prefix) or norm == prefix.rstrip("/") for prefix in _ALLOWED_CONFIG_PREFIXES):
        allowed_str = "、".join(_ALLOWED_CONFIG_PREFIXES)
        raise ValueError(f"拒絕存取：{norm} 不在允許的唯讀探查目錄白名單內（{allowed_str}）。")

    lower_path = norm.lower()
    for kw in _DENIED_PATH_KEYWORDS:
        if kw in lower_path:
            raise ValueError(f"拒絕存取：路徑包含敏感保護關鍵字 {kw!r}，禁止讀取。")
    return norm


def _redact_and_truncate(text: str) -> str:
    if not text:
        return "(沒有輸出)"
    redacted = _REDACT_PEM_RE.sub("[REDACTED PRIVATE KEY]", text)
    redacted = _REDACT_KV_RE.sub(r"\1[REDACTED]", redacted)
    if len(redacted) > MAX_OUTPUT_CHARS:
        return redacted[:MAX_OUTPUT_CHARS] + "\n... (輸出過長已截斷)"
    return redacted


def _build_ssh_args(remote_cmd: str) -> tuple[list[str], int]:
    host = (os.getenv("RHEL_HOST") or "").strip()
    if not host:
        raise ValueError("缺少 RHEL_HOST 環境變數。請在 mcp_config.yaml 的 env 區段設定目標 RHEL 主機位址或別名。")
    _validate_identifier(host, "RHEL_HOST")

    user = (os.getenv("RHEL_SSH_USER") or "").strip()
    port = (os.getenv("RHEL_SSH_PORT") or "22").strip()
    key_path = (os.getenv("RHEL_SSH_KEY_PATH") or "").strip()
    strict_host = (os.getenv("RHEL_STRICT_HOST_KEY") or "yes").strip()

    try:
        timeout = max(3, min(int(os.getenv("RHEL_CONNECT_TIMEOUT") or "10"), 60))
    except ValueError:
        timeout = 10

    if not port.isdigit() or not (1 <= int(port) <= 65535):
        raise ValueError(f"非法的 RHEL_SSH_PORT：{port!r}")

    if strict_host not in ("yes", "accept-new", "no"):
        strict_host = "yes"

    args = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={timeout}",
        "-o",
        f"StrictHostKeyChecking={strict_host}",
        "-p",
        port,
    ]
    if key_path:
        args.extend(["-i", key_path])

    target = f"{_validate_identifier(user, 'RHEL_SSH_USER')}@{host}" if user else host
    args.extend(["--", target, remote_cmd])
    return args, timeout


def _run_remote(remote_cmd: str) -> str:
    try:
        ssh_args, timeout = _build_ssh_args(remote_cmd)
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        proc = subprocess.run(
            ssh_args,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout + 15,
            creationflags=creationflags,
        )
        combined = (proc.stdout or "").strip()
        err_out = (proc.stderr or "").strip()
        if proc.returncode != 0 and not combined:
            return f"[ERROR] 遠端執行失敗 (exit {proc.returncode}): {_redact_and_truncate(err_out)}"
        if err_out and combined:
            combined = f"{combined}\n[STDERR]\n{err_out}"
        return _redact_and_truncate(combined)
    except subprocess.TimeoutExpired:
        return "[ERROR] SSH 遠端指令執行逾時，已中止連線。"
    except Exception as e:
        return f"[ERROR] {e}"


@mcp.tool()
def rhel_system_overview() -> str:
    """探查 RHEL 主機系統概況：OS 版本、Kernel、Uptime、CPU/記憶體使用量與前 10 大高負載行程"""
    cmd = (
        "echo '=== OS Release ===' && cat /etc/redhat-release /etc/os-release 2>/dev/null | head -n 15; "
        "echo '=== Kernel & Uptime ===' && uname -srm && uptime; "
        "echo '=== Memory (MB) ===' && free -m; "
        "echo '=== Top Processes (by CPU) ===' && "
        "ps -eo pid,ppid,user,%cpu,%mem,stat,comm --sort=-%cpu | head -n 11"
    )
    return _run_remote(cmd)


@mcp.tool()
def rhel_storage_and_fs() -> str:
    """探查 RHEL 儲存與檔案系統狀態：掛載點空間使用率 (df -hT)、Inode 使用率 (df -i) 與區塊裝置 (lsblk)"""
    cmd = (
        "echo '=== Filesystem Usage ===' && df -hT -x tmpfs -x devtmpfs; "
        "echo '=== Inode Usage ===' && df -i -x tmpfs -x devtmpfs; "
        "echo '=== Block Devices ===' && lsblk -o NAME,FSTYPE,SIZE,MOUNTPOINT,UUID"
    )
    return _run_remote(cmd)


@mcp.tool()
def rhel_network_status() -> str:
    """探查 RHEL 網路狀態：網路介面與 IP、路由表、DNS 設定與目前監聽的 TCP/UDP 連接埠"""
    cmd = (
        "echo '=== Network Interfaces ===' && ip -br addr; "
        "echo '=== Routing Table ===' && ip route; "
        "echo '=== DNS Resolver ===' && cat /etc/resolv.conf 2>/dev/null; "
        "echo '=== Listening Ports ===' && ss -tuln"
    )
    return _run_remote(cmd)


@mcp.tool()
def rhel_service_status(service_name: str = "", failed_only: bool = False) -> str:
    """查詢 RHEL systemd 服務狀態。不指定 service_name 時列出失敗或活動中的服務；指定時顯示該服務詳細狀態"""
    if failed_only or not service_name.strip():
        state_flag = "--state=failed" if failed_only else "--type=service --state=running,failed"
        cmd = f"systemctl list-units {state_flag} --no-pager --no-legend | head -n 80"
        return _run_remote(cmd)

    try:
        safe_unit = _validate_identifier(service_name, "service_name")
    except ValueError as e:
        return f"[ERROR] {e}"

    quoted = shlex.quote(safe_unit)
    cmd = f"systemctl status {quoted} --no-pager -n 25"
    return _run_remote(cmd)


@mcp.tool()
def rhel_query_journal(
    unit: str = "",
    priority: str = "err",
    since: str = "1 hour ago",
    lines: int = 50,
) -> str:
    """結構化檢索 RHEL systemd journald 日誌（強制無分頁並限制行數上限 200 行）"""
    parts = ["journalctl", "--no-pager"]

    if unit.strip():
        try:
            safe_unit = _validate_identifier(unit, "unit")
        except ValueError as e:
            return f"[ERROR] {e}"
        parts.extend(["-u", shlex.quote(safe_unit)])

    prio = priority.strip().lower()
    if prio:
        if prio not in _VALID_PRIORITIES:
            return f"[ERROR] 非法的 priority 參數：{priority!r}（允許值：{sorted(_VALID_PRIORITIES)}）"
        parts.extend(["-p", shlex.quote(prio)])

    since_clean = since.strip()
    if since_clean:
        if not _SINCE_RE.match(since_clean) or len(since_clean) > 40:
            return f"[ERROR] 非法的 since 時間格式：{since!r}"
        parts.extend(["--since", shlex.quote(since_clean)])

    safe_lines = max(1, min(int(lines or 50), 200))
    parts.extend(["-n", str(safe_lines)])

    return _run_remote(" ".join(parts))


@mcp.tool()
def rhel_security_status() -> str:
    """探查 RHEL 安全性子系統狀態：SELinux 模式與狀態、firewalld 防火牆規則摘要，以及待安裝的安全性更新"""
    cmd = (
        "echo '=== SELinux Status ===' && (sestatus || getenforce) 2>/dev/null; "
        "echo '=== Recent SELinux AVC Denials ===' && "
        "(ausearch -m avc -ts recent 2>/dev/null | tail -n 20 || "
        "journalctl -t setroubleshoot --no-pager -n 15 2>/dev/null || echo '無權限或無近期 AVC 紀錄'); "
        "echo '=== Firewalld State ===' && "
        "(firewall-cmd --state && firewall-cmd --list-all) 2>/dev/null || echo 'firewalld 未執行或需要權限'; "
        "echo '=== Security Updates (dnf) ===' && "
        "dnf updateinfo summary --security 2>/dev/null | tail -n 25"
    )
    return _run_remote(cmd)


@mcp.tool()
def rhel_read_config(file_path: str, max_lines: int = 120) -> str:
    """安全讀取 RHEL 白名單目錄內的公開系統設定檔（自動阻擋敏感機密路徑並遮罩密碼與金鑰）"""
    try:
        safe_path = _validate_config_path(file_path)
    except ValueError as e:
        return f"[ERROR] {e}"

    safe_lines = max(1, min(int(max_lines or 120), 300))
    quoted_path = shlex.quote(safe_path)
    # 於遠端先解析符號連結真實路徑，防止透過 symlink 指向 /etc/shadow 或私鑰
    cmd = (
        f"real=$(realpath -e {quoted_path} 2>/dev/null) || {{ echo '[ERROR] 檔案不存在或無法存取'; exit 1; }}; "
        "case $real in "
        "*shadow*|*gshadow*|*sudoers*|*private*|*secret*|*key*|*token*|*.ssh*|*.env*) "
        "echo '[ERROR] 拒絕存取：解析後之真實路徑屬於敏感機密檔案'; exit 1 ;; "
        "/etc/*|/usr/lib/systemd/*|/proc/*|/sys/*) "
        f"head -n {safe_lines} -- $real ;; "
        "*) echo '[ERROR] 拒絕存取：解析後之真實路徑不在允許的目錄白名單內'; exit 1 ;; "
        "esac"
    )
    return _run_remote(cmd)


if __name__ == "__main__":
    mcp.run()
