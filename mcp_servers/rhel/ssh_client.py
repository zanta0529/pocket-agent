"""RHEL SSH 雙軌連線轉接器（Adapter Pattern）：憑證快取、Askpass、known_hosts 同步與 1 MB 安全串流擷取。"""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from mcp_servers._shared import CONSTANTS, AuditLogger, CredentialCache, tty_prompt
from mcp_servers.rhel import security

MAX_CAPTURE_BYTES = CONSTANTS.max_capture_bytes

CREDS_CACHE = CredentialCache(ttl_seconds=600.0)
AUDIT = AuditLogger("rhel-ops")


def clear_creds_cache(target_host: str = "") -> None:
    CREDS_CACHE.clear(target_host=target_host, host_env_key="RHEL_HOST")


def format_audit_target(host: str = "", port: str = "", user: str = "") -> str:
    h = host.strip() or CREDS_CACHE.get("RHEL_HOST") or (os.getenv("RHEL_HOST") or "").strip() or "?"
    p = port.strip() or CREDS_CACHE.get("RHEL_SSH_PORT") or (os.getenv("RHEL_SSH_PORT") or "").strip() or "22"
    u = user.strip() or CREDS_CACHE.get("RHEL_SSH_USER") or (os.getenv("RHEL_SSH_USER") or "").strip() or "?"
    return f"{u}@{h}:{p}"


def resolve_connection_params(
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
) -> tuple[str, str, str, str, str]:
    """依「注入參數 → 記憶體快取 → 環境變數 → 終端機互動詢問」順序取得連線五要素。"""
    with CREDS_CACHE.lock:
        cached_host = CREDS_CACHE.get("RHEL_HOST")
        cached_port = CREDS_CACHE.get("RHEL_SSH_PORT")
        cached_user = CREDS_CACHE.get("RHEL_SSH_USER")
        cached_key = CREDS_CACHE.get("RHEL_SSH_KEY_PATH")

        host = ssh_host.strip() or cached_host or (os.getenv("RHEL_HOST") or "").strip()
        port = ssh_port.strip() or cached_port or (os.getenv("RHEL_SSH_PORT") or "").strip()
        user = ssh_user.strip() or cached_user or (os.getenv("RHEL_SSH_USER") or "").strip()
        key_path = ssh_key_path.strip() or cached_key or (os.getenv("RHEL_SSH_KEY_PATH") or "").strip()

        if not host:
            host = tty_prompt("請輸入 RHEL 主機 IP 或位址 (RHEL_HOST): ").strip()
        if not port:
            port = tty_prompt("請輸入 SSH 連接埠 (RHEL_SSH_PORT) [預設 22]: ").strip() or "22"
        if not user:
            user = tty_prompt(f"請輸入 {host} 的 SSH 登入帳號 (RHEL_SSH_USER): ").strip()

        if not host:
            raise ValueError("未提供 RHEL 主機位址 (RHEL_HOST)。")
        if not user:
            raise ValueError("未提供 SSH 登入帳號 (RHEL_SSH_USER)。")

        if key_path:
            resolved_key = Path(key_path).expanduser().resolve()
            if not resolved_key.is_file():
                raise ValueError(f"指定的 SSH 私鑰檔案不存在或無法存取：{key_path!r}")
            key_path = str(resolved_key)

        host = security.validate_host(host)
        user = security.validate_identifier(user, "RHEL_SSH_USER")
        port = str(int(security.validate_port(port)))
        target = (host, port, user, key_path)
        cached_target = (cached_host, cached_port, cached_user, cached_key)

        env_key = (os.getenv("RHEL_SSH_KEY_PATH") or "").strip()
        if env_key:
            env_key = str(Path(env_key).expanduser().resolve())
        raw_env_host = (os.getenv("RHEL_HOST") or "").strip()
        try:
            norm_env_host = security.validate_host(raw_env_host) if raw_env_host else ""
        except ValueError:
            norm_env_host = raw_env_host
        env_target = (
            norm_env_host,
            (os.getenv("RHEL_SSH_PORT") or "22").strip(),
            (os.getenv("RHEL_SSH_USER") or "").strip(),
            env_key,
        )

        password = ssh_password
        if not password:
            password = CREDS_CACHE.get_target_secret(target)
        if not password and target == cached_target:
            password = CREDS_CACHE.get("RHEL_SSH_PASSWORD")
        if not password and target == env_target:
            env_name = "RHEL_SSH_KEY_PASSPHRASE" if key_path else "RHEL_SSH_PASSWORD"
            password = os.getenv(env_name) or ""

        if not key_path:
            if not password:
                password = tty_prompt(f"請輸入 {user}@{host} 的 SSH 登入密碼: ", is_password=True)
            if not password:
                raise ValueError("未提供 SSH 登入密碼 (RHEL_SSH_PASSWORD)；未指定私鑰時必須提供密碼驗證。")

        CREDS_CACHE.update(
            {
                "RHEL_HOST": host,
                "RHEL_SSH_PORT": port,
                "RHEL_SSH_USER": user,
                "RHEL_SSH_PASSWORD": password,
                "RHEL_SSH_KEY_PATH": key_path,
            }
        )
        CREDS_CACHE.set_target_secret(target, password)
        return host, port, user, password, key_path


