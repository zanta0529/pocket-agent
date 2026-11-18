"""CLI 執行工具：讓模型能在本機執行指令（附使用者確認機制）。

安全設計：
- 預設每次執行前透過 confirm callback 詢問使用者
- 逾時強制終止（預設 60 秒）
- 輸出截斷，避免灌爆 context
"""

import locale
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from pocket_agent.system.constants import (
    DEFAULT_CLI_TIMEOUT_SEC,
    DEFAULT_PROCESS_BUFFER_BYTES,
    DEFAULT_TOOL_OUTPUT_CHARS,
    MAX_CLI_TIMEOUT_SEC,
    SENSITIVE_ENV_EXACT_KEYS,
    SENSITIVE_ENV_PREFIXES,
)
from pocket_agent.system.permissions import CommandVerdict, classify_command_safety

log = logging.getLogger("pocket-agent")

# 向後相容別名 (對齊 constants.py)
MAX_OUTPUT = DEFAULT_TOOL_OUTPUT_CHARS  # 回傳給模型的最大字元數 (適合 64k~128k 視窗)
MAX_OUTPUT_BYTES = DEFAULT_PROCESS_BUFFER_BYTES  # 讀取子行程輸出的位元組上限 (1 MB 安全緩衝區)


def _decode_candidates() -> list[str]:
    """子行程輸出的候選編碼，依序嘗試。

    Windows 上沒有單一正確答案：`type` 一個 UTF-8 檔案吐的是 UTF-8，
    但 dir、ping 這些內建指令吐的是主控台的 OEM 碼頁（正體中文為
    cp950）。先試 UTF-8 是因為誤判風險低——中文的 cp950 位元組序列
    多半不是合法的 UTF-8，會解碼失敗而落到下一個候選。
    """
    candidates = ["utf-8-sig"]  # 同時吃掉 BOM 與無 BOM 的 UTF-8

    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            candidates.append(f"cp{kernel32.GetOEMCP()}")  # 主控台
            candidates.append(f"cp{kernel32.GetACP()}")  # 系統 ANSI
        except Exception:
            pass

    candidates.append(locale.getpreferredencoding(False))

    seen, ordered = set(), []
    for enc in candidates:
        key = (enc or "").lower()
        if key and key not in seen:
            seen.add(key)
            ordered.append(enc)
    return ordered


def _decode(raw: bytes) -> str:
    """把子行程輸出解成文字；全部失敗才用替代字元硬解。"""
    if not raw:
        return ""
    for enc in _decode_candidates():
        try:
            return raw.decode(enc)
        except (
            UnicodeDecodeError,
            LookupError,
        ):
            continue
    return raw.decode("utf-8", errors="replace")


def _trim_partial_utf8(raw: bytes) -> bytes:
    """截斷輸出時，移除結尾被切斷的 UTF-8 多位元組字元，避免整段被誤判為其他編碼。"""
    for cut in range(4):
        candidate = raw[: len(raw) - cut]
        try:
            candidate.decode("utf-8")
            return candidate
        except UnicodeDecodeError:
            continue
    return raw


SHELL_TOOLS = [
    {
        "type": "function",
        "name": "run_command",
        "description": (
            "在使用者的電腦上執行 CLI 指令並回傳輸出。"
            "適合執行腳本、git 操作、編譯建置或安裝套件等。"
            "【強烈警告】嚴禁使用此工具進行檔案讀寫或目錄搜尋（如 cat、ls、grep、sed、find 等），"
            "若需操作檔案請務必改用原生的 fs 檔案工具。"
            "注意：Windows 環境用 cmd.exe 執行（此電腦的群組原則封鎖 PowerShell，請勿使用 PowerShell 語法）。"
            "危險或不可逆的操作（刪除、覆蓋、對外發送）請先向使用者說明再執行。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要執行的完整指令",
                },
                "cwd": {
                    "type": "string",
                    "description": "工作目錄（省略時用程式目前目錄）",
                },
                "timeout_sec": {
                    "type": "integer",
                    "description": "逾時秒數（預設 60，最多 600）",
                },
            },
            "required": ["command"],
        },
    },
]


