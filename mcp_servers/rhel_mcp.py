"""遠端 RHEL 主機唯讀探查與維運診斷 MCP 伺服器。

安全設計原則：
1. 雙軌制連線認證：支援指定私鑰認證（含 Passphrase 解鎖）與加固帳密驗證智慧切換；
   強制停用 GSSAPI、全域 SSH Agent 並鎖定 IdentitiesOnly，絕不讀取或濫用開發者本機 ~/.ssh/ 全域金鑰。
2. 支援彈性設定：RHEL_HOST、RHEL_SSH_PORT、RHEL_SSH_USER、RHEL_SSH_PASSWORD、RHEL_SSH_KEY_PATH 皆可預填或留空；
   未設定項目於執行期詢問並快取於當前行程；不主動將密碼寫入暫存腳本或日誌。
3. 提供預先定義的唯讀查詢；參數經驗證及 Shell 引號保護，不提供任意指令入口。
4. 設定檔僅回傳批准的 SELINUX 列舉值；日誌僅回傳等級計數，目錄列舉暫停提供。
   其他診斷仍使用已知機密遮罩，不能辨識所有未知機密，亦不保證整段對話不落盤。
   建議使用專用低權限帳號；可寫目錄的替換競態與 ASKPASS 後代清理仍有平台限制。
"""

import getpass
import ipaddress
import json
import os
import posixpath
import re
import shlex
import subprocess
import sys
import tempfile
import time
import warnings
from pathlib import Path

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
_CREDS_UPDATED_AT: float = 0.0
_CREDS_TTL_SECONDS: float = 600.0  # 10 分鐘閒置清除


def _check_creds_ttl() -> None:
    global _CREDS_UPDATED_AT
    now = time.monotonic()
    if _RUNTIME_CREDS and (now - _CREDS_UPDATED_AT > _CREDS_TTL_SECONDS):
        _RUNTIME_CREDS.clear()
        _CREDS_UPDATED_AT = 0.0


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
    ".netrc",
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
    r"|psk|authorization|database_url|aws_access_key_id|private_key|preshared_key)\b[^\r\n\x00]*"
)
_REDACT_URL_RE = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@")
MAX_CAPTURE_BYTES = 1024 * 1024  # 1 MB 安全串流緩衝上限，兼顧長日誌容錯與防爆記憶體 DoS


def _get_audit_log_path() -> Path:
    """解析集中化稽核日誌 (agent.log) 路徑。"""
    env_log = (os.getenv("POCKET_AGENT_LOG_FILE") or "").strip()
    if env_log:
        return Path(env_log).expanduser().resolve()
    try:
        from pocket_agent.system.paths import log_dir

        return log_dir() / "agent.log"
    except ImportError:
        src_dir = Path(__file__).resolve().parent.parent / "src"
        if src_dir.is_dir() and str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
            try:
                from pocket_agent.system.paths import log_dir

                return log_dir() / "agent.log"
            except ImportError:
                pass
        return Path.home() / ".pocket-agent" / "logs" / "agent.log"


def _format_audit_target(host: str = "", port: str = "", user: str = "") -> str:
    h = host.strip() or _RUNTIME_CREDS.get("RHEL_HOST", "") or (os.getenv("RHEL_HOST") or "").strip() or "?"
    p = port.strip() or _RUNTIME_CREDS.get("RHEL_SSH_PORT", "") or (os.getenv("RHEL_SSH_PORT") or "").strip() or "22"
    u = user.strip() or _RUNTIME_CREDS.get("RHEL_SSH_USER", "") or (os.getenv("RHEL_SSH_USER") or "").strip() or "?"
    return f"{u}@{h}:{p}"


def _audit_log(
    tool_name: str,
    command: str,
    *,
    target: str = "",
    status: str = "OK",
    detail: str = "",
) -> None:
    """將 MCP 工具執行的遠端指令與敏感操作寫入 Pocket Agent 集中化日誌 (agent.log) 供稽核使用。"""
    try:
        log_path = _get_audit_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        now_t = time.time()
        lt = time.localtime(now_t)
        ms = int((now_t - int(now_t)) * 1000)
        ts = f"{time.strftime('%Y-%m-%d %H:%M:%S', lt)},{ms:03d}"
        safe_cmd = " ".join(_REDACT_KV_RE.sub(r"\1[REDACTED]", command).splitlines()).strip()
        safe_target = " ".join(_REDACT_KV_RE.sub(r"\1[REDACTED]", target).splitlines()).strip()
        safe_detail = " ".join(_REDACT_KV_RE.sub(r"\1[REDACTED]", detail).splitlines()).strip()
        parts = [f"{ts} [AUDIT] [MCP:rhel-ops] tool={tool_name} status={status}"]
        if safe_target:
            parts.append(f"target={safe_target}")
        if safe_cmd:
            parts.append(f"command={safe_cmd!r}")
        if safe_detail:
            parts.append(f"detail={safe_detail!r}")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(" ".join(parts) + "\n")
    except Exception:
        pass


def _validate_host(value: str) -> str:
    clean = value.strip()
    if not clean or clean.startswith("-"):
        raise ValueError("非法的 RHEL_HOST 參數格式。")
    raw_ip = clean.strip("[]")
    try:
        ip = ipaddress.ip_address(raw_ip)
        return f"[{ip}]" if ip.version == 6 else str(ip)
    except ValueError:
        pass
    if not _IDENT_RE.match(clean):
        raise ValueError("非法的 RHEL_HOST 參數格式（僅允許合法的 IPv4、IPv6 或主機名稱）。")
    return clean


