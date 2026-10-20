"""遠端 RHEL 主機唯讀探查與維運診斷 MCP 伺服器。

安全設計原則：
1. 僅允許帳號＋密碼驗證（強制停用 Pubkey、GSSAPI 與 SSH Agent，不殘留金鑰或免密後門）。
2. 支援彈性設定：RHEL_HOST、RHEL_SSH_PORT、RHEL_SSH_USER、RHEL_SSH_PASSWORD 皆可於設定檔預填、留空或省略；
   未設定項目於執行期動態詢問使用者並僅快取於本機記憶體，絕不暴露予 LLM 或寫入日誌。
3. 100% 結構化唯讀探查工具，所有識別字經正規表示式白名單與 shlex.quote 雙重防護。
4. 遠端設定檔讀取受白名單目錄、敏感關鍵字黑名單與 realpath 解析保護，並自動遮罩機密欄位。
"""

import getpass
import os
import posixpath
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer

mcp = MCPServer("rhel-ops")

MAX_OUTPUT_CHARS = 6000
_IDENT_RE = re.compile(r"^[a-zA-Z0-9_.@-]+$")
_SINCE_RE = re.compile(r"^[a-zA-Z0-9_.:+\- ]+$")
_SEARCH_RE = re.compile(r"^[a-zA-Z0-9_.:@/+\- ]+$")

# 執行期記憶體快取（僅存活於當前行程生命週期，不落盤）
_RUNTIME_CREDS: dict[str, str] = {}

_VALID_SORT_KEYS = {"cpu", "mem", "memory"}

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
    "/opt/",
    "/proc/",
    "/run/systemd/",
    "/srv/",
    "/sys/",
    "/usr/lib/systemd/",
    "/usr/local/etc/",
    "/usr/share/",
    "/var/log/",
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
    "opasswd",
    ".ssh",
    ".env",
    "kubeconfig",
    "admin.conf",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "secure",
    "audit",
    "wtmp",
    "btmp",
    "lastlog",
)

_REDACT_KV_RE = re.compile(
    r"(?i)(\b(?:password|passwd|secret|token|api_key|apikey|client_secret|passphrase"
    r"|private_key|privatekey|preshared_key|presharedkey)\b"
    r"\s*[:=]\s*)([^\s#;\"']+|\"[^\"]*\"|'[^']*')"
)
_REDACT_PEM_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----")


def _validate_identifier(value: str, field_name: str) -> str:
    clean = value.strip()
    if not clean or clean.startswith("-") or not _IDENT_RE.match(clean):
        raise ValueError(f"非法的 {field_name} 參數格式：{value!r}（僅允許英數字、點、底線、連字號與 @）")
    return clean


def _validate_search_pattern(value: str, field_name: str, max_len: int = 50) -> str:
    clean = value.strip()
    if not clean or clean.startswith("-") or not _SEARCH_RE.match(clean) or len(clean) > max_len:
        raise ValueError(
            f"非法的 {field_name} 參數格式：{value!r}（僅允許英數字、斜線、冒號、點、空白等安全符號，"
            f"且限 {max_len} 字元以內）"
        )
    return clean


def _validate_port(value: str) -> str:
    clean = value.strip()
    if not clean.isdigit() or not (1 <= int(clean) <= 65535):
        raise ValueError(f"非法的連接埠數值：{value!r}（必須為 1~65535 之間的整數）")
    return clean


def _validate_pid(value: str | int) -> str:
    clean = str(value).strip()
    if not clean.isdigit() or int(clean) <= 0:
        raise ValueError(f"非法的行程 PID 數值：{value!r}（必須為正整數）")
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


def _tty_prompt(prompt_text: str, *, is_password: bool = False) -> str:
    """當獨立執行且未由上層注入時，直接透過作業系統終端機設備（CONIN$/CONOUT$ 或 /dev/tty）詢問。"""
    if is_password:
        return getpass.getpass(prompt_text)

    tty_in_name = "CONIN$" if os.name == "nt" else "/dev/tty"
    tty_out_name = "CONOUT$" if os.name == "nt" else "/dev/tty"
    try:
        with (
            open(tty_out_name, "w", encoding="utf-8") as tty_out,
            open(tty_in_name, encoding="utf-8") as tty_in,
        ):
            tty_out.write(prompt_text)
            tty_out.flush()
            return tty_in.readline().strip()
    except OSError:
        sys.stderr.write(prompt_text)
        sys.stderr.flush()
        return sys.stdin.readline().strip()