def build_ssh_args(
    remote_cmd: str,
    ssh_host: str = "",
    ssh_port: str = "",
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_key_path: str = "",
) -> tuple[list[str], str, int]:
    host, port, user, password, key_path = resolve_connection_params(
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
                        with open(known_hosts_file, "a", encoding="utf-8") as fh:
                            if prefix_nl:
                                fh.write(prefix_nl)
                            for entry in user_lines:
                                fh.write(f"{entry}\n")
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

    args.extend(["--", f"{user}@{host}", remote_cmd])
    return args, password, timeout


def capture_ssh(
    ssh_args: list[str],
    *,
    timeout: float,
    creationflags: int,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    """非阻塞輪詢雙管線；共同期限涵蓋主行程及 EOF，超過 1 MB 立即強制終止並捨棄內容。"""
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


def run_with_askpass(
    ssh_args: list[str],
    password: str,
    timeout: int,
    creationflags: int,
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory(prefix="rhel_mcp_") as tmp_dir:
        tmp_path = Path(tmp_dir)
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

        return capture_ssh(ssh_args, timeout=timeout + 15, creationflags=creationflags, env=env)


def is_transport_or_auth_error(msg: str) -> bool:
    """辨識連線、認證與逾時等傳輸層錯誤，避免被工具層摘要錯誤覆寫。"""
    if not msg.startswith("[ERROR]"):
        return False
    return any(marker in msg for marker in ("[CONN_AUTH]", "[HOST_KEY_MISMATCH]", "執行逾時"))


def run_remote(
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
        ssh_args, password, timeout = build_ssh_args(
            security.diagnostic_command(remote_cmd),
            ssh_host,
            ssh_port,
            ssh_user,
            ssh_password,
            ssh_key_path,
        )
        target_str = format_audit_target(ssh_host, ssh_port, ssh_user)
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        proc = run_with_askpass(ssh_args, password, timeout, creationflags)

        if proc.returncode == 255:
            stderr_lower = (proc.stderr or "").lower()
            if "host key verification failed" in stderr_lower or "known_hosts" in stderr_lower:
                AUDIT.log(
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
                AUDIT.log(
                    tool_name,
                    remote_cmd,
                    target=target_str,
                    status="AUTH_FAIL",
                    detail="Permission denied (exit 255)",
                )
                clear_creds_cache(ssh_host)
                return (
                    "[ERROR] [CONN_AUTH] 遠端 SSH 認證失敗 (Permission denied, exit 255)\n"
                    "提示：請確認帳號與密碼/金鑰是否正確，且目標主機已啟用對應認證方式。"
                )

            AUDIT.log(
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
                AUDIT.log(tool_name, remote_cmd, target=target_str, status="ERROR", detail=stderr_msg)
                return f"[ERROR] {stderr_msg}"
            AUDIT.log(tool_name, remote_cmd, target=target_str, status="ERROR", detail=f"exit {proc.returncode}")
            return f"[ERROR] 遠端執行失敗 (exit {proc.returncode})"

        if proc.returncode != 0:
            AUDIT.log(
                tool_name,
                remote_cmd,
                target=target_str,
                status="PARTIAL_ERROR",
                detail=f"exit {proc.returncode}",
            )
            combined = f"[ERROR] 遠端指令部分或全部失敗 (exit {proc.returncode})\n{combined}"
        else:
            AUDIT.log(tool_name, remote_cmd, target=target_str, status="OK")
        return security.redact_and_truncate(combined)
    except ValueError as exc:
        AUDIT.log(
            tool_name,
            remote_cmd,
            target=format_audit_target(ssh_host, ssh_port, ssh_user),
            status="AUTH_FAIL",
            detail=str(exc),
        )
        clear_creds_cache(ssh_host)
        return "[ERROR] [CONN_AUTH] 連線參數或安全檢查失敗，請確認設定。"
    except subprocess.TimeoutExpired:
        AUDIT.log(
            tool_name,
            remote_cmd,
            target=format_audit_target(ssh_host, ssh_port, ssh_user),
            status="TIMEOUT",
            detail="SSH command execution timed out",
        )
        return "[ERROR] SSH 遠端指令執行逾時，已中止連線。"
    except Exception:
        AUDIT.log(
            tool_name,
            remote_cmd,
            target=format_audit_target(ssh_host, ssh_port, ssh_user),
            status="ERROR",
            detail="SSH execution exception",
        )
        return "[ERROR] SSH 執行或安全擷取失敗，未回傳原始例外內容。"