def _validate_identifier(value: str, field_name: str) -> str:
    clean = value.strip()
    if not clean or clean.startswith("-") or not _IDENT_RE.match(clean):
        raise ValueError(f"非法的 {field_name} 參數格式（僅允許英數字、點、底線、連字號與 @，且不得以連字號開頭）")
    return clean


def _validate_search_pattern(value: str, field_name: str, max_len: int = 50) -> str:
    clean = value.strip()
    if not clean or clean.startswith("-") or not _SEARCH_RE.match(clean) or len(clean) > max_len:
        raise ValueError(
            f"非法的 {field_name} 參數格式（僅允許英數字、斜線、冒號、點、空白等安全符號，且限 {max_len} 字元以內）"
        )
    return clean


def _validate_port(value: str) -> str:
    clean = value.strip()
    if not clean.isdigit() or not (1 <= int(clean) <= 65535):
        raise ValueError("非法的連接埠數值（必須為 1~65535 之間的整數）")
    return clean


def _validate_pid(value: str | int) -> str:
    clean = str(value).strip()
    if not clean.isdigit() or int(clean) <= 0:
        raise ValueError("非法的行程 PID 數值（必須為正整數）")
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
        raise ValueError(f"拒絕存取：指定路徑不在允許的唯讀探查目錄白名單內（{allowed_str}）。")

    lower_path = norm.lower()
    for kw in _DENIED_PATH_KEYWORDS:
        if kw in lower_path:
            raise ValueError("拒絕存取：路徑包含敏感保護關鍵字，禁止讀取。")
    return norm


def _remote_path_guard(path: str, *, directory: bool) -> str:
    """以同一套規則檢查實際路徑；不能消除可寫目錄內的替換競態。"""
    denied = "|".join(f"*{keyword}*" for keyword in _DENIED_PATH_KEYWORDS)
    allowed = [f"{prefix.rstrip('/')}/*" for prefix in _ALLOWED_CONFIG_PREFIXES]
    if directory:
        allowed.extend(prefix.rstrip("/") for prefix in _ALLOWED_CONFIG_PREFIXES)
    kind = "d" if directory else "f"
    return (
        f"real=$(realpath -e -- {shlex.quote(path)} 2>/dev/null) || "
        "{ echo '[ERROR] 路徑不存在或無法解析'; exit 1; }; "
        "lower=$(printf '%s' \"$real\" | LC_ALL=C tr '[:upper:]' '[:lower:]') || exit 1; "
        f'case "$lower" in {denied}) '
        "echo '[ERROR] 拒絕存取：解析後路徑包含敏感關鍵字'; exit 1 ;; esac; "
        f'case "$real" in {"|".join(allowed)}) ;; '
        "*) echo '[ERROR] 拒絕存取：解析後路徑不在目錄白名單內'; exit 1 ;; esac; "
        f'[ -{kind} "$real" ] || '
        "{ echo '[ERROR] 不支援的檔案類型'; exit 1; }; "
    )


def _diagnostic_command(command: str) -> str:
    """使用 RHEL Bash 保存管線錯誤；限量顯示仍排空上游，避免 head 的 SIGPIPE。"""
    script = (
        "export LC_ALL=C; set -E -o pipefail; "
        'trap \'rc=$?; printf "[ERROR] 查詢區段失敗 (exit %s)\\n" "$rc" >&2\' ERR; ' + command
    )
    return "bash -c " + shlex.quote(script)