def is_shell_tool(name: str) -> bool:
    return name == "run_command"


def scrub_subprocess_env(
    base_env: dict[str, str] | None = None,
    *,
    extra_allowlist: list[str] | tuple[str, ...] | None = None,
) -> dict[str, str]:
    """複製環境變數並剝除主程序之敏感 API Key／憑證，避免子行程無意繼承洩漏。"""
    env = dict(base_env if base_env is not None else os.environ)
    allow_set: set[str] = {str(k).strip() for k in (extra_allowlist or ()) if str(k).strip()}
    try:
        from pocket_agent.system.config import config

        cfg_allow = config.get("system.subprocess_env_allowlist", [])
        if isinstance(cfg_allow, (list, tuple)):
            allow_set.update(str(k).strip() for k in cfg_allow if str(k).strip())
    except Exception:
        pass

    for key in list(env.keys()):
        if key in allow_set:
            continue
        if key in SENSITIVE_ENV_EXACT_KEYS or key.startswith(SENSITIVE_ENV_PREFIXES):
            env.pop(key, None)
    return env


@dataclass(frozen=True)
class ShellTerminationStatus:
    """子行程終止與程序樹清理結果契約。"""

    exited: bool
    tree_cleanup_confirmed: bool
    output_complete: bool
    backend: str
    detail: str = ""


class _ProcessContainmentStrategy(ABC):
    """子行程生命週期圍堵與樹狀終止策略介面。"""

    @abstractmethod
    def attach(self, proc: subprocess.Popen) -> None:
        """將已啟動的子行程綁定至圍堵機制。"""

    @abstractmethod
    def terminate(
        self,
        proc: subprocess.Popen,
        timeout: float = 0.5,
        *,
        output_complete: bool = False,
    ) -> ShellTerminationStatus:
        """終止子行程與其後代，並回傳可驗證的清理狀態。"""

    def cleanup(self) -> None:
        """釋放策略持有的作業系統資源控制代碼。"""


class _FallbackTreeContainment(_ProcessContainmentStrategy):
    """預設跨平台行程樹終止策略（Windows taskkill / POSIX process group）。"""

    def attach(self, proc: subprocess.Popen) -> None:
        return None

    def terminate(
        self,
        proc: subprocess.Popen,
        timeout: float = 0.5,
        *,
        output_complete: bool = False,
    ) -> ShellTerminationStatus:
        if sys.platform == "win32":
            try:
                res = subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=timeout,
                )
                exited = proc.poll() is not None
                if res.returncode == 0 and exited:
                    return ShellTerminationStatus(
                        exited=True,
                        tree_cleanup_confirmed=True,
                        output_complete=output_complete,
                        backend="taskkill",
                    )
                if not exited:
                    proc.kill()
                    exited = proc.poll() is not None
                return ShellTerminationStatus(
                    exited=exited,
                    tree_cleanup_confirmed=False,
                    output_complete=output_complete,
                    backend="taskkill_fallback",
                    detail=f"taskkill rc={res.returncode}",
                )
            except Exception as e:
                try:
                    proc.kill()
                except Exception:
                    pass
                return ShellTerminationStatus(
                    exited=proc.poll() is not None,
                    tree_cleanup_confirmed=False,
                    output_complete=output_complete,
                    backend="proc_kill_fallback",
                    detail=str(e),
                )

        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
            t0 = time.monotonic()
            while time.monotonic() - t0 < timeout:
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
            if proc.poll() is None:
                os.killpg(pgid, signal.SIGKILL)
            exited = proc.poll() is not None
            return ShellTerminationStatus(
                exited=exited,
                tree_cleanup_confirmed=exited,
                output_complete=output_complete,
                backend="posix_pgid",
            )
        except ProcessLookupError:
            return ShellTerminationStatus(
                exited=True,
                tree_cleanup_confirmed=True,
                output_complete=output_complete,
                backend="posix_pgid",
            )
        except Exception as e:
            try:
                proc.kill()
            except Exception:
                pass
            return ShellTerminationStatus(
                exited=proc.poll() is not None,
                tree_cleanup_confirmed=False,
                output_complete=output_complete,
                backend="proc_kill_fallback",
                detail=str(e),
            )


