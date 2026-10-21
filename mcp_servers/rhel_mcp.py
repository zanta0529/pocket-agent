"""遠端 RHEL 主機唯讀探查與維運診斷 MCP 伺服器。

安全設計原則：
1. 雙軌制連線認證：支援指定私鑰認證（含 Passphrase 解鎖）與加固帳密驗證智慧切換；
   強制停用 GSSAPI、全域 SSH Agent 並鎖定 IdentitiesOnly，絕不讀取或濫用開發者本機 ~/.ssh/ 全域金鑰。
2. 支援彈性設定：RHEL_HOST、RHEL_SSH_PORT、RHEL_SSH_USER、RHEL_SSH_PASSWORD、RHEL_SSH_KEY_PATH 皆可預填或留空；
   未設定項目於執行期動態詢問並僅快取於當前行程記憶體，絕不落盤、絕不暴露予 LLM 或寫入日誌。
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
import threading
import warnings
from pathlib import Path
from typing import BinaryIO

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer

mcp = MCPServer("rhel-ops")

MAX_OUTPUT_CHARS = 12000
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
    "/run/systemd/",
    "/srv/",
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
_REDACT_PEM_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)")
# 遇到敏感欄位時遮蔽整行剩餘內容，涵蓋 JSON、CLI 引數及未閉合的引號。
_REDACT_SECRET_LINE_RE = re.compile(
    r"(?im)\b(?:[\w-]*(?:password|passwd|secret|token|passphrase|api[_-]?key)[\w-]*"
    r"|authorization|database_url|aws_access_key_id|private_key|preshared_key)\b[^\r\n\x00]*"
)
_REDACT_URL_RE = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@")
MAX_CAPTURE_BYTES = 1024 * 1024  # 1 MB 安全串流緩衝上限，兼顧長日誌容錯與防爆記憶體 DoS


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
    redacted = _REDACT_SECRET_LINE_RE.sub("[REDACTED]", redacted)
    redacted = _REDACT_URL_RE.sub(r"\1[REDACTED]@", redacted)
    if len(redacted) > MAX_OUTPUT_CHARS:
        return (
            redacted[:MAX_OUTPUT_CHARS] + f"\n... (輸出過長已截斷，已達 {MAX_OUTPUT_CHARS} 字元上限)\n"
            "[提示：若需檢視後續內容或關鍵錯誤，請使用 grep 參數過濾關鍵字、縮短 since 時間範圍，或調整 max_lines 行數]"
        )
    return redacted


def _tty_prompt(prompt_text: str, *, is_password: bool = False) -> str:
    """當獨立執行且未由上層注入時，直接透過作業系統終端機設備（CONIN$/CONOUT$ 或 /dev/tty）詢問。"""
    if is_password:
        if os.name == "nt" and sys.stdin is not sys.__stdin__:
            raise ValueError("沒有安全的互動終端機，請由上層注入連線參數。")
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                return getpass.getpass(prompt_text)
            except (getpass.GetPassWarning, EOFError, OSError) as exc:
                raise ValueError("無法安全讀取密碼，請由上層注入連線參數。") from exc

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
    except OSError as exc:
        raise ValueError("沒有可用的互動終端機，請由上層注入連線參數；不讀取 MCP stdin。") from exc


def _resolve_connection_params(
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
) -> tuple[str, str, str, str, str]:
    """依「注入參數 → 記憶體快取 → 環境變數 → 終端機互動詢問」順序取得連線五要素。"""
    cached_host = _RUNTIME_CREDS.get("RHEL_HOST", "")
    cached_port = _RUNTIME_CREDS.get("RHEL_SSH_PORT", "")
    cached_user = _RUNTIME_CREDS.get("RHEL_SSH_USER", "")
    cached_key = _RUNTIME_CREDS.get("RHEL_SSH_KEY_PATH", "")

    input_host = ssh_host.strip()
    input_port = ssh_port.strip()
    input_user = ssh_user.strip()
    input_key = ssh_key_path.strip()

    host = input_host or cached_host or (os.getenv("RHEL_HOST") or "").strip()
    port = input_port or cached_port or (os.getenv("RHEL_SSH_PORT") or "").strip()
    user = input_user or cached_user or (os.getenv("RHEL_SSH_USER") or "").strip()
    key_path = input_key or cached_key or (os.getenv("RHEL_SSH_KEY_PATH") or "").strip()

    if not host:
        host = _tty_prompt("請輸入 RHEL 主機 IP 或位址 (RHEL_HOST): ").strip()
    if not port:
        port = _tty_prompt("請輸入 SSH 連接埠 (RHEL_SSH_PORT) [預設 22]: ").strip() or "22"
    if not user:
        user = _tty_prompt(f"請輸入 {host} 的 SSH 登入帳號 (RHEL_SSH_USER): ").strip()

    if not host:
        raise ValueError("未提供 RHEL 主機位址 (RHEL_HOST)。")
    if not user:
        raise ValueError("未提供 SSH 登入帳號 (RHEL_SSH_USER)。")

    if key_path:
        # 金鑰模式：驗證私鑰檔案是否存在
        resolved_key = Path(key_path).expanduser().resolve()
        if not resolved_key.is_file():
            raise ValueError(f"指定的 SSH 私鑰檔案不存在或無法存取：{key_path!r}")
        key_path = str(resolved_key)

    host = _validate_identifier(host, "RHEL_HOST")
    user = _validate_identifier(user, "RHEL_SSH_USER")
    port = str(int(_validate_port(port)))
    target = (host, port, user, key_path)
    cached_target = (cached_host, cached_port, cached_user, cached_key)
    env_key = (os.getenv("RHEL_SSH_KEY_PATH") or "").strip()
    if env_key:
        env_key = str(Path(env_key).expanduser().resolve())
    env_target = (
        (os.getenv("RHEL_HOST") or "").strip(),
        (os.getenv("RHEL_SSH_PORT") or "22").strip(),
        (os.getenv("RHEL_SSH_USER") or "").strip(),
        env_key,
    )
    password = ssh_password
    if not password and target == cached_target:
        password = _RUNTIME_CREDS.get("RHEL_SSH_PASSWORD", "")
    if not password and target == env_target:
        env_name = "RHEL_SSH_KEY_PASSPHRASE" if key_path else "RHEL_SSH_PASSWORD"
        password = os.getenv(env_name) or ""

    if not key_path:
        # 帳密模式：密碼為必填
        if not password:
            password = _tty_prompt(f"請輸入 {user}@{host} 的 SSH 登入密碼: ", is_password=True)
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
    _RUNTIME_CREDS["RHEL_SSH_KEY_PATH"] = key_path
    return host, port, user, password, key_path


def _build_ssh_args(
    remote_cmd: str,
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
) -> tuple[list[str], str, int]:
    host, port, user, password, key_path = _resolve_connection_params(
        ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path
    )
    strict_host = (os.getenv("RHEL_STRICT_HOST_KEY") or "accept-new").strip()

    try:
        timeout = max(3, min(int(os.getenv("RHEL_CONNECT_TIMEOUT") or "10"), 60))
    except ValueError:
        timeout = 10

    if strict_host not in ("yes", "accept-new", "no"):
        strict_host = "accept-new"

    args = [
        "ssh",
        "-F",
        "none",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        "PermitLocalCommand=no",
        "-o",
        "ProxyCommand=none",
        "-o",
        f"ConnectTimeout={timeout}",
        "-o",
        f"StrictHostKeyChecking={strict_host}",
        "-o",
        "BatchMode=no",
        "-o",
        "GSSAPIAuthentication=no",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "IdentityAgent=none",
        "-p",
        port,
    ]
    if key_path:
        args.extend(
            [
                "-o",
                "PubkeyAuthentication=yes",
                "-o",
                "PreferredAuthentications=publickey",
                "-o",
                "PasswordAuthentication=no",
                "-o",
                "KbdInteractiveAuthentication=no",
                "-i",
                key_path,
            ]
        )
    else:
        args.extend(
            [
                "-o",
                "PubkeyAuthentication=no",
                "-o",
                "PreferredAuthentications=keyboard-interactive,password",
                "-o",
                "NumberOfPasswordPrompts=1",
            ]
        )

    if strict_host == "no":
        null_dev = "NUL" if os.name == "nt" else "/dev/null"
        args.extend(["-o", f"UserKnownHostsFile={null_dev}"])

    target = f"{user}@{host}"
    args.extend(["--", target, remote_cmd])
    return args, password, timeout


def _capture_ssh(
    ssh_args: list[str], *, timeout: int, creationflags: int, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """雙管線並行排空；總接收量超限即中止，不回傳可能截斷的機密。"""
    with subprocess.Popen(
        ssh_args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creationflags,
        env=env,
    ) as proc:
        buffers = [bytearray(), bytearray()]
        lock = threading.Lock()
        exceeded = threading.Event()

        def drain(stream: BinaryIO, index: int) -> None:
            while chunk := stream.read(4096):
                with lock:
                    if exceeded.is_set():
                        continue
                    if sum(map(len, buffers)) + len(chunk) > MAX_CAPTURE_BYTES:
                        exceeded.set()
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    else:
                        buffers[index].extend(chunk)

        assert proc.stdout is not None and proc.stderr is not None
        threads = [
            threading.Thread(target=drain, args=(stream, index), daemon=True)
            for index, stream in enumerate((proc.stdout, proc.stderr))
        ]
        for thread in threads:
            thread.start()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            proc.wait()
            raise
        finally:
            for thread in threads:
                thread.join()
        if exceeded.is_set():
            return subprocess.CompletedProcess(ssh_args, 1, "", "輸出超過安全位元組上限，已中止並捨棄內容。")
        return subprocess.CompletedProcess(
            ssh_args, proc.returncode, *(data.decode("utf-8", errors="replace") for data in buffers)
        )


def _run_with_askpass(
    ssh_args: list[str], password: str, timeout: int, creationflags: int
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory(prefix="rhel_mcp_") as tmp_dir:
        tmp_path = Path(tmp_dir)

        # 透過專用 askpass.py 自隔離環境變數輸出密碼，100% 記憶體傳遞，杜絕密碼寫入硬碟暫存檔（零落盤）
        # 使用獨立 .py 腳本徹底消除 Windows cmd.exe -c 行內命令字串巢狀雙引號引發之 SyntaxError
        askpass_py = tmp_path / "askpass.py"
        askpass_py.write_text(
            "import os, sys\n"
            'sys.stdout.buffer.write((os.environ.get("RHEL_SSH_ASKPASS_PW", "") + "\\n").encode("utf-8"))\n',
            encoding="utf-8",
        )

        is_frozen = getattr(sys, "frozen", False)
        exe_path = Path(sys.executable)
        is_pocket_agent_exe = is_frozen or "pocket-agent" in exe_path.stem.lower()

        python_exe = str(exe_path)
        if not is_frozen and exe_path.name.lower() == "pythonw.exe":
            console_py = exe_path.with_name("python.exe")
            if console_py.exists():
                python_exe = str(console_py)

        extra_flag = " --internal-mcp-server" if is_pocket_agent_exe else ""

        if os.name == "nt":
            askpass_script = tmp_path / "askpass.bat"
            askpass_script.write_text(
                f'@"{python_exe}"{extra_flag} "{askpass_py}"\r\n',
                encoding="utf-8",
            )
        else:
            askpass_script = tmp_path / "askpass.sh"
            askpass_script.write_text(
                f'#!/bin/sh\nexec "{python_exe}"{extra_flag} "{askpass_py}"\n',
                encoding="utf-8",
            )
            askpass_script.chmod(0o700)

        env = dict(os.environ)
        env["RHEL_SSH_ASKPASS_PW"] = password
        env["SSH_ASKPASS"] = str(askpass_script)
        env["SSH_ASKPASS_REQUIRE"] = "force"
        env.setdefault("DISPLAY", "1")
        env.pop("SSH_AUTH_SOCK", None)

        return _capture_ssh(ssh_args, timeout=timeout + 15, creationflags=creationflags, env=env)


def _run_remote(
    remote_cmd: str,
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
) -> str:
    try:
        ssh_args, password, timeout = _build_ssh_args(
            remote_cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path
        )
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        proc = _run_with_askpass(ssh_args, password, timeout, creationflags)

        if proc.returncode == 255:
            # 遠端連線或金鑰/認證失敗。為防止本機行程命令列引數洩漏密碼（Process Argument Inspection），
            # 嚴格禁止使用 plink 傳遞明文密碼參數。強制使用安全 SSH_ASKPASS 通道。
            _RUNTIME_CREDS.clear()
            err_msg = proc.stderr.strip() if proc.stderr else "無法建立 OpenSSH 安全通道。"
            return (
                f"[ERROR] [CONN_AUTH] 遠端 SSH 連線或認證失敗 (exit 255): {_redact_and_truncate(err_msg)}\n"
                "提示：請確認目標主機已啟用密碼/金鑰驗證，或檢查網路與金鑰檔案權限設定。"
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
        if err_out:
            combined = f"{combined}\n[STDERR]\n{err_out}"
        if proc.returncode != 0:
            combined = f"[ERROR] 遠端指令部分或全部失敗 (exit {proc.returncode})\n{combined}"
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
    ssh_key_path: str = "",
) -> str:
    """探查 RHEL 主機系統概況：OS 版本、Kernel、Uptime、CPU/記憶體使用量與前 10 大高負載行程"""
    cmd = (
        "echo '=== OS Release ===' && cat /etc/redhat-release /etc/os-release 2>/dev/null | head -n 15; "
        "echo '=== Kernel & Uptime ===' && uname -srm && uptime; "
        "echo '=== Memory (MB) ===' && free -m; "
        "echo '=== Top Processes (by CPU) ===' && "
        "ps -eo pid,ppid,user,%cpu,%mem,stat,comm --sort=-%cpu | head -n 11"
    )
    return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path)


@mcp.tool()
def rhel_storage_and_fs(
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
) -> str:
    """探查 RHEL 儲存與檔案系統狀態：掛載點空間使用率 (df -hT)、Inode 使用率 (df -i) 與區塊裝置 (lsblk)"""
    cmd = (
        "echo '=== Filesystem Usage ===' && df -hT -x tmpfs -x devtmpfs; "
        "echo '=== Inode Usage ===' && df -i -x tmpfs -x devtmpfs; "
        "echo '=== Block Devices ===' && lsblk -o NAME,FSTYPE,SIZE,MOUNTPOINT,UUID"
    )
    return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path)


@mcp.tool()
def rhel_network_status(
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
) -> str:
    """探查 RHEL 網路狀態：網路介面與 IP、路由表、DNS 設定與目前監聽的 TCP/UDP 連接埠"""
    cmd = (
        "echo '=== Network Interfaces ===' && ip -br addr; "
        "echo '=== Routing Table ===' && ip route; "
        "echo '=== DNS Resolver ===' && cat /etc/resolv.conf 2>/dev/null; "
        "echo '=== Listening Ports ===' && ss -tuln"
    )
    return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path)


@mcp.tool()
def rhel_service_status(
    service_name: str = "",
    failed_only: bool = False,
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
) -> str:
    """查詢 RHEL systemd 服務狀態。不指定 service_name 時列出失敗或活動中的服務；指定時顯示該服務詳細狀態"""
    if failed_only or not service_name.strip():
        state_flag = "--state=failed" if failed_only else "--type=service --state=running,failed"
        cmd = f"systemctl list-units {state_flag} --no-pager --no-legend | head -n 80"
        return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path)

    try:
        safe_unit = _validate_identifier(service_name, "service_name")
    except ValueError as e:
        return f"[ERROR] {e}"

    quoted = shlex.quote(safe_unit)
    cmd = f"systemctl status {quoted} --no-pager -n 25"
    return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path)


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
    ssh_key_path: str = "",
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

    return _run_remote(" ".join(parts), ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path)


@mcp.tool()
def rhel_security_status(
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
) -> str:
    """探查 RHEL 安全性子系統狀態：SELinux 模式與狀態、firewalld 防火牆規則摘要，以及待安裝的安全性更新"""
    cmd = (
        "echo '=== SELinux Status ===' && (sestatus || getenforce) 2>/dev/null; "
        "echo '=== Recent SELinux AVC Denials ===' && "
        "(bash -o pipefail -c 'ausearch -m avc -ts recent | tail -n 20' || "
        "journalctl -t setroubleshoot --no-pager -n 15 || echo '[ERROR] AVC 查詢失敗'); "
        "echo '=== Firewalld State ===' && "
        "(firewall-cmd --state && firewall-cmd --list-all) 2>/dev/null || echo 'firewalld 未執行或需要權限'; "
        "echo '=== Security Updates (dnf) ===' && "
        "dnf --cacheonly --setopt=logfile=/dev/null --setopt=debuglevel=0 "
        "updateinfo summary --security || "
        "echo '[ERROR] 安全更新查詢失敗；快取可能不足或過期，不自動更新中繼資料'"
    )
    return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path)


@mcp.tool()
def rhel_read_config(
    file_path: str,
    max_lines: int = 120,
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
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
    return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path)


@mcp.tool()
def rhel_list_directory(
    dir_path: str,
    show_selinux: bool = False,
    max_lines: int = 100,
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
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
    return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path)


@mcp.tool()
def rhel_selinux_context(
    target_path: str,
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
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
    return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path)


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
    ssh_key_path: str = "",
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
        return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path)

    cols = "pid,ppid,user,%cpu,%mem,stat,label,comm" if show_selinux else "pid,ppid,user,%cpu,%mem,stat,comm"

    if search.strip():
        try:
            safe_search = _validate_search_pattern(search, "search")
        except ValueError as e:
            return f"[ERROR] {e}"
        quoted_search = shlex.quote(safe_search)
        cmd = (
            f"echo '=== Search Processes matching {quoted_search} ===' && "
            f"ps -eo {cols} | grep -i -- {quoted_search} | grep -v grep | head -n {safe_lines}"
        )
        return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path)

    sort_clean = sort_by.strip().lower()
    if sort_clean not in _VALID_SORT_KEYS:
        return f"[ERROR] 非法的 sort_by 參數：{sort_by!r}（僅允許 'cpu'、'mem' 或 'memory'）"

    sort_col = "-%mem" if sort_clean in ("mem", "memory") else "-%cpu"
    cmd = (
        f"echo '=== Top Processes (sorted by {sort_clean}) ===' && "
        f"ps -eo {cols} --sort={sort_col} | head -n {safe_lines}"
    )
    return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path)


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
    ssh_key_path: str = "",
) -> str:
    """利用 ss (Socket Statistics) 結構化探查網路連線狀態（支援 TCP/UDP 協議、狀態過濾、特定 Port 與行程關聯）"""
    proto_clean = protocol.strip().lower()
    if proto_clean not in {"all", "tcp", "udp"}:
        return "[ERROR] 非法的 protocol 參數（僅允許 all、tcp、udp）。"
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
    return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path)


@mcp.tool()
def rhel_package_info(
    package_name: str,
    verify_integrity: bool = False,
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
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
            f"(rpm -qi {quoted} 2>/dev/null || "
            f"dnf --cacheonly --setopt=logfile=/dev/null --setopt=debuglevel=0 info {quoted} || "
            "echo '[ERROR] 查無套件或快取不足，不自動更新中繼資料'); "
            "echo '=== Changelog Summary (recent) ===' && "
            f"rpm -q --changelog {quoted} 2>/dev/null | head -n 20"
        )
    return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path)


@mcp.tool()
def rhel_container_status(
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
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
    return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path)


@mcp.tool()
def rhel_cron_and_timers(
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
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
    return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path)


@mcp.tool()
def rhel_hardware_info(
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
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
    return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path)


if __name__ == "__main__":
    mcp.run()
