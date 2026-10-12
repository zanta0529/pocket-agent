"""CLI 執行工具：讓模型能在本機執行指令（附使用者確認機制）。

安全設計：
- 預設每次執行前透過 confirm callback 詢問使用者
- 逾時強制終止（預設 60 秒）
- 輸出截斷，避免灌爆 context
"""

import locale
import logging
import os
import re
import signal
import subprocess
import sys
import threading

log = logging.getLogger("pocket-agent")

MAX_OUTPUT = 8000  # 回傳給模型的最大字元數
MAX_OUTPUT_BYTES = MAX_OUTPUT * 4  # 讀取子行程輸出的位元組上限（UTF-8 最多 4 位元組／字元）


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


def _kill_proc_tree(proc: subprocess.Popen) -> None:
    """終止子行程及其整個行程樹。"""
    if sys.platform == "win32":
        subprocess.call(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            proc.terminate()


def call_shell_tool(name: str, args: dict, confirm=None) -> str:
    """執行指令並回傳結果字串。confirm(command) 回傳 False 時拒絕執行。

    這是阻塞呼叫，core 端請用 asyncio.to_thread 包起來跑。
    """
    command = str(args.get("command", "")).strip()
    if not command:
        return "[ERROR] 沒有提供指令。"

    # --- Hard Command Floor (災難指令安全底線) ---
    # 阻擋 rm -rf /, del /s /q (根目錄或全磁碟), rd /s, format, mkfs 等毀滅性關鍵字
    # 注意：此為最後防線，容易被混淆繞過。真實防禦仍需依賴 Sandbox 或確認機制。
    danger_patterns = [
        r"rm\s+-[A-Za-z]*r[A-Za-z]*[fFv]*\s+(?:/[A-Za-z0-9_]*|~|\$HOME|\*|\s*$)",  # 更廣泛的 rm -rf 攔截
        r"del\s+(/[sqf]\s*)+\s*(?:[A-Za-z]:\\|\*)",  # 涵蓋 del /s /q *
        r"\b(?:rd|rmdir)\s+(?:/[sq]\s*)+\s*[A-Za-z]:\\?(?:\s|$)",  # rd /s /q C:\
        r"format\s+[A-Za-z]:",  # format C:
        r"mkfs",  # mkfs.*
    ]
    for pattern in danger_patterns:
        if re.search(pattern, command, re.IGNORECASE):
            log.warning(f"SECURITY BLOCK: 嘗試執行毀滅性指令被系統底線攔截 -> {command}")
            return f"[ERROR] 觸發安全底線限制，拒絕執行該高風險指令：{command}"
    # ----------------------------------------------

    cwd = args.get("cwd") or None
    if cwd and not os.path.isdir(cwd):
        return f"[ERROR] 工作目錄不存在或不是目錄：{cwd}"

    if confirm is not None:
        try:
            allowed = confirm(command)
        except Exception:
            log.exception("CLI confirm callback 失敗")
            allowed = False
    else:
        allowed = False

    if not allowed:
        return "[使用者拒絕執行此指令，請改用其他方式或詢問使用者。]"

    try:
        timeout = min(int(args.get("timeout_sec") or 60), 600)
    except (
        TypeError,
        ValueError,
    ):
        timeout = 60

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
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = creationflags
        else:
            kwargs["start_new_session"] = True

        proc = subprocess.Popen(cmd_args, **kwargs)

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
                text += "\n…（輸出過長，已提早截斷）"
            return text.strip()

        try:
            proc.wait(timeout=timeout)
            t.join(timeout=1.0)
        except subprocess.TimeoutExpired:
            _kill_proc_tree(proc)
            t.join(timeout=1.0)
            partial = collect_output()
            message = f"[ERROR] 指令逾時（{timeout} 秒），已強制終止。"
            if partial:
                message += f"\n已收集的輸出：\n{partial}"
            return message
        finally:
            # 讀取執行緒仍在阻塞讀取時關閉管道可能卡住，故僅在其結束後才關閉，避免檔案描述元外洩
            if not t.is_alive():
                try:
                    proc.stdout.close()
                except Exception:
                    pass
    except Exception as e:
        return f"[ERROR] {e}"

    output = collect_output() or "(沒有輸出)"

    return f"exit code: {proc.returncode}\n{output}"