def _resolve_connection_params(
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
) -> tuple[str, str, str, str]:
    """依「注入參數 → 記憶體快取 → 環境變數 → 終端機互動詢問」順序取得連線四要素。"""
    cached_host = _RUNTIME_CREDS.get("RHEL_HOST", "")
    cached_port = _RUNTIME_CREDS.get("RHEL_SSH_PORT", "")
    cached_user = _RUNTIME_CREDS.get("RHEL_SSH_USER", "")

    input_host = ssh_host.strip()
    input_port = ssh_port.strip()
    input_user = ssh_user.strip()

    host = input_host or cached_host or (os.getenv("RHEL_HOST") or "").strip()
    port = input_port or cached_port or (os.getenv("RHEL_SSH_PORT") or "").strip()
    user = input_user or cached_user or (os.getenv("RHEL_SSH_USER") or "").strip()

    # 憑證快取隔離：僅當連線目標（host、user）與既有快取完全相符時才沿用舊密碼，防止跨主機密碼外洩
    is_same_target = (not input_host or input_host == cached_host) and (not input_user or input_user == cached_user)

    if ssh_password:
        password = ssh_password
    elif is_same_target and _RUNTIME_CREDS.get("RHEL_SSH_PASSWORD"):
        password = _RUNTIME_CREDS["RHEL_SSH_PASSWORD"]
    else:
        password = os.getenv("RHEL_SSH_PASSWORD") or ""

    if not host:
        host = _tty_prompt("請輸入 RHEL 主機 IP 或位址 (RHEL_HOST): ").strip()
    if not port:
        port = _tty_prompt("請輸入 SSH 連接埠 (RHEL_SSH_PORT) [預設 22]: ").strip() or "22"
    if not user:
        user = _tty_prompt(f"請輸入 {host} 的 SSH 登入帳號 (RHEL_SSH_USER): ").strip()
    if not password:
        password = _tty_prompt(f"請輸入 {user}@{host} 的 SSH 登入密碼: ", is_password=True)

    if not host:
        raise ValueError("未提供 RHEL 主機位址 (RHEL_HOST)。")
    if not user:
        raise ValueError("未提供 SSH 登入帳號 (RHEL_SSH_USER)。")
    if not password:
        raise ValueError("未提供 SSH 登入密碼 (RHEL_SSH_PASSWORD)；本工具僅允許帳號密碼驗證。")

    _validate_identifier(host, "RHEL_HOST")
    _validate_identifier(user, "RHEL_SSH_USER")
    if not port.isdigit() or not (1 <= int(port) <= 65535):
        raise ValueError(f"非法的 RHEL_SSH_PORT：{port!r}（必須為 1~65535 之間的整數）")

    _RUNTIME_CREDS["RHEL_HOST"] = host
    _RUNTIME_CREDS["RHEL_SSH_PORT"] = port
    _RUNTIME_CREDS["RHEL_SSH_USER"] = user
    _RUNTIME_CREDS["RHEL_SSH_PASSWORD"] = password
    return host, port, user, password


def _build_ssh_args(
    remote_cmd: str,
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
) -> tuple[list[str], str, int]:
    host, port, user, password = _resolve_connection_params(ssh_host, ssh_port, ssh_user, ssh_password)
    strict_host = (os.getenv("RHEL_STRICT_HOST_KEY") or "accept-new").strip()

    try:
        timeout = max(3, min(int(os.getenv("RHEL_CONNECT_TIMEOUT") or "10"), 60))
    except ValueError:
        timeout = 10

    if strict_host not in ("yes", "accept-new", "no"):
        strict_host = "accept-new"

    args = [
        "ssh",
        "-o",
        f"ConnectTimeout={timeout}",
        "-o",
        f"StrictHostKeyChecking={strict_host}",
        "-o",
        "BatchMode=no",
        "-o",
        "PubkeyAuthentication=no",
        "-o",
        "GSSAPIAuthentication=no",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "IdentityAgent=none",
        "-o",
        "PreferredAuthentications=keyboard-interactive,password",
        "-o",
        "NumberOfPasswordPrompts=1",
        "-p",
        port,
    ]
    if strict_host == "no":
        null_dev = "NUL" if os.name == "nt" else "/dev/null"
        args.extend(["-o", f"UserKnownHostsFile={null_dev}"])

    target = f"{user}@{host}"
    args.extend(["--", target, remote_cmd])
    return args, password, timeout