class _WindowsJobContainment(_ProcessContainmentStrategy):
    """Windows Job Object 行程生命週期管理（僅負責行程樹清理，非檔案或網路沙盒）。"""

    def __init__(self) -> None:
        self._job_handle = None
        self._attached = False
        self._fallback = _FallbackTreeContainment()

    def attach(self, proc: subprocess.Popen) -> None:
        raw_handle = getattr(proc, "_handle", None)
        if not isinstance(raw_handle, int) or raw_handle <= 0:
            return
        try:
            import win32job

            job = win32job.CreateJobObject(None, "")
            info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
            info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
            win32job.AssignProcessToJobObject(job, raw_handle)
            self._job_handle = job
            self._attached = True
        except Exception as e:
            log.debug("Windows Job Object 綁定失敗，將降級至 taskkill：%s", e)
            self.cleanup()

    def terminate(
        self,
        proc: subprocess.Popen,
        timeout: float = 0.5,
        *,
        output_complete: bool = False,
    ) -> ShellTerminationStatus:
        if not self._attached or self._job_handle is None:
            return self._fallback.terminate(proc, timeout=timeout, output_complete=output_complete)
        try:
            import win32job

            win32job.TerminateJobObject(self._job_handle, 1)
            t0 = time.monotonic()
            while time.monotonic() - t0 < timeout:
                if proc.poll() is not None:
                    break
                time.sleep(0.02)
            exited = proc.poll() is not None
            return ShellTerminationStatus(
                exited=exited,
                tree_cleanup_confirmed=exited,
                output_complete=output_complete,
                backend="win32_job",
            )
        except Exception as e:
            fb = self._fallback.terminate(proc, timeout=timeout, output_complete=output_complete)
            return ShellTerminationStatus(
                exited=fb.exited,
                tree_cleanup_confirmed=fb.tree_cleanup_confirmed,
                output_complete=output_complete,
                backend=f"win32_job->{fb.backend}",
                detail=str(e),
            )
        finally:
            self.cleanup()

    def cleanup(self) -> None:
        if self._job_handle is not None:
            try:
                import win32api

                win32api.CloseHandle(self._job_handle)
            except Exception:
                pass
            self._job_handle = None
            self._attached = False


def _create_containment_strategy() -> _ProcessContainmentStrategy:
    """依作業系統與 pywin32 可用性建立行程圍堵策略（Factory）。"""
    if sys.platform == "win32":
        try:
            import win32job  # noqa: F401

            return _WindowsJobContainment()
        except ImportError:
            pass
    return _FallbackTreeContainment()


_active_shell_procs: set[subprocess.Popen] = set()
_active_proc_strategies: dict[subprocess.Popen, _ProcessContainmentStrategy] = {}
_shell_proc_lock = threading.Lock()
_shell_cancel_generation: int = 0


def _kill_proc_tree(
    proc: subprocess.Popen,
    timeout: float = 0.5,
    *,
    output_complete: bool = False,
) -> ShellTerminationStatus:
    """終止子行程及其整個行程樹，回傳結構化清理狀態契約。"""
    with _shell_proc_lock:
        strategy = _active_proc_strategies.get(proc)
    if strategy is None:
        strategy = _FallbackTreeContainment()
    return strategy.terminate(proc, timeout=timeout, output_complete=output_complete)