def _redact_and_truncate(text: str) -> str:
    if not text:
        return "(沒有輸出)"
    if any(ord(char) < 32 and char not in "\n\r\t" for char in text) or "\ufffd" in text:
        return "[ERROR] 輸出包含二進位或無法安全解碼的內容，已捨棄。"
    # 未知縮排／引號語法無法可靠判定敏感值邊界，整份拒絕而非只遮住第一行。
    if re.search(
        r"(?im)\b(?:[\w-]*(?:password|passwd|secret|token|passphrase|api[_-]?key)[\w-]*"
        r"|psk|authorization|private_key|preshared_key)\b[\"']?\s*[:=][ \t]*"
        r"(?:[|>][^\r\n]*|\"[^\"\r\n]*|'[^'\r\n]*|[ \t]*)(?:\r?\n|\Z)",
        text,
    ):
        return "[ERROR] 偵測到無法安全判定範圍的敏感欄位，已捨棄內容。"
    if re.search(
        r"(?im)^[^\r\n]*(?:password|passwd|secret|token|passphrase|api[_-]?key|psk)"
        r"[^\r\n]*[:=][^\r\n]*\r?\n[ \t]+\S",
        text,
    ):
        return "[ERROR] 敏感欄位後含縮排延續內容，已捨棄。"
    if re.search(r"(?im)^\s*(?:machine|default|login|password)\s*$", text):
        return "[ERROR] 偵測到跨行憑證格式，已捨棄內容。"
    redacted = _REDACT_PEM_RE.sub("[REDACTED PRIVATE KEY]", text)
    redacted = _REDACT_KV_RE.sub(r"\1[REDACTED]", redacted)
    redacted = _REDACT_SECRET_LINE_RE.sub("[REDACTED]", redacted)
    redacted = _REDACT_URL_RE.sub(r"\1[REDACTED]@", redacted)
    if len(redacted) > MAX_OUTPUT_CHARS:
        return (
            redacted[:MAX_OUTPUT_CHARS] + f"\n... (輸出過長已截斷，已達 {MAX_OUTPUT_CHARS} 字元上限)\n"
            "[提示：若需檢視後續內容或縮小範圍，請指定更精確的查詢條件、縮短 since 時間範圍，或調整行數上限]"
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
    _check_creds_ttl()
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

    host = _validate_host(host)
    user = _validate_identifier(user, "RHEL_SSH_USER")
    port = str(int(_validate_port(port)))
    target = (host, port, user, key_path)
    cached_target = (cached_host, cached_port, cached_user, cached_key)
    env_key = (os.getenv("RHEL_SSH_KEY_PATH") or "").strip()
    if env_key:
        env_key = str(Path(env_key).expanduser().resolve())
    raw_env_host = (os.getenv("RHEL_HOST") or "").strip()
    try:
        norm_env_host = _validate_host(raw_env_host) if raw_env_host else ""
    except ValueError:
        norm_env_host = raw_env_host
    env_target = (
        norm_env_host,
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
            raise ValueError("未提供 SSH 登入密碼 (RHEL_SSH_PASSWORD)；未指定私鑰時必須提供密碼驗證。")

    _RUNTIME_CREDS["RHEL_HOST"] = host
    _RUNTIME_CREDS["RHEL_SSH_PORT"] = port
    _RUNTIME_CREDS["RHEL_SSH_USER"] = user
    _RUNTIME_CREDS["RHEL_SSH_PASSWORD"] = password
    _RUNTIME_CREDS["RHEL_SSH_KEY_PATH"] = key_path
    global _CREDS_UPDATED_AT
    _CREDS_UPDATED_AT = time.monotonic()
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
    try:
        from pocket_agent.system.paths import user_dir

        default_known_hosts = user_dir() / "rhel_known_hosts"
    except ImportError:
        default_known_hosts = Path.home() / ".pocket-agent" / "rhel_known_hosts"

    known_hosts_file = os.getenv("RHEL_KNOWN_HOSTS_FILE", str(default_known_hosts))
    Path(known_hosts_file).parent.mkdir(parents=True, exist_ok=True)
    if not Path(known_hosts_file).exists():
        Path(known_hosts_file).touch(mode=0o600, exist_ok=True)

    # 若專屬 known_hosts 缺少該主機，但本機 ~/.ssh/known_hosts 已有使用者過去核可之可信紀錄，安全同步匯入
    if Path(known_hosts_file).exists():
        try:
            content = Path(known_hosts_file).read_text(encoding="utf-8", errors="ignore")
            clean_host = host.strip("[]")
            host_prefixes = (f"{clean_host} ", f"[{clean_host}]:", f"{host} ")
            existing_lines = [line.strip() for line in content.splitlines() if line.strip()]
            has_entry = any(line.startswith(host_prefixes) for line in existing_lines)
            if not has_entry:
                user_known_hosts = Path.home() / ".ssh" / "known_hosts"
                if user_known_hosts.exists():
                    user_lines = [
                        line
                        for line in user_known_hosts.read_text(encoding="utf-8", errors="ignore").splitlines()
                        if line.strip() and line.strip().startswith(host_prefixes)
                    ]
                    if user_lines:
                        prefix_nl = "\n" if content and not content.endswith("\n") else ""
                        with open(known_hosts_file, "a", encoding="utf-8") as f:
                            if prefix_nl:
                                f.write(prefix_nl)
                            for entry in user_lines:
                                f.write(f"{entry}\n")
        except Exception:
            pass

    try:
        timeout = max(3, min(int(os.getenv("RHEL_CONNECT_TIMEOUT") or "10"), 60))
    except ValueError:
        timeout = 10

    strict_env = (os.getenv("RHEL_STRICT_HOST_KEY") or "accept-new").strip().lower()
    strict_host = "yes" if strict_env in ("yes", "strict", "true", "1") else "accept-new"

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
        f"UserKnownHostsFile={known_hosts_file}",
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

    target = f"{user}@{host}"
    args.extend(["--", target, remote_cmd])
    return args, password, timeout


def _capture_ssh(
    ssh_args: list[str], *, timeout: float, creationflags: int, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """非阻塞輪詢雙管線；共同期限涵蓋主行程及 EOF，失敗時捨棄所有片段。

    Python 3.14 支援 Windows 管線 set_blocking；不支援時直接失敗，不退回阻塞讀取。
    本機強制終止 SSH 主行程與所有後代行程。
    """
    deadline = time.monotonic() + timeout
    extra_popen_args = {}
    if os.name != "nt":
        extra_popen_args["start_new_session"] = True
    proc = subprocess.Popen(
        ssh_args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        creationflags=creationflags,
        env=env,
        **extra_popen_args,
    )
    buffers = [bytearray(), bytearray()]
    streams = [proc.stdout, proc.stderr]
    total = 0
    try:
        assert all(stream is not None for stream in streams)
        active = {index: stream.fileno() for index, stream in enumerate(streams) if stream is not None}
        for fd in active.values():
            os.set_blocking(fd, False)
        while active or proc.poll() is None:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("ssh", timeout)
            progressed = False
            for index, fd in list(active.items()):
                try:
                    chunk = os.read(fd, 4096)
                except BlockingIOError:
                    continue
                if not chunk:
                    del active[index]
                    continue
                progressed = True
                total += len(chunk)
                if total > MAX_CAPTURE_BYTES:
                    return subprocess.CompletedProcess(ssh_args, 1, "", "輸出超過安全位元組上限，已中止並捨棄內容。")
                buffers[index].extend(chunk)
            if not progressed:
                time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        try:
            output = [data.decode("utf-8", errors="strict") for data in buffers]
        except UnicodeDecodeError:
            return subprocess.CompletedProcess(ssh_args, 1, "", "輸出無法安全解碼，已捨棄內容。")
        return subprocess.CompletedProcess(ssh_args, proc.returncode, *output)
    finally:
        # 無讀取執行緒，也不使用 Popen context manager 的無上限 wait。
        try:
            if proc.poll() is None:
                if os.name == "nt":
                    try:
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            check=False,
                            creationflags=subprocess.CREATE_NO_WINDOW,
                        )
                    except Exception:
                        proc.kill()
                else:
                    try:
                        os.killpg(os.getpgid(proc.pid), 9)
                    except Exception:
                        proc.kill()
            proc.wait(timeout=1)
        except OSError, subprocess.TimeoutExpired:
            pass
        finally:
            for stream in streams:
                if stream is not None:
                    stream.close()


def _run_with_askpass(
    ssh_args: list[str], password: str, timeout: int, creationflags: int
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory(prefix="rhel_mcp_") as tmp_dir:
        tmp_path = Path(tmp_dir)

        # 暫存腳本不含密碼；密碼透過子行程環境傳遞，仍受本機行程權限與系統診斷機制影響。
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

        safe_keys = {
            "SYSTEMROOT",
            "SYSTEMDRIVE",
            "WINDIR",
            "PROGRAMDATA",
            "PROGRAMFILES",
            "PROGRAMFILES(X86)",
            "PATH",
            "PATHEXT",
            "TMP",
            "TEMP",
            "USERPROFILE",
            "ALLUSERSPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
            "HOME",
            "HOMEPATH",
            "HOMEDRIVE",
            "COMSPEC",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "TERM",
            "DISPLAY",
            "SSH_ASKPASS_REQUIRE",
        }
        env = {k: v for k, v in os.environ.items() if k.upper() in safe_keys}
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
    *,
    tool_name: str = "rhel_remote",
) -> str:
    try:
        ssh_args, password, timeout = _build_ssh_args(
            _diagnostic_command(remote_cmd), ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path
        )
        target_str = _format_audit_target(ssh_host, ssh_port, ssh_user)
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        proc = _run_with_askpass(ssh_args, password, timeout, creationflags)

        if proc.returncode == 255:
            stderr_lower = (proc.stderr or "").lower()
            if "host key verification failed" in stderr_lower or "known_hosts" in stderr_lower:
                _audit_log(
                    tool_name,
                    remote_cmd,
                    target=target_str,
                    status="HOST_KEY_MISMATCH",
                    detail="Host key verification failed (exit 255)",
                )
                return (
                    "[ERROR] [HOST_KEY_MISMATCH] 目標主機金鑰驗證失敗 (exit 255)\n"
                    "提示：目標主機公鑰與專屬已知清單紀錄不符（可能主機金鑰曾變更或存在安全風險），連線已被拒絕。"
                )

            if "permission denied" in stderr_lower:
                _audit_log(
                    tool_name,
                    remote_cmd,
                    target=target_str,
                    status="AUTH_FAIL",
                    detail="Permission denied (exit 255)",
                )
                _RUNTIME_CREDS.clear()
                global _CREDS_UPDATED_AT
                _CREDS_UPDATED_AT = 0.0
                return (
                    "[ERROR] [CONN_AUTH] 遠端 SSH 認證失敗 (Permission denied, exit 255)\n"
                    "提示：請確認帳號與密碼/金鑰是否正確，且目標主機已啟用對應認證方式。"
                )

            _audit_log(
                tool_name,
                remote_cmd,
                target=target_str,
                status="CONN_FAIL",
                detail="SSH connection or authentication failed (exit 255)",
            )
            return (
                "[ERROR] [CONN_AUTH] 遠端 SSH 連線或認證失敗 (exit 255)\n"
                "提示：請確認網路連線、目標主機防火牆與 SSH 連接埠，或檢查已知主機清單。"
            )

        combined = (proc.stdout or "").strip()

        if proc.returncode != 0 and not combined:
            stderr_msg = (proc.stderr or "").strip()
            if stderr_msg in (
                "輸出超過安全位元組上限，已中止並捨棄內容。",
                "輸出無法安全解碼，已捨棄內容。",
            ):
                _audit_log(
                    tool_name,
                    remote_cmd,
                    target=target_str,
                    status="ERROR",
                    detail=stderr_msg,
                )
                return f"[ERROR] {stderr_msg}"
            _audit_log(
                tool_name,
                remote_cmd,
                target=target_str,
                status="ERROR",
                detail=f"exit {proc.returncode}",
            )
            return f"[ERROR] 遠端執行失敗 (exit {proc.returncode})"

        if proc.returncode != 0:
            _audit_log(
                tool_name,
                remote_cmd,
                target=target_str,
                status="PARTIAL_ERROR",
                detail=f"exit {proc.returncode}",
            )
            combined = f"[ERROR] 遠端指令部分或全部失敗 (exit {proc.returncode})\n{combined}"
        else:
            _audit_log(tool_name, remote_cmd, target=target_str, status="OK")
        return _redact_and_truncate(combined)
    except ValueError as e:
        _audit_log(
            tool_name,
            remote_cmd,
            target=_format_audit_target(ssh_host, ssh_port, ssh_user),
            status="AUTH_FAIL",
            detail=str(e),
        )
        _RUNTIME_CREDS.clear()
        _CREDS_UPDATED_AT = 0.0
        return "[ERROR] [CONN_AUTH] 連線參數或安全檢查失敗，請確認設定。"
    except subprocess.TimeoutExpired:
        _audit_log(
            tool_name,
            remote_cmd,
            target=_format_audit_target(ssh_host, ssh_port, ssh_user),
            status="TIMEOUT",
            detail="SSH command execution timed out",
        )
        return "[ERROR] SSH 遠端指令執行逾時，已中止連線。"
    except Exception:
        _audit_log(
            tool_name,
            remote_cmd,
            target=_format_audit_target(ssh_host, ssh_port, ssh_user),
            status="ERROR",
            detail="SSH execution exception",
        )
        return "[ERROR] SSH 執行或安全擷取失敗，未回傳原始例外內容。"


@mcp.tool()
def rhel_system_overview(
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
) -> str:
    """探查 RHEL 主機系統概況：OS 版本、Kernel、Uptime、時間同步、PSI 壓力、記憶體與高負載行程"""
    cmd = (
        "echo '=== OS Release ===' && cat /etc/redhat-release /etc/os-release 2>/dev/null | sed -n '1,15p'; "
        "echo '=== Kernel & Uptime ===' && uname -srm && uptime && "
        "(rpm -q kernel --last 2>/dev/null | sed -n '1,5p' || true) && "
        "(grubby --default-kernel 2>/dev/null || true) && "
        "(timedatectl show --property=NTPSynchronized,NTP,Timezone 2>/dev/null || true) && "
        "(chronyc tracking 2>/dev/null || true) && "
        "(tuned-adm active 2>/dev/null || true); "
        "echo '=== CPU / IO Wait & Pressure (vmstat / PSI) ===' && "
        "(vmstat -w 2>/dev/null || true) && "
        "(head -n 2 /proc/pressure/cpu /proc/pressure/memory /proc/pressure/io 2>/dev/null "
        "|| echo 'PSI 未啟用 (預設需核心參數 psi=1)'); "
        "echo '=== Memory (MB) ===' && free -m; "
        "echo '=== Top Processes (by CPU) ===' && "
        "ps -eo pid,ppid,user,%cpu,%mem,stat,comm --sort=-%cpu | sed -n '1,11p'"
    )
    return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path, tool_name="rhel_system_overview")


@mcp.tool()
def rhel_storage_and_fs(
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
) -> str:
    """探查 RHEL 儲存與檔案系統：掛載空間 (df -hT)、Inode、區塊裝置與掛載選項 (findmnt)"""
    cmd = (
        "echo '=== Filesystem Usage ===' && df -hT -x tmpfs -x devtmpfs; "
        "echo '=== Inode Usage ===' && df -i -x tmpfs -x devtmpfs; "
        "echo '=== Block Devices ===' && lsblk -o NAME,FSTYPE,SIZE,MOUNTPOINT,UUID; "
        "echo '=== Mount Options ===' && "
        "(findmnt -no TARGET,SOURCE,FSTYPE,OPTIONS --list -t nodevtmpfs,nosquashfs 2>/dev/null "
        "| grep -E '^(/|/boot|/home|/tmp|/var|/dev/shm|/opt|/srv)([[:space:]]|/|$)' || true)"
    )
    return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path, tool_name="rhel_storage_and_fs")