def _run_with_askpass(ssh_args: list[str], password: str, timeout: int, creationflags: int):
    with tempfile.TemporaryDirectory(prefix="rhel_mcp_") as tmp_dir:
        tmp_path = Path(tmp_dir)

        # 透過內嵌 Python 腳本自隔離環境變數輸出密碼，100% 記憶體傳遞，杜絕密碼寫入硬碟暫存檔（零落盤）
        py_cmd = 'import os, sys; sys.stdout.write(os.environ["RHEL_SSH_ASKPASS_PW"] + "\\n")'
        if os.name == "nt":
            askpass_script = tmp_path / "askpass.bat"
            askpass_script.write_text(f'@"{sys.executable}" -c "{py_cmd}"\r\n', encoding="utf-8")
        else:
            askpass_script = tmp_path / "askpass.sh"
            askpass_script.write_text(f"#!/bin/sh\nexec \"{sys.executable}\" -c '{py_cmd}'\n", encoding="utf-8")
            askpass_script.chmod(0o700)

        env = dict(os.environ)
        env["RHEL_SSH_ASKPASS_PW"] = password
        env["SSH_ASKPASS"] = str(askpass_script)
        env["SSH_ASKPASS_REQUIRE"] = "force"
        env.setdefault("DISPLAY", "1")
        env.pop("SSH_AUTH_SOCK", None)

        return subprocess.run(
            ssh_args,
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout + 15,
            creationflags=creationflags,
            env=env,
        )


def _run_remote(
    remote_cmd: str,
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
) -> str:
    try:
        ssh_args, password, timeout = _build_ssh_args(remote_cmd, ssh_host, ssh_port, ssh_user, ssh_password)
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        proc = _run_with_askpass(ssh_args, password, timeout, creationflags)

        if proc.returncode == 255:
            # 遠端連線或金鑰/認證失敗。為防止本機行程命令列引數洩漏密碼（Process Argument Inspection），
            # 嚴格禁止使用 plink 傳遞明文密碼參數。強制使用安全 SSH_ASKPASS 通道。
            _RUNTIME_CREDS.clear()
            err_msg = proc.stderr.strip() if proc.stderr else "無法建立 OpenSSH 安全通道。"
            return (
                f"[ERROR] [CONN_AUTH] 遠端 SSH 連線或認證失敗 (exit 255): {_redact_and_truncate(err_msg)}\n"
                "提示：請確認目標主機已啟用密碼/鍵盤互動驗證，或檢查網路與金鑰設定。"
            )

        combined = (proc.stdout or "").strip()
        err_out = (proc.stderr or "").strip()
        if proc.returncode != 0 and not combined:
            if proc.returncode == 255 or "permission denied" in err_out.lower() or "access denied" in err_out.lower():
                _RUNTIME_CREDS.clear()
                return (
                    f"[ERROR] [CONN_AUTH] 遠端連線或認證失敗 (exit {proc.returncode}): {_redact_and_truncate(err_out)}"
                )
            return f"[ERROR] 遠端執行失敗 (exit {proc.returncode}): {_redact_and_truncate(err_out)}"
        if err_out and combined and proc.returncode != 0:
            combined = f"{combined}\n[STDERR]\n{err_out}"
        return _redact_and_truncate(combined)
    except ValueError as e:
        _RUNTIME_CREDS.clear()
        return f"[ERROR] [CONN_AUTH] {e}"
    except subprocess.TimeoutExpired:
        return "[ERROR] SSH 遠端指令執行逾時，已中止連線。"
    except Exception as e:
        return f"[ERROR] {e}"