def interrupt_active_shell_processes_with_status() -> tuple[int, list[ShellTerminationStatus]]:
    """強制終止所有目前執行中的 Shell 子行程樹，並回傳各行程的清理狀態契約。"""
    global _shell_cancel_generation
    statuses: list[ShellTerminationStatus] = []
    with _shell_proc_lock:
        _shell_cancel_generation += 1
        items = [(p, _active_proc_strategies.get(p) or _FallbackTreeContainment()) for p in _active_shell_procs]

    for proc, strategy in items:
        try:
            statuses.append(strategy.terminate(proc, output_complete=False))
        except Exception as e:
            statuses.append(
                ShellTerminationStatus(
                    exited=proc.poll() is not None,
                    tree_cleanup_confirmed=False,
                    output_complete=False,
                    backend="error",
                    detail=str(e),
                )
            )

    time.sleep(0.05)
    with _shell_proc_lock:
        for p in list(_active_shell_procs):
            if p.poll() is not None:
                _active_shell_procs.discard(p)
                strat = _active_proc_strategies.pop(p, None)
                if strat is not None:
                    strat.cleanup()

    if statuses:
        log.warning("已嘗試終止 %d 個進行中的 Shell 子行程樹", len(statuses))
    return len(statuses), statuses


def interrupt_active_shell_processes() -> int:
    """強制終止所有目前執行中的 Shell 子進程樹，防止取消後背景繼續執行。"""
    count, _ = interrupt_active_shell_processes_with_status()
    return count