@mcp.tool()
def rhel_network_status(
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
) -> str:
    """探查 RHEL 網路狀態：網路介面、路由表、DNS 與監聽的 TCP/UDP 連接埠及行程"""
    cmd = (
        "echo '=== Network Interfaces ===' && ip -br addr; "
        "echo '=== Routing Table ===' && ip route; "
        "echo '=== DNS Resolver ===' && cat /etc/resolv.conf 2>/dev/null; "
        "echo '=== Listening Ports ===' && ss -tulnp"
    )
    return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path, tool_name="rhel_network_status")


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
    """查詢 systemd 服務狀態；未指定服務或 failed_only 時回傳前 50 筆結構化單元狀態與總數。

    指定 service_name 時回傳結構化屬性；支援以逗號或空白分隔一次傳入多個服務名稱（最多 10 個，
    例如 'sshd,chronyd,tuned,auditd'），請優先合併呼叫以減少 SSH 連線次數。
    """
    if failed_only or not service_name.strip():
        state_flag = "--state=failed" if failed_only else "--type=service --state=running,failed"
        cmd = (
            f"systemctl list-units {state_flag} --no-pager --no-legend --plain "
            '| awk \'NR<=50 {u=$1; sub(/@[^.]+/, "@*", u); print u, $2, $3, $4} '
            'END {print "matching_units=" NR}\''
        )
        return _run_remote(
            cmd,
            ssh_host,
            ssh_port,
            ssh_user,
            ssh_password,
            ssh_key_path,
            tool_name="rhel_service_status",
        )

    raw_units = [u for u in re.split(r"[\s,]+", service_name.strip()) if u]
    if len(raw_units) > 10:
        _audit_log(
            "rhel_service_status",
            f"service_name={service_name!r}",
            target=_format_audit_target(ssh_host, ssh_port, ssh_user),
            status="BLOCKED",
            detail="Too many service names (max 10)",
        )
        return "[ERROR] 單次查詢的服務數量不得超過 10 個。"

    quoted_units: list[str] = []
    for unit_item in raw_units:
        try:
            safe_unit = _validate_identifier(unit_item, "service_name")
        except ValueError as e:
            _audit_log(
                "rhel_service_status",
                f"service_name={service_name!r}",
                target=_format_audit_target(ssh_host, ssh_port, ssh_user),
                status="BLOCKED",
                detail=str(e),
            )
            return _redact_and_truncate(f"[ERROR] {e}")
        quoted_units.append(shlex.quote(safe_unit))

    joined_units = " ".join(quoted_units)
    props = (
        "Id,LoadState,ActiveState,SubState,UnitFileState,TriggeredBy,"
        "MainPID,ExecMainCode,ExecMainStatus,Result,NRestarts,"
        "ActiveEnterTimestamp,InactiveEnterTimestamp"
    )
    cmd = f"systemctl show {joined_units} --no-pager --property={props}"
    return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path, tool_name="rhel_service_status")


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
    """查詢有界 journald 等級計數；不回傳訊息原文。grep 暫不支援，lines 為樣本上限。"""
    if grep.strip():
        _audit_log(
            "rhel_query_journal",
            f"unit={unit!r} grep={grep!r}",
            target=_format_audit_target(ssh_host, ssh_port, ssh_user),
            status="BLOCKED",
            detail="grep parameter rejected in safe summary mode",
        )
        return "[ERROR] 安全摘要模式不接受訊息關鍵字；請使用 unit、priority 與 since 篩選。"
    parts = ["journalctl", "--no-pager", "--output=json", "--output-fields=PRIORITY", "--quiet"]

    if unit.strip():
        try:
            safe_unit = _validate_identifier(unit, "unit")
        except ValueError as e:
            _audit_log(
                "rhel_query_journal",
                f"unit={unit!r}",
                target=_format_audit_target(ssh_host, ssh_port, ssh_user),
                status="BLOCKED",
                detail=str(e),
            )
            return _redact_and_truncate(f"[ERROR] {e}")
        parts.extend(["-u", shlex.quote(safe_unit)])

    prio_aliases = {"error": "err", "warn": "warning", "critical": "crit"}
    prio = prio_aliases.get(priority.strip().lower(), priority.strip().lower())
    if prio and prio not in ("all", "none"):
        if prio not in _VALID_PRIORITIES:
            _audit_log(
                "rhel_query_journal",
                f"priority={priority!r}",
                target=_format_audit_target(ssh_host, ssh_port, ssh_user),
                status="BLOCKED",
                detail="Invalid priority",
            )
            return f"[ERROR] 非法的 priority 參數（允許值：{sorted(_VALID_PRIORITIES)}）"
        parts.extend(["-p", shlex.quote(prio)])

    since_clean = since.strip()
    if since_clean:
        if not _SINCE_RE.match(since_clean) or len(since_clean) > 40:
            _audit_log(
                "rhel_query_journal",
                f"since={since!r}",
                target=_format_audit_target(ssh_host, ssh_port, ssh_user),
                status="BLOCKED",
                detail="Invalid since format",
            )
            return "[ERROR] 非法的 since 時間格式（僅允許安全字元集且限 40 字元以內）"
        parts.extend(["--since", shlex.quote(since_clean)])

    safe_lines = max(1, min(int(lines or 50), 200))
    parts.extend(["-n", str(safe_lines)])
    result = _run_remote(
        " ".join(parts),
        ssh_host,
        ssh_port,
        ssh_user,
        ssh_password,
        ssh_key_path,
        tool_name="rhel_query_journal",
    )
    if result.startswith("[ERROR]"):
        return "[ERROR] 日誌摘要查詢失敗；可能缺少權限或不支援指定的 journalctl 選項。"
    counts = {str(level): 0 for level in range(8)}
    try:
        for line in [] if result == "(沒有輸出)" else result.splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            level = record.get("PRIORITY")
            if not isinstance(level, str) or level not in counts:
                return "[ERROR] 日誌摘要格式不符，已捨棄。"
            counts[level] += 1
    except ValueError, AttributeError:
        return "[ERROR] 日誌摘要格式不符，已捨棄。"
    return json.dumps({"sample_limit": safe_lines, "sample_count": sum(counts.values()), "priorities": counts})