@mcp.tool()
def rhel_system_overview(
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
) -> str:
    """探查 RHEL 主機系統概況：OS 版本、Kernel、Uptime、CPU/記憶體使用量與前 10 大高負載行程"""
    cmd = (
        "echo '=== OS Release ===' && cat /etc/redhat-release /etc/os-release 2>/dev/null | head -n 15; "
        "echo '=== Kernel & Uptime ===' && uname -srm && uptime; "
        "echo '=== Memory (MB) ===' && free -m; "
        "echo '=== Top Processes (by CPU) ===' && "
        "ps -eo pid,ppid,user,%cpu,%mem,stat,comm --sort=-%cpu | head -n 11"
    )
    return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password)


@mcp.tool()
def rhel_storage_and_fs(
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
) -> str:
    """探查 RHEL 儲存與檔案系統狀態：掛載點空間使用率 (df -hT)、Inode 使用率 (df -i) 與區塊裝置 (lsblk)"""
    cmd = (
        "echo '=== Filesystem Usage ===' && df -hT -x tmpfs -x devtmpfs; "
        "echo '=== Inode Usage ===' && df -i -x tmpfs -x devtmpfs; "
        "echo '=== Block Devices ===' && lsblk -o NAME,FSTYPE,SIZE,MOUNTPOINT,UUID"
    )
    return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password)


@mcp.tool()
def rhel_network_status(
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
) -> str:
    """探查 RHEL 網路狀態：網路介面與 IP、路由表、DNS 設定與目前監聽的 TCP/UDP 連接埠"""
    cmd = (
        "echo '=== Network Interfaces ===' && ip -br addr; "
        "echo '=== Routing Table ===' && ip route; "
        "echo '=== DNS Resolver ===' && cat /etc/resolv.conf 2>/dev/null; "
        "echo '=== Listening Ports ===' && ss -tuln"
    )
    return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password)


@mcp.tool()
def rhel_service_status(
    service_name: str = "",
    failed_only: bool = False,
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
) -> str:
    """查詢 RHEL systemd 服務狀態。不指定 service_name 時列出失敗或活動中的服務；指定時顯示該服務詳細狀態"""
    if failed_only or not service_name.strip():
        state_flag = "--state=failed" if failed_only else "--type=service --state=running,failed"
        cmd = f"systemctl list-units {state_flag} --no-pager --no-legend | head -n 80"
        return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password)

    try:
        safe_unit = _validate_identifier(service_name, "service_name")
    except ValueError as e:
        return f"[ERROR] {e}"

    quoted = shlex.quote(safe_unit)
    cmd = f"systemctl status {quoted} --no-pager -n 25"
    return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password)


@mcp.tool()
def rhel_query_journal(
    unit: str = "",
    priority: str = "err",
    since: str = "1 hour ago",
    grep: str = "",
    lines: int = 50,
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
) -> str:
    """結構化檢索 RHEL systemd journald 日誌（支援 unit、優先等級、時間範圍與 grep 關鍵字）"""
    parts = ["journalctl", "--no-pager"]

    if unit.strip():
        try:
            safe_unit = _validate_identifier(unit, "unit")
        except ValueError as e:
            return f"[ERROR] {e}"
        parts.extend(["-u", shlex.quote(safe_unit)])

    prio_aliases = {"error": "err", "warn": "warning", "critical": "crit"}
    prio = prio_aliases.get(priority.strip().lower(), priority.strip().lower())
    if prio and prio not in ("all", "none"):
        if prio not in _VALID_PRIORITIES:
            return f"[ERROR] 非法的 priority 參數：{priority!r}（允許值：{sorted(_VALID_PRIORITIES)}）"
        parts.extend(["-p", shlex.quote(prio)])

    since_clean = since.strip()
    if since_clean:
        if not _SINCE_RE.match(since_clean) or len(since_clean) > 40:
            return f"[ERROR] 非法的 since 時間格式：{since!r}"
        parts.extend(["--since", shlex.quote(since_clean)])

    if grep.strip():
        try:
            safe_grep = _validate_search_pattern(grep, "grep")
        except ValueError as e:
            return f"[ERROR] {e}"
        parts.extend(["-g", shlex.quote(safe_grep)])

    safe_lines = max(1, min(int(lines or 50), 200))
    parts.extend(["-n", str(safe_lines)])

    return _run_remote(" ".join(parts), ssh_host, ssh_port, ssh_user, ssh_password)