def call_shell_tool(name: str, args: dict, confirm=None) -> str:
    """執行指令並回傳結果字串。confirm(command) 回傳 False 時拒絕執行。

    這是阻塞呼叫，core 端請用 asyncio.to_thread 包起來跑。
    """
    command = str(args.get("command", "")).strip()
    if not command:
        return "[ERROR] 沒有提供指令。"

    safety = classify_command_safety(command)
    if safety.verdict == CommandVerdict.BLOCKED:
        log.warning("SECURITY BLOCK: 嘗試執行毀滅性指令被系統底線攔截 (%s) -> %s", safety.reason, command)
        return f"[ERROR] 觸發安全底線限制（{safety.reason}），拒絕執行該高風險指令：{command}"

    cwd = args.get("cwd") or None
    if cwd and not os.path.isdir(cwd):
        return f"[ERROR] 工作目錄不存在或不是目錄：{cwd}"

    confirm_prompt = command
    if safety.verdict == CommandVerdict.CAUTION:
        from pocket_agent.system.i18n import t

        localized_reason = t(safety.reason_key) if safety.reason_key else safety.reason
        confirm_prompt = t("confirmation.git_caution_banner", reason=localized_reason, command=command)

    start_generation = _shell_cancel_generation
    if confirm is not None:
        try:
            allowed = confirm(confirm_prompt)
        except Exception:
            log.exception("CLI confirm callback 失敗")
            allowed = False
    else:
        allowed = False

    if _shell_cancel_generation != start_generation:
        return "[操作已由使用者取消中斷，指令未建立與執行]"

    if allowed is True:
        pass
    elif isinstance(allowed, str) and allowed.strip():
        return (
            f"[使用者拒絕執行此指令，並提供後續指示：\n"
            f"「{allowed.strip()}」\n"
            "請立即並嚴格遵循使用者的上述指示繼續處理任務，切勿再次嘗試執行違背該指示的指令。]"
        )
    elif isinstance(allowed, tuple) and len(allowed) == 2 and not allowed[0]:
        custom_inst = str(allowed[1]).strip()
        if custom_inst:
            return (
                f"[使用者拒絕執行此指令，並提供後續指示：\n"
                f"「{custom_inst}」\n"
                "請立即並嚴格遵循使用者的上述指示繼續處理任務，切勿再次嘗試執行違背該指示的指令。]"
            )
        return "[使用者拒絕執行此指令，請改用其他方式或詢問使用者。]"
    else:
        return "[使用者拒絕執行此指令，請改用其他方式或詢問使用者。]"

    try:
        timeout = min(
            int(args.get("timeout_sec") or DEFAULT_CLI_TIMEOUT_SEC),
            MAX_CLI_TIMEOUT_SEC,
        )
    except (
        TypeError,
        ValueError,
    ):
        timeout = DEFAULT_CLI_TIMEOUT_SEC

    # GUI 程式底下跑 console 指令不要閃出黑視窗
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

    log.debug("CLI run: %s", command)

    from pocket_agent.system.config import config  # 依照系統設定決定 sandbox 模式

    sandbox_mode = config.get("system.use_sandbox", "none").lower()

    cmd_args = command
    is_shell = True

    if sandbox_mode == "wsl":
        cmd_args = ["wsl", "--", "sh", "-c", command]
        is_shell = False
        log.debug("Using WSL Sandbox")
    elif sandbox_mode == "docker":
        container_name = config.get("system.sandbox_container", "pocket_sandbox")
        cmd_args = ["docker", "exec", container_name, "sh", "-c", command]
        is_shell = False
        log.debug("Using Docker Sandbox")

    try:
        kwargs = {
            "shell": is_shell,
            "cwd": cwd,
            "env": scrub_subprocess_env(),
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = creationflags
        else:
            kwargs["start_new_session"] = True

        with _shell_proc_lock:
            if _shell_cancel_generation != start_generation:
                return "[操作已由使用者取消中斷，指令未建立與執行]"
            proc = subprocess.Popen(cmd_args, **kwargs)
            strategy = _create_containment_strategy()
            strategy.attach(proc)
            _active_shell_procs.add(proc)
            _active_proc_strategies[proc] = strategy

        # 累積原始位元組，結束後一次解碼，避免多位元組字元被分段切斷而誤判編碼
        output_buf = bytearray()
        truncated = [False]

        def reader_thread():
            while True:
                try:
                    buf = proc.stdout.read(1024)
                except ValueError, OSError:
                    break  # stdout 已被關閉
                if not buf:
                    break
                output_buf.extend(buf)
                if len(output_buf) > MAX_OUTPUT_BYTES:
                    truncated[0] = True
                    _kill_proc_tree(proc)
                    break

        t = threading.Thread(target=reader_thread, daemon=True)
        t.start()

        def collect_output() -> str:
            raw = bytes(output_buf[:MAX_OUTPUT_BYTES]) if truncated[0] else bytes(output_buf)
            if truncated[0]:
                raw = _trim_partial_utf8(raw)
            text = _decode(raw)
            if len(text) > MAX_OUTPUT:
                text = text[:MAX_OUTPUT]
                truncated[0] = True
            if truncated[0]:
                text += (
                    f"\n…（輸出過長，已提早截斷至前 {MAX_OUTPUT} 字元）\n"
                    "[系統提示：若需檢視後續內容或特定錯誤，請透過 grep、head/tail 或加上更精確的過濾指令再次執行]"
                )
            return text.strip()

        try:
            proc.wait(timeout=timeout)
            t.join(timeout=1.0)
        except subprocess.TimeoutExpired:
            term_status = _kill_proc_tree(proc, output_complete=False)
            try:
                proc.stdout.close()
            except Exception:
                pass
            t.join(timeout=0.5)
            partial = collect_output()
            if term_status.exited and term_status.tree_cleanup_confirmed:
                message = f"[ERROR] 指令逾時（{timeout} 秒），已強制終止（輸出可能不完整）。"
            elif term_status.exited:
                message = (
                    f"[ERROR] 指令逾時（{timeout} 秒），主程序已退出但程序樹清理未確認"
                    f"（後端：{term_status.backend}，輸出可能不完整）。"
                )
            else:
                message = (
                    f"[ERROR] 指令逾時（{timeout} 秒），已請求終止但尚未確認程序退出"
                    f"（後端：{term_status.backend}，輸出可能不完整）。"
                )
            if partial:
                message += f"\n已收集的輸出：\n{partial}"
            return message
        finally:
            with _shell_proc_lock:
                _active_shell_procs.discard(proc)
                strat = _active_proc_strategies.pop(proc, None)
            if strat is not None:
                strat.cleanup()
            try:
                proc.stdout.close()
            except Exception:
                pass
            t.join(timeout=0.2)
    except Exception as e:
        return f"[ERROR] {e}"

    output = collect_output() or "(沒有輸出)"

    return f"exit code: {proc.returncode}\n{output}"