@mcp.tool()
def rhel_security_status(
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
) -> str:
    """探查 RHEL 安全性狀態：SELinux 模式與 firewalld 預設／作用中區域、執行期與永久規則摘要"""
    cmd = (
        "echo '=== SELinux Status ===' && (sestatus || getenforce) 2>/dev/null; "
        "echo '=== Recent SELinux AVC Denials ==='; "
        "echo '[提示] 不回傳 AVC 原文；請使用 rhel_query_journal 取得有界等級摘要'; "
        "echo '=== Firewalld State (Runtime) ===' && "
        "(firewall-cmd --state && firewall-cmd --get-default-zone && "
        "firewall-cmd --get-active-zones && firewall-cmd --list-all) 2>/dev/null "
        "|| echo 'firewalld 未執行或需要權限'; "
        "echo '=== Firewalld State (Permanent) ===' && "
        "(firewall-cmd --permanent --list-all 2>/dev/null || echo '無法讀取 permanent 規則'); "
        "echo '=== Security Updates (dnf) ===' && "
        "echo '[UNSUPPORTED] 安全更新查詢已停用（DNF 快取存取存在潛在副作用）'"
    )
    return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path, tool_name="rhel_security_status")


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
    """僅提供 /etc/selinux/config 的 SELINUX 列舉值；不提供任意設定檔原文。"""
    if file_path != "/etc/selinux/config":
        _audit_log(
            "rhel_read_config",
            f"file_path={file_path!r}",
            target=_format_audit_target(ssh_host, ssh_port, ssh_user),
            status="BLOCKED",
            detail="Only /etc/selinux/config is permitted",
        )
        return "[ERROR] 安全摘要模式僅支援 /etc/selinux/config 的 SELINUX 欄位，不讀取其他設定檔。"
    cmd = (
        _remote_path_guard(file_path, directory=False) + "awk -F= -- '/^[[:space:]]*SELINUX[[:space:]]*=/ {"
        'v=$2; gsub(/[[:space:]]/,"",v); '
        'if (v == "enforcing" || v == "permissive" || v == "disabled") print "SELINUX=" v'
        '}\' "$real"'
    )
    result = _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path, tool_name="rhel_read_config")
    allowed = {"SELINUX=enforcing", "SELINUX=permissive", "SELINUX=disabled"}
    if result not in allowed:
        return "[ERROR] 無法取得唯一且有效的 SELINUX 設定，已捨棄內容。"
    return result


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
    """目錄列舉暫停提供；保留介面，但不讀取或回傳檔名及符號連結目標。"""
    _audit_log(
        "rhel_list_directory",
        f"dir_path={dir_path!r}",
        target=_format_audit_target(ssh_host, ssh_port, ssh_user),
        status="BLOCKED",
        detail="Directory listing disabled in safe summary mode",
    )
    return "[ERROR] 安全摘要模式不提供目錄列舉；請使用特定的系統診斷工具。"


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
        _audit_log(
            "rhel_selinux_context",
            f"target_path={target_path!r}",
            target=_format_audit_target(ssh_host, ssh_port, ssh_user),
            status="BLOCKED",
            detail=str(e),
        )
        return _redact_and_truncate(f"[ERROR] {e}")

    quoted_path = shlex.quote(safe_path)
    cmd = (
        f"echo '=== Current SELinux Context (ls -dZ) ===' && ls -dZ -- {quoted_path} 2>&1; "
        "echo '=== Expected Context (matchpathcon) ===' && "
        f"(matchpathcon {quoted_path} 2>/dev/null || echo '無 matchpathcon 紀錄或工具未安裝')"
    )
    return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path, tool_name="rhel_selinux_context")


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
            _audit_log(
                "rhel_process_inspect",
                f"pid={pid!r}",
                target=_format_audit_target(ssh_host, ssh_port, ssh_user),
                status="BLOCKED",
                detail=str(e),
            )
            return _redact_and_truncate(f"[ERROR] {e}")
        cmd = (
            f"echo '=== Process Detail (PID: {safe_pid}) ===' && "
            f"ps -p {safe_pid} -o pid,ppid,user,stat,comm 2>/dev/null || echo '行程不存在或已終止'; "
            "echo '=== Process Status Summary ===' && "
            f"(cat /proc/{safe_pid}/status 2>/dev/null | "
            "grep -E '^(Name|State|Tgid|Pid|PPid|Uid|Gid|Threads|VmRSS|VmSize)' || true); "
            "echo '=== Process SELinux Label ===' && "
            f"(ps -q {safe_pid} -o pid,user,label,comm 2>/dev/null || echo '無 SELinux 標籤資訊')"
        )
        return _run_remote(
            cmd,
            ssh_host,
            ssh_port,
            ssh_user,
            ssh_password,
            ssh_key_path,
            tool_name="rhel_process_inspect",
        )

    cols = "pid,ppid,user,%cpu,%mem,stat,label,comm" if show_selinux else "pid,ppid,user,%cpu,%mem,stat,comm"

    if search.strip():
        try:
            safe_search = _validate_search_pattern(search, "search")
        except ValueError as e:
            _audit_log(
                "rhel_process_inspect",
                f"search={search!r}",
                target=_format_audit_target(ssh_host, ssh_port, ssh_user),
                status="BLOCKED",
                detail=str(e),
            )
            return _redact_and_truncate(f"[ERROR] {e}")
        quoted_search = shlex.quote(safe_search)
        cmd = (
            f"echo '=== Search Processes matching {quoted_search} ===' && "
            f"(ps -eo {cols} | grep -i -- {quoted_search} | grep -v grep | sed -n '1,{safe_lines}p' "
            "|| echo '查無符合條件的行程')"
        )
        return _run_remote(
            cmd,
            ssh_host,
            ssh_port,
            ssh_user,
            ssh_password,
            ssh_key_path,
            tool_name="rhel_process_inspect",
        )

    sort_clean = sort_by.strip().lower()
    if sort_clean not in _VALID_SORT_KEYS:
        _audit_log(
            "rhel_process_inspect",
            f"sort_by={sort_by!r}",
            target=_format_audit_target(ssh_host, ssh_port, ssh_user),
            status="BLOCKED",
            detail="Invalid sort_by parameter",
        )
        return "[ERROR] 非法的 sort_by 參數（僅允許 'cpu'、'mem' 或 'memory'）。"

    sort_col = "-%mem" if sort_clean in ("mem", "memory") else "-%cpu"
    cmd = (
        f"echo '=== Top Processes (sorted by {sort_clean}) ===' && "
        f"ps -eo {cols} --sort={sort_col} | sed -n '1,{safe_lines}p'"
    )
    return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path, tool_name="rhel_process_inspect")


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
        _audit_log(
            "rhel_network_connections",
            f"protocol={protocol!r}",
            target=_format_audit_target(ssh_host, ssh_port, ssh_user),
            status="BLOCKED",
            detail="Invalid protocol parameter",
        )
        return "[ERROR] 非法的 protocol 參數（僅允許 all、tcp、udp）。"
    proto_flag = "-t" if proto_clean == "tcp" else "-u" if proto_clean == "udp" else "-tu"
    proc_flag = " -p" if show_process else ""

    state_clean = state.strip().lower()
    valid_states = {"all", "listening", "established", "time-wait", "close-wait", "syn-sent", "syn-recv"}
    if state_clean not in valid_states:
        _audit_log(
            "rhel_network_connections",
            f"state={state!r}",
            target=_format_audit_target(ssh_host, ssh_port, ssh_user),
            status="BLOCKED",
            detail="Invalid state parameter",
        )
        return f"[ERROR] 非法的 state 參數（允許值：{sorted(valid_states)}）"

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
            _audit_log(
                "rhel_network_connections",
                f"port={port!r}",
                target=_format_audit_target(ssh_host, ssh_port, ssh_user),
                status="BLOCKED",
                detail=str(e),
            )
            return _redact_and_truncate(f"[ERROR] {e}")
        filter_expr = f" sport = :{safe_port} or dport = :{safe_port}"

    safe_lines = max(5, min(int(max_lines or 60), 150))
    cmd = f"{base_cmd}{filter_expr} | sed -n '1,{safe_lines}p'"
    return _run_remote(
        cmd,
        ssh_host,
        ssh_port,
        ssh_user,
        ssh_password,
        ssh_key_path,
        tool_name="rhel_network_connections",
    )


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
    """查詢已安裝之 RHEL RPM 套件資訊、Changelog 摘要與完整性驗證 (rpm -qi, rpm -V)"""
    try:
        safe_pkg = _validate_identifier(package_name, "package_name")
    except ValueError as e:
        _audit_log(
            "rhel_package_info",
            f"package_name={package_name!r}",
            target=_format_audit_target(ssh_host, ssh_port, ssh_user),
            status="BLOCKED",
            detail=str(e),
        )
        return _redact_and_truncate(f"[ERROR] {e}")

    quoted = shlex.quote(safe_pkg)
    if verify_integrity:
        cmd = (
            f"rpm -q {quoted} >/dev/null 2>&1 || "
            "{ echo '[ERROR] 套件未安裝或資料庫查詢失敗'; exit 1; }; "
            f"if rpm -V --noscripts {quoted} >/dev/null 2>&1; then rc=0; else rc=$?; fi; "
            "case $rc in 0) echo 'verification=unchanged' ;; "
            "1) echo 'verification=differences_or_verification_error' ;; "
            "*) echo '[ERROR] RPM 驗證失敗'; exit 1 ;; esac"
        )
    else:
        cmd = (
            f"echo '=== Package Info (rpm -qi) ===' && "
            f"(rpm -qi {quoted} 2>/dev/null || "
            "echo '[ERROR] 查無套件，不自動透過 DNF 查詢中繼資料'); "
            "echo '=== Changelog Summary (recent) ===' && "
            f"rpm -q --changelog {quoted} 2>/dev/null | sed -n '1,20p'"
        )
    return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path, tool_name="rhel_package_info")