@mcp.tool()
def rhel_security_status(
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
) -> str:
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
    return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password)


@mcp.tool()
def rhel_read_config(
    file_path: str,
    max_lines: int = 120,
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
) -> str:
    """安全讀取 RHEL 白名單目錄內的公開系統設定檔（自動阻擋敏感機密路徑並遮罩密碼與金鑰）"""
    try:
        safe_path = _validate_config_path(file_path)
    except ValueError as e:
        return f"[ERROR] {e}"

    safe_lines = max(1, min(int(max_lines or 120), 300))
    quoted_path = shlex.quote(safe_path)
    denied_case = "|".join(f"*{kw}*" for kw in _DENIED_PATH_KEYWORDS)
    allowed_case = "|".join(f"{p.rstrip('/')}/*" for p in _ALLOWED_CONFIG_PREFIXES)
    cmd = (
        f"real=$(realpath -e {quoted_path} 2>/dev/null) || {{ echo '[ERROR] 檔案不存在或無法存取'; exit 1; }}; "
        'case "$real" in '
        f"{denied_case}) "
        "echo '[ERROR] 拒絕存取：解析後之真實路徑屬於敏感機密檔案'; exit 1 ;; "
        f"{allowed_case}) "
        f'head -n {safe_lines} -- "$real" ;; '
        "*) echo '[ERROR] 拒絕存取：解析後之真實路徑不在允許的目錄白名單內'; exit 1 ;; "
        "esac"
    )
    return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password)


@mcp.tool()
def rhel_list_directory(
    dir_path: str,
    show_selinux: bool = False,
    max_lines: int = 100,
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
) -> str:
    """安全列出 RHEL 白名單目錄（如 /etc、/opt、/srv、/var/log 等）內的檔案清單（可選顯示 SELinux 標籤）"""
    try:
        safe_path = _validate_config_path(dir_path)
    except ValueError as e:
        return f"[ERROR] {e}"

    safe_lines = max(1, min(int(max_lines or 100), 250))
    quoted_path = shlex.quote(safe_path)
    denied_case = "|".join(f"*{kw}*" for kw in _DENIED_PATH_KEYWORDS)
    allowed_patterns = [p.rstrip("/") for p in _ALLOWED_CONFIG_PREFIXES] + [
        f"{p.rstrip('/')}/*" for p in _ALLOWED_CONFIG_PREFIXES
    ]
    allowed_case = "|".join(allowed_patterns)
    ls_flags = "-laZ --time-style=long-iso" if show_selinux else "-la --time-style=long-iso"
    cmd = (
        f"real=$(realpath -e {quoted_path} 2>/dev/null) || {{ echo '[ERROR] 目錄不存在或無法存取'; exit 1; }}; "
        "[ -d \"$real\" ] || { echo '[ERROR] 指定路徑並非目錄'; exit 1; }; "
        "case $real in "
        f"{denied_case}) "
        "echo '[ERROR] 拒絕存取：解析後之真實路徑屬於敏感保護目錄'; exit 1 ;; "
        f"{allowed_case}) "
        f'ls {ls_flags} -- "$real" | head -n {safe_lines} ;; '
        "*) echo '[ERROR] 拒絕存取：解析後之真實路徑不在允許的目錄白名單內'; exit 1 ;; "
        "esac"
    )
    return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password)


@mcp.tool()
def rhel_selinux_context(
    target_path: str,
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
) -> str:
    """查詢指定檔案或目錄的目前 SELinux 安全標籤 (ls -dZ) 與政策預期標籤 (matchpathcon)"""
    try:
        safe_path = _validate_config_path(target_path)
    except ValueError as e:
        return f"[ERROR] {e}"

    quoted_path = shlex.quote(safe_path)
    cmd = (
        f"echo '=== Current SELinux Context (ls -dZ) ===' && ls -dZ -- {quoted_path} 2>&1; "
        "echo '=== Expected Context (matchpathcon) ===' && "
        f"(matchpathcon {quoted_path} 2>/dev/null || echo '無 matchpathcon 紀錄或工具未安裝')"
    )
    return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password)


@mcp.tool()
def rhel_process_inspect(
    pid: str = "",
    search: str = "",
    sort_by: str = "cpu",
    show_selinux: bool = False,
    max_lines: int = 50,
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
) -> str:
    """深度探查 RHEL 行程資訊：可指定 PID 檢視細節、以關鍵字搜尋行程、或依 CPU/Memory 排序（支援 SELinux 標籤）"""
    safe_lines = max(5, min(int(max_lines or 50), 100))

    if pid.strip():
        try:
            safe_pid = _validate_pid(pid)
        except ValueError as e:
            return f"[ERROR] {e}"
        cmd = (
            f"echo '=== Process Detail (PID: {safe_pid}) ===' && "
            f"ps -fp {safe_pid} 2>/dev/null || echo '行程不存在或已終止'; "
            "echo '=== Process Status Summary ===' && "
            f"(cat /proc/{safe_pid}/status 2>/dev/null | "
            "grep -E '^(Name|State|Tgid|Pid|PPid|Uid|Gid|Threads|VmRSS|VmSize)' || true); "
            "echo '=== Process SELinux Label ===' && "
            f"(ps -q {safe_pid} -o pid,user,label,comm 2>/dev/null || echo '無 SELinux 標籤資訊')"
        )
        return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password)

    cols = "pid,ppid,user,%cpu,%mem,stat,label,comm" if show_selinux else "pid,ppid,user,%cpu,%mem,stat,comm"

    if search.strip():
        try:
            safe_search = _validate_search_pattern(search, "search")
        except ValueError as e:
            return f"[ERROR] {e}"
        quoted_search = shlex.quote(safe_search)
        cmd = (
            f"echo '=== Search Processes matching {quoted_search} ===' && "
            f"ps -eo {cols},args | grep -i -- {quoted_search} | grep -v grep | head -n {safe_lines}"
        )
        return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password)

    sort_clean = sort_by.strip().lower()
    if sort_clean not in _VALID_SORT_KEYS:
        return f"[ERROR] 非法的 sort_by 參數：{sort_by!r}（僅允許 'cpu'、'mem' 或 'memory'）"

    sort_col = "-%mem" if sort_clean in ("mem", "memory") else "-%cpu"
    cmd = (
        f"echo '=== Top Processes (sorted by {sort_clean}) ===' && "
        f"ps -eo {cols} --sort={sort_col} | head -n {safe_lines}"
    )
    return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password)


@mcp.tool()
def rhel_network_connections(
    protocol: str = "all",
    state: str = "all",
    port: str = "",
    show_process: bool = True,
    max_lines: int = 60,
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
) -> str:
    """利用 ss (Socket Statistics) 結構化探查網路連線狀態（支援 TCP/UDP 協議、狀態過濾、特定 Port 與行程關聯）"""
    proto_clean = protocol.strip().lower()
    proto_flag = "-t" if proto_clean == "tcp" else "-u" if proto_clean == "udp" else "-tu"
    proc_flag = " -p" if show_process else ""

    state_clean = state.strip().lower()
    valid_states = {"all", "listening", "established", "time-wait", "close-wait", "syn-sent", "syn-recv"}
    if state_clean not in valid_states:
        return f"[ERROR] 非法的 state 參數：{state!r}（允許值：{sorted(valid_states)}）"

    if state_clean == "listening":
        base_cmd = f"ss {proto_flag}ln{proc_flag}"
    elif state_clean == "all":
        base_cmd = f"ss {proto_flag}an{proc_flag}"
    else:
        base_cmd = f"ss {proto_flag}an{proc_flag} state {shlex.quote(state_clean)}"

    filter_expr = ""
    if port.strip():
        try:
            safe_port = _validate_port(port)
        except ValueError as e:
            return f"[ERROR] {e}"
        filter_expr = f" sport = :{safe_port} or dport = :{safe_port}"

    safe_lines = max(5, min(int(max_lines or 60), 150))
    cmd = f"{base_cmd}{filter_expr} | head -n {safe_lines}"
    return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password)