@mcp.tool()
def rhel_container_status(
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
) -> str:
    """探查 RHEL 容器執行環境；在未證明無副作用前暫停提供。"""
    _audit_log(
        "rhel_container_status",
        "container_status",
        target=_format_audit_target(ssh_host, ssh_port, ssh_user),
        status="BLOCKED",
        detail="Container status disabled in safe summary mode",
    )
    return "[ERROR] [UNSUPPORTED] 安全摘要模式下暫停提供容器狀態探查（尚未證明命令無副作用）。"


@mcp.tool()
def rhel_cron_and_timers(
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
) -> str:
    """提供已載入 timer 與系統 crontab 項目計數，不回傳名稱、帳號或執行命令。"""
    cmd = (
        "echo '=== Loaded Timer Count ===' && "
        "systemctl list-units --type=timer --all --no-pager --no-legend "
        "| awk 'END {print \"loaded_timers=\" NR}'; "
        "echo '=== System Crontab Entry Count ===' && "
        "awk '/^[[:space:]]*($|#)/ {next} "
        "/^[[:space:]]*[A-Za-z_][A-Za-z_0-9]*[[:space:]]*=/ {next} "
        '{n++} END {print "system_crontab_entries=" n+0}\' /etc/crontab'
    )
    return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path, tool_name="rhel_cron_and_timers")


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
    return _run_remote(cmd, ssh_host, ssh_port, ssh_user, ssh_password, ssh_key_path, tool_name="rhel_hardware_info")


if __name__ == "__main__":
    mcp.run()