@mcp.tool()
def rhel_package_info(
    package_name: str,
    verify_integrity: bool = False,
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
) -> str:
    """查詢 RHEL RPM/DNF 套件資訊、版本與檔案完整性驗證 (rpm -qi, rpm -V, dnf info)"""
    try:
        safe_pkg = _validate_identifier(package_name, "package_name")
    except ValueError as e:
        return f"[ERROR] {e}"

    quoted = shlex.quote(safe_pkg)
    if verify_integrity:
        cmd = (
            f"echo '=== Package Verification (rpm -V) ===' && "
            f"(rpm -V {quoted} || echo '檔案有變更或未安裝'); "
            "echo '=== Installed Files (rpm -ql, head 30) ===' && "
            f"rpm -ql {quoted} 2>/dev/null | head -n 30"
        )
    else:
        cmd = (
            f"echo '=== Package Info (rpm -qi) ===' && "
            f"(rpm -qi {quoted} 2>/dev/null || dnf info {quoted} 2>/dev/null || echo '查無此套件'); "
            "echo '=== Changelog Summary (recent) ===' && "
            f"rpm -q --changelog {quoted} 2>/dev/null | head -n 20"
        )
    return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password)


@mcp.tool()
def rhel_container_status(
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
) -> str:
    """探查 RHEL 容器執行環境：檢視 Podman/Docker 容器狀態、映像檔與 Pod 清單"""
    cmd = (
        "echo '=== Container Engine Detection ===' && "
        "(which podman 2>/dev/null || which docker 2>/dev/null || echo '未安裝 podman 或 docker'); "
        "echo '=== Running/All Containers ===' && "
        "((podman ps -a --format 'table {{.ID}} {{.Image}} {{.Status}} {{.Names}}' 2>/dev/null || "
        "docker ps -a --format 'table {{.ID}}\t{{.Image}}\t{{.Status}}\t{{.Names}}' 2>/dev/null) "
        "| head -n 30 || echo '無容器或未啟動'); "
        "echo '=== Podman Pods ===' && "
        "(podman pod ps 2>/dev/null | head -n 15 || echo '無 podman pod 紀錄')"
    )
    return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password)


@mcp.tool()
def rhel_cron_and_timers(
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
) -> str:
    """探查 RHEL 定時排程作業：systemd 定時器 (timers) 與系統 cron 排程清單"""
    cmd = (
        "echo '=== Active Systemd Timers ===' && "
        "systemctl list-timers --no-pager --no-legend | head -n 30; "
        "echo '=== System Crontab (/etc/crontab) ===' && "
        "cat /etc/crontab 2>/dev/null; "
        "echo '=== Cron Daily/Hourly Directory Listing ===' && "
        "ls -la /etc/cron.daily /etc/cron.hourly 2>/dev/null | head -n 30"
    )
    return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password)


@mcp.tool()
def rhel_hardware_info(
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
) -> str:
    """探查 RHEL 主機硬體規格與虛擬化環境：CPU 架構、記憶體規格與虛擬化平台偵測"""
    cmd = (
        "echo '=== Virtualization Platform ===' && "
        "(systemd-detect-virt || echo '實體機或未偵測到虛擬化層'); "
        "echo '=== CPU Architecture (lscpu) ===' && "
        "(lscpu 2>/dev/null | grep -E '^(Architecture|CPU\\(s\\)|Model name|Thread|Core|Socket|Virtualization|NUMA)' "
        "|| uname -m); "
        "echo '=== Memory & Swap Details ===' && "
        "(free -h && grep -E '^(MemTotal|MemFree|MemAvailable|SwapTotal|SwapFree)' /proc/meminfo)"
    )
    return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password)


if __name__ == "__main__":
    mcp.run()
