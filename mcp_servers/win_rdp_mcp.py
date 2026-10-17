"""遠端 Windows 主機 RDP（Port 3389）視覺控制與結構化唯讀巡檢 MCP 伺服器。

安全設計原則：
1. 僅允許帳號＋密碼驗證（於 `.rdp` 強制停用智慧卡、WebAuthn、本機磁碟掛載；
   Session 憑證於啟動握手後立即從記憶體清除，不殘留免密後門）。
2. 支援彈性設定：WIN_HOST、WIN_RDP_PORT、WIN_RDP_USER、WIN_RDP_PASSWORD 皆可於設定檔預填、留空或省略；
   未設定項目於執行期動態詢問使用者並僅快取於本機記憶體，絕不暴露予 LLM 或寫入日誌。
3. 雙層混合架構（純 Port 3389）：
   - Layer A：遠端桌面 GUI 視窗截圖、座標沙盒限制之滑鼠操作、前景驗證之鍵盤輸入。
   - Layer B：透過 RDP 剪貼簿虛擬通道（`Win+R` 執行 `cmd.exe /c "<唯讀指令> 2>&1 | clip"`）取回無損結構化巡檢文字。
4. 遠端設定檔讀取受白名單目錄、敏感關鍵字黑名單保護，並自動遮罩機密欄位。
"""

import ctypes
import getpass
import ntpath
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from ctypes import wintypes
from pathlib import Path
from typing import Any

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer

mcp = MCPServer("win-rdp-ops")

MAX_OUTPUT_CHARS = 6000
_HOST_RE = re.compile(r"^[a-zA-Z0-9_.\-]+$")
_USER_RE = re.compile(r"^[a-zA-Z0-9_.@\\\-]+$")
_SERVICE_RE = re.compile(r"^[a-zA-Z0-9_.\-]+$")

# 執行期記憶體快取（僅存活於當前行程生命週期，不落盤）
_RUNTIME_CREDS: dict[str, str] = {}
_ACTIVE_SESSION: dict[str, Any] = {
    "process": None,
    "hwnd": 0,
    "target": "",
}

_VALID_EVENT_LOGS = {
    "system": "System",
    "application": "Application",
    "security": "Security",
    "setup": "Setup",
}

_VALID_EVENT_LEVELS = {
    "critical": "1",
    "error": "2",
    "warning": "3",
    "info": "4",
    "information": "4",
    "1": "1",
    "2": "2",
    "3": "3",
    "4": "4",
}

_ALLOWED_WIN_CONFIG_PREFIXES = (
    "c:\\windows\\system32\\drivers\\etc\\",
    "c:\\windows\\win.ini",
    "c:\\windows\\system.ini",
    "c:\\inetpub\\wwwroot\\",
    "c:\\programdata\\",
)

_DENIED_WIN_PATH_KEYWORDS = (
    "sam",
    "security",
    "system32\\config",
    "ntds.dit",
    "private",
    "secret",
    "key",
    "token",
    "password",
    "passwd",
    ".ssh",
    ".env",
    ".pfx",
    ".p12",
    ".pem",
    "id_rsa",
    "id_ed25519",
    "unattend.xml",
    "web.config",
)

_REDACT_KV_RE = re.compile(
    r"(?i)(\b(?:password|passwd|secret|token|api_key|apikey|client_secret|passphrase|connectionstring)\b"
    r"\s*[:=]\s*)([^\s#;\"']+|\"[^\"]*\"|'[^']*')"
)
_REDACT_PEM_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----")

_BLOCKED_HOTKEYS = {
    "ctrl+alt+del",
    "ctrl+alt+delete",
    "alt+tab",
    "win+l",
}


def _validate_host(value: str) -> str:
    clean = value.strip()
    if not clean or clean.startswith("-") or not _HOST_RE.match(clean):
        raise ValueError(f"非法的 WIN_HOST 參數格式：{value!r}（僅允許英數字、點、底線與連字號）")
    return clean


def _validate_user(value: str) -> str:
    clean = value.strip()
    if not clean or clean.startswith("-") or not _USER_RE.match(clean):
        raise ValueError(f"非法的 WIN_RDP_USER 參數格式：{value!r}（僅允許英數字、點、底線、連字號、@ 與 \\）")
    return clean


def _validate_service_name(value: str) -> str:
    clean = value.strip()
    if not clean or clean.startswith("-") or not _SERVICE_RE.match(clean):
        raise ValueError(f"非法的 service_name 參數格式：{value!r}（僅允許英數字、點、底線與連字號）")
    return clean


def _validate_win_config_path(file_path: str) -> str:
    raw = file_path.strip().replace("/", "\\")
    if not re.match(r"^[a-zA-Z]:\\", raw):
        raise ValueError("Windows 設定檔路徑必須為完整磁碟機絕對路徑（例如 C:\\Windows\\win.ini）。")
    if ".." in raw.split("\\"):
        raise ValueError("拒絕存取：路徑不得包含 '..' 跳躍符號。")

    norm = ntpath.normpath(raw)
    lower_path = norm.lower()
    is_allowed = any(
        lower_path.startswith(prefix) or lower_path == prefix.rstrip("\\") for prefix in _ALLOWED_WIN_CONFIG_PREFIXES
    )
    if not is_allowed:
        allowed_str = "、".join(_ALLOWED_WIN_CONFIG_PREFIXES)
        raise ValueError(f"拒絕存取：{norm} 不在允許的唯讀探查目錄白名單內（{allowed_str}）。")

    for kw in _DENIED_WIN_PATH_KEYWORDS:
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
    rdp_host: str = "",
    rdp_port: str = "",
    rdp_user: str = "",
    rdp_password: str = "",
) -> tuple[str, str, str, str]:
    """依「注入參數 → 記憶體快取 → 環境變數 → 終端機互動詢問」順序取得 RDP 連線四要素。"""
    host = (rdp_host or _RUNTIME_CREDS.get("WIN_HOST") or os.getenv("WIN_HOST") or "").strip()
    port = (rdp_port or _RUNTIME_CREDS.get("WIN_RDP_PORT") or os.getenv("WIN_RDP_PORT") or "").strip()
    user = (rdp_user or _RUNTIME_CREDS.get("WIN_RDP_USER") or os.getenv("WIN_RDP_USER") or "").strip()
    password = rdp_password or _RUNTIME_CREDS.get("WIN_RDP_PASSWORD") or os.getenv("WIN_RDP_PASSWORD") or ""

    if not host:
        host = _tty_prompt("請輸入 Windows 主機 IP 或位址 (WIN_HOST): ").strip()
    if not port:
        port = _tty_prompt("請輸入 RDP 連接埠 (WIN_RDP_PORT) [預設 3389]: ").strip() or "3389"
    if not user:
        user = _tty_prompt(f"請輸入 {host} 的 Windows 登入帳號 (WIN_RDP_USER): ").strip()
    if not password:
        password = _tty_prompt(f"請輸入 {user}@{host} 的 Windows 登入密碼: ", is_password=True)

    if not host:
        raise ValueError("未提供 Windows 主機位址 (WIN_HOST)。")
    if not user:
        raise ValueError("未提供 Windows 登入帳號 (WIN_RDP_USER)。")
    if not password:
        raise ValueError("未提供 Windows 登入密碼 (WIN_RDP_PASSWORD)；本工具僅允許帳號密碼驗證。")

    _validate_host(host)
    _validate_user(user)
    if not port.isdigit() or not (1 <= int(port) <= 65535):
        raise ValueError(f"非法的 WIN_RDP_PORT：{port!r}（必須為 1~65535 之間的整數）")

    _RUNTIME_CREDS["WIN_HOST"] = host
    _RUNTIME_CREDS["WIN_RDP_PORT"] = port
    _RUNTIME_CREDS["WIN_RDP_USER"] = user
    _RUNTIME_CREDS["WIN_RDP_PASSWORD"] = password
    return host, port, user, password


def _parse_resolution(res_str: str | None = None) -> tuple[int, int]:
    raw = (res_str or os.getenv("WIN_RDP_RESOLUTION") or "1280x800").strip().lower()
    match = re.match(r"^(\d{3,4})x(\d{3,4})$", raw)
    if not match:
        return 1280, 800
    w = max(640, min(int(match.group(1)), 3840))
    h = max(480, min(int(match.group(2)), 2160))
    return w, h


def _build_rdp_file_content(host: str, port: str, user: str, resolution: str | None = None) -> str:
    """產生強制關閉智慧卡／WebAuthn／磁碟掛載後門、僅開啟剪貼簿通道之視窗化 .rdp 設定內容。"""
    w, h = _parse_resolution(resolution)
    full_addr = host if port == "3389" else f"{host}:{port}"
    lines = [
        "screen mode id:i:1",
        "use multimon:i:0",
        f"desktopwidth:i:{w}",
        f"desktopheight:i:{h}",
        "session bpp:i:32",
        "compression:i:1",
        "keyboardhook:i:2",
        "audiocapturemode:i:0",
        "videoplaybackmode:i:1",
        "connection type:i:7",
        "networkautodetect:i:1",
        "bandwidthautodetect:i:1",
        "displayconnectionbar:i:1",
        "enableworkspacereconnect:i:0",
        "disable wallpaper:i:1",
        "allow font smoothing:i:0",
        "allow desktop composition:i:0",
        "disable full window drag:i:1",
        "disable menu anims:i:1",
        "disable themes:i:0",
        "disable cursor setting:i:0",
        "bitmapcachepersistenable:i:1",
        f"full address:s:{full_addr}",
        f"username:s:{user}",
        "audiomode:i:2",
        "redirectprinters:i:0",
        "redirectcomports:i:0",
        "redirectsmartcards:i:0",
        "redirectwebauthn:i:0",
        "redirectclipboard:i:1",
        "redirectposdevices:i:0",
        "redirectdirectx:i:1",
        "redirectdrives:i:0",
        "autoreconnection enabled:i:0",
        "authentication level:i:0",
        "prompt for credentials:i:0",
        "negotiate security layer:i:1",
        "enablecredsspsupport:i:1",
        "remoteapplicationmode:i:0",
    ]
    return "\r\n".join(lines) + "\r\n"


# ── Windows Win32 Credential & GUI Helpers ───────────────────────────────


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _set_ephemeral_rdp_credential(host: str, user: str, password: str) -> None:
    """在 Windows 記憶體工作階段（CRED_PERSIST_SESSION）寫入短暫 TERMSRV 憑證供 mstsc 握手讀取。"""
    if os.name != "nt":
        return
    advapi32 = ctypes.windll.advapi32
    target_name = f"TERMSRV/{host}"
    pw_bytes = password.encode("utf-16-le")
    blob_buf = (ctypes.c_byte * len(pw_bytes)).from_buffer_copy(pw_bytes)

    cred = _CREDENTIALW()
    cred.Flags = 0
    cred.Type = 2  # CRED_TYPE_DOMAIN_PASSWORD
    cred.TargetName = ctypes.cast(ctypes.create_unicode_buffer(target_name), wintypes.LPWSTR)
    cred.CredentialBlobSize = len(pw_bytes)
    cred.CredentialBlob = ctypes.cast(blob_buf, ctypes.POINTER(ctypes.c_byte))
    cred.Persist = 1  # CRED_PERSIST_SESSION（僅限當次登入記憶體，不跨重開機持久化）
    cred.UserName = ctypes.cast(ctypes.create_unicode_buffer(user), wintypes.LPWSTR)

    advapi32.CredWriteW(ctypes.byref(cred), 0)


def _delete_ephemeral_rdp_credential(host: str) -> None:
    """握手結束後立即從 Windows Credential Manager 移除 TERMSRV 憑證，不留免密後門。"""
    if os.name != "nt":
        return
    advapi32 = ctypes.windll.advapi32
    target_name = f"TERMSRV/{host}"
    for cred_type in (2, 1):
        advapi32.CredDeleteW(ctypes.create_unicode_buffer(target_name), cred_type, 0)


def _find_rdp_hwnd_for_host(host: str) -> int:
    """尋找標題包含目標主機位址之 mstsc.exe 遠端桌面視窗 HWND。"""
    if os.name != "nt":
        return 0
    user32 = ctypes.windll.user32
    found_hwnd = 0

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _enum_cb(hwnd: int, _lparam: int) -> bool:
        nonlocal found_hwnd
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        class_buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, class_buf, 64)
        cls_name = class_buf.value
        if host.lower() in title.lower() and (
            "TscShellContainerClass" in cls_name or "Remote Desktop" in title or "遠端桌面" in title
        ):
            found_hwnd = hwnd
            return False
        return True

    user32.EnumWindows(WNDENUMPROC(_enum_cb), 0)
    return found_hwnd


def _focus_rdp_window(hwnd: int) -> bool:
    if os.name != "nt" or not hwnd:
        return False
    user32 = ctypes.windll.user32
    if not user32.IsWindow(hwnd):
        return False
    SW_RESTORE = 9
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.25)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.15)
    return int(user32.GetForegroundWindow()) == int(hwnd)


def _get_client_rect_screen(hwnd: int) -> tuple[int, int, int, int]:
    """取得視窗 Client 區域在螢幕上的 (left, top, width, height)。"""
    user32 = ctypes.windll.user32
    rect = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    width = max(1, rect.right - rect.left)
    height = max(1, rect.bottom - rect.top)
    return pt.x, pt.y, width, height


def _get_clipboard_text() -> str:
    if os.name != "nt":
        return ""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    CF_UNICODETEXT = 13
    if not user32.OpenClipboard(0):
        return ""
    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return ""
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        kernel32.GlobalLock.restype = ctypes.c_void_p
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _set_clipboard_text(text: str) -> None:
    if os.name != "nt":
        return
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    if not user32.OpenClipboard(0):
        return
    try:
        user32.EmptyClipboard()
        data = (text + "\0").encode("utf-16-le")
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h_mem:
            return
        kernel32.GlobalLock.restype = ctypes.c_void_p
        ptr = kernel32.GlobalLock(h_mem)
        if ptr:
            ctypes.memmove(ptr, data, len(data))
            kernel32.GlobalUnlock(h_mem)
            user32.SetClipboardData(CF_UNICODETEXT, h_mem)
    finally:
        user32.CloseClipboard()


def _ensure_rdp_session(
    rdp_host: str = "",
    rdp_port: str = "",
    rdp_user: str = "",
    rdp_password: str = "",
) -> tuple[int, str]:
    """確保目標 Windows 主機的 mstsc.exe 遠端桌面視窗已啟動並連線，回傳 (hwnd, host)。"""
    host, port, user, password = _resolve_connection_params(rdp_host, rdp_port, rdp_user, rdp_password)
    if os.name != "nt":
        raise RuntimeError(
            "目前純 Port 3389 GUI/剪貼簿橋接模式僅支援於 Windows 主機（需使用系統內建 mstsc.exe 與 Win32 API）。"
        )

    mstsc_path = shutil.which("mstsc.exe") or r"C:\Windows\System32\mstsc.exe"
    if not Path(mstsc_path).is_file():
        raise RuntimeError("找不到系統內建的遠端桌面客戶端 (mstsc.exe)。")

    target_key = f"{user}@{host}:{port}"
    existing_hwnd = _ACTIVE_SESSION.get("hwnd") or 0
    if _ACTIVE_SESSION.get("target") == target_key and existing_hwnd and ctypes.windll.user32.IsWindow(existing_hwnd):
        return existing_hwnd, host

    found = _find_rdp_hwnd_for_host(host)
    if found:
        _ACTIVE_SESSION["hwnd"] = found
        _ACTIVE_SESSION["target"] = target_key
        return found, host

    try:
        timeout = max(5, min(int(os.getenv("WIN_RDP_TIMEOUT") or "15"), 60))
    except ValueError:
        timeout = 15

    rdp_content = _build_rdp_file_content(host, port, user)
    tmp_dir = Path(tempfile.mkdtemp(prefix="pocket_rdp_"))
    rdp_file = tmp_dir / "session.rdp"
    rdp_file.write_text(rdp_content, encoding="utf-8")

    try:
        _set_ephemeral_rdp_credential(host, user, password)
        proc = subprocess.Popen([mstsc_path, str(rdp_file)])
        _ACTIVE_SESSION["process"] = proc
        _ACTIVE_SESSION["target"] = target_key

        deadline = time.time() + timeout
        hwnd = 0
        while time.time() < deadline:
            hwnd = _find_rdp_hwnd_for_host(host)
            if hwnd:
                break
            time.sleep(0.4)

        if not hwnd:
            _RUNTIME_CREDS.clear()
            raise RuntimeError(
                f"等待 mstsc.exe 遠端桌面視窗連線至 {host}:{port} 逾時（{timeout} 秒）。"
                "請確認目標主機 Port 3389 可達且帳號密碼正確。"
            )

        # 等待 RDP 完成圖形桌面握手載入後再清除暫存憑證
        time.sleep(2.0)
        _ACTIVE_SESSION["hwnd"] = hwnd
        return hwnd, host
    finally:
        _delete_ephemeral_rdp_credential(host)
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ── Win32 Input Simulation (Strictly Scoped to RDP Window) ───────────────

_VK_MAP = {
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "backspace": 0x08,
    "delete": 0x2E,
    "del": 0x2E,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "shift": 0x10,
    "win": 0x5B,
    "lwin": 0x5B,
    "f1": 0x70,
    "f2": 0x71,
    "f3": 0x72,
    "f4": 0x73,
    "f5": 0x74,
    "f6": 0x75,
    "f7": 0x76,
    "f8": 0x77,
    "f9": 0x78,
    "f10": 0x79,
    "f11": 0x7A,
    "f12": 0x7B,
}


def _send_vk_combo(hwnd: int, combo_str: str) -> None:
    norm = combo_str.strip().lower().replace(" ", "")
    if norm in _BLOCKED_HOTKEYS:
        raise ValueError(f"安全限制：禁止發送系統保護組合鍵 {combo_str!r}。")

    parts = [p for p in norm.split("+") if p]
    if not parts:
        raise ValueError("未指定有效的按鍵名稱。")

    vks: list[int] = []
    for p in parts:
        if p in _VK_MAP:
            vks.append(_VK_MAP[p])
        elif len(p) == 1 and p.isalnum():
            vks.append(ord(p.upper()))
        else:
            raise ValueError(f"不支援的按鍵名稱：{p!r}")

    if not _focus_rdp_window(hwnd):
        raise RuntimeError("無法將遠端桌面視窗置於前景，已中止按鍵發送以保護本機視窗。")

    user32 = ctypes.windll.user32
    KEYEVENTF_KEYUP = 0x0002
    for vk in vks:
        user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.03)
    for vk in reversed(vks):
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.03)


def _send_unicode_text(hwnd: int, text: str) -> None:
    if not _focus_rdp_window(hwnd):
        raise RuntimeError("無法將遠端桌面視窗置於前景，已中止文字輸入以保護本機視窗。")

    user32 = ctypes.windll.user32
    KEYEVENTF_UNICODE = 0x0004
    KEYEVENTF_KEYUP = 0x0002

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_ulonglong),
        ]

    class INPUT_UNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", INPUT_UNION)]

    for ch in text:
        code = ord(ch)
        inp_down = INPUT(type=1, u=INPUT_UNION(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, 0)))
        inp_up = INPUT(type=1, u=INPUT_UNION(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0)))
        user32.SendInput(1, ctypes.byref(inp_down), ctypes.sizeof(INPUT))
        user32.SendInput(1, ctypes.byref(inp_up), ctypes.sizeof(INPUT))
        time.sleep(0.01)


def _run_rdp_clip_command(
    remote_cmd: str,
    rdp_host: str = "",
    rdp_port: str = "",
    rdp_user: str = "",
    rdp_password: str = "",
) -> str:
    """透過 RDP 視窗 Win+R 執行唯讀命令並以 `| clip` 經由 CLIPRDR 虛擬通道取回純文字輸出。"""
    try:
        hwnd, host = _ensure_rdp_session(rdp_host, rdp_port, rdp_user, rdp_password)
        try:
            timeout = max(5, min(int(os.getenv("WIN_RDP_TIMEOUT") or "15"), 60))
        except ValueError:
            timeout = 15

        sentinel = f"__POCKET_AGENT_RDP_WAIT_{uuid.uuid4().hex}__"
        _set_clipboard_text(sentinel)

        _send_vk_combo(hwnd, "win+r")
        time.sleep(0.45)
        wrapped_cmd = f'cmd.exe /c "{remote_cmd} 2>&1 | clip"'
        _send_unicode_text(hwnd, wrapped_cmd)
        time.sleep(0.15)
        _send_vk_combo(hwnd, "enter")

        deadline = time.time() + timeout
        result_text = sentinel
        while time.time() < deadline:
            time.sleep(0.4)
            current = _get_clipboard_text()
            if current and current != sentinel and current != wrapped_cmd:
                result_text = current
                break

        _set_clipboard_text("")
        if result_text == sentinel:
            return (
                f"[ERROR] 透過 RDP 剪貼簿通道於 {host} 執行唯讀巡檢逾時（{timeout} 秒）。\n"
                "可能原因：\n"
                "1. 遠端桌面尚停留在登入確認橫幅（Legal Notice）或鎖定畫面。\n"
                "2. 目標 Windows 主機群組原則停用了剪貼簿重導向（CLIPRDR）。\n"
                "建議改呼叫 `win_rdp_screenshot` 檢視目前遠端桌面畫面狀態。"
            )
        return _redact_and_truncate(result_text.strip())
    except ValueError as e:
        _RUNTIME_CREDS.clear()
        return f"[ERROR] [CONN_AUTH] 參數驗證失敗: {e}"
    except Exception as e:
        msg = str(e)
        tag = "[CONN_AUTH] " if "等待 mstsc.exe 遠端桌面視窗連線" in msg else ""
        return f"[ERROR] {tag}RDP 遠端巡檢執行異常: {e}"


# ── Layer A: RDP GUI Vision & Input Tools ────────────────────────────────


@mcp.tool()
def win_rdp_screenshot(
    monitor_wait_sec: float = 0.5,
    rdp_host: str = "",
    rdp_port: str = "",
    rdp_user: str = "",
    rdp_password: str = "",
) -> str:
    """擷取目前 Windows 遠端桌面（mstsc.exe）視窗畫面並儲存為 PNG 圖檔，回傳圖檔路徑與視窗尺寸。"""
    try:
        hwnd, host = _ensure_rdp_session(rdp_host, rdp_port, rdp_user, rdp_password)
        wait_s = max(0.0, min(float(monitor_wait_sec), 5.0))
        if wait_s > 0:
            time.sleep(wait_s)

        _focus_rdp_window(hwnd)
        left, top, width, height = _get_client_rect_screen(hwnd)

        from PIL import ImageGrab

        img = ImageGrab.grab(bbox=(left, top, left + width, top + height), all_screens=True)
        out_dir = Path(tempfile.gettempdir()) / "pocket_agent_rdp_shots"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"rdp_{host}_{int(time.time())}.png"
        img.save(out_path, format="PNG")
        return (
            f"[OK] 已擷取 {host} 遠端桌面畫面。\n"
            f"- 圖檔路徑: {out_path}\n"
            f"- 視窗客戶區尺寸: {width}x{height} (座標範圍 x: 0~{width - 1}, y: 0~{height - 1})"
        )
    except ValueError as e:
        _RUNTIME_CREDS.clear()
        return f"[ERROR] [CONN_AUTH] 參數驗證失敗: {e}"
    except Exception as e:
        msg = str(e)
        tag = "[CONN_AUTH] " if "等待 mstsc.exe 遠端桌面視窗連線" in msg else ""
        return f"[ERROR] {tag}擷取遠端桌面畫面失敗: {e}"


def _validate_mouse_bounds(x: int, y: int, width: int, height: int) -> None:
    if not (0 <= x < width and 0 <= y < height):
        raise ValueError(
            f"座標 ({x}, {y}) 超出遠端桌面視窗客戶區範圍 (0..{width - 1}, 0..{height - 1})，拒絕點擊視窗外區域。"
        )


@mcp.tool()
def win_rdp_mouse_action(
    action: str,
    x: int,
    y: int,
    button: str = "left",
    scroll_delta: int = 0,
    rdp_host: str = "",
    rdp_port: str = "",
    rdp_user: str = "",
    rdp_password: str = "",
) -> str:
    """在 Windows 遠端桌面視窗內指定相對座標 (x, y) 執行滑鼠操作（click, double_click, right_click, move, scroll）。"""
    try:
        act = action.strip().lower()
        if act not in ("click", "double_click", "right_click", "move", "scroll"):
            raise ValueError(f"不支援的滑鼠動作：{action!r}（僅支援 click, double_click, right_click, move, scroll）")

        hwnd, host = _ensure_rdp_session(rdp_host, rdp_port, rdp_user, rdp_password)
        if not _focus_rdp_window(hwnd):
            return "[ERROR] 無法將遠端桌面視窗置於前景，已中止滑鼠操作。"

        left, top, width, height = _get_client_rect_screen(hwnd)
        _validate_mouse_bounds(int(x), int(y), width, height)

        screen_x = left + int(x)
        screen_y = top + int(y)
        user32 = ctypes.windll.user32
        user32.SetCursorPos(screen_x, screen_y)
        time.sleep(0.05)

        MOUSEEVENTF_LEFTDOWN = 0x0002
        MOUSEEVENTF_LEFTUP = 0x0004
        MOUSEEVENTF_RIGHTDOWN = 0x0008
        MOUSEEVENTF_RIGHTUP = 0x0010
        MOUSEEVENTF_WHEEL = 0x0800

        if act == "click":
            if button.strip().lower() == "right":
                user32.mouse_event(MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
                user32.mouse_event(MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
            else:
                user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        elif act == "right_click":
            user32.mouse_event(MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
            user32.mouse_event(MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
        elif act == "double_click":
            for _ in range(2):
                user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                time.sleep(0.06)
        elif act == "scroll":
            user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, int(scroll_delta), 0)

        return f"[OK] 已於 {host} 遠端桌面視窗座標 ({x}, {y}) 執行 {act}。"
    except ValueError as e:
        return f"[ERROR] 參數驗證失敗: {e}"
    except Exception as e:
        return f"[ERROR] 遠端桌面滑鼠操作失敗: {e}"


@mcp.tool()
def win_rdp_keyboard_action(
    action: str,
    text: str = "",
    keys: str = "",
    rdp_host: str = "",
    rdp_port: str = "",
    rdp_user: str = "",
    rdp_password: str = "",
) -> str:
    """在 Windows 遠端桌面視窗內輸入文字（type_text）或發送按鍵／組合鍵（key_press，如 enter、esc、win+r）。"""
    try:
        act = action.strip().lower()
        if act not in ("type_text", "key_press", "hotkey"):
            raise ValueError(f"不支援的鍵盤動作：{action!r}（僅支援 type_text, key_press, hotkey）")

        hwnd, host = _ensure_rdp_session(rdp_host, rdp_port, rdp_user, rdp_password)
        if act == "type_text":
            if not text:
                raise ValueError("type_text 動作必須提供非空的 text 參數。")
            _send_unicode_text(hwnd, text)
            return f"[OK] 已於 {host} 遠端桌面視窗輸入文字（共 {len(text)} 字元）。"

        if not keys:
            raise ValueError(f"{act} 動作必須提供非空的 keys 參數（例如 'enter' 或 'win+r'）。")
        _send_vk_combo(hwnd, keys)
        return f"[OK] 已於 {host} 遠端桌面視窗發送按鍵 {keys!r}。"
    except ValueError as e:
        return f"[ERROR] 參數驗證失敗: {e}"
    except Exception as e:
        return f"[ERROR] 遠端桌面鍵盤操作失敗: {e}"


@mcp.tool()
def win_rdp_disconnect() -> str:
    """關閉目前由 MCP 管理的 Windows 遠端桌面（mstsc.exe）視窗並清除連線快取。"""
    proc = _ACTIVE_SESSION.get("process")
    hwnd = _ACTIVE_SESSION.get("hwnd") or 0
    if os.name == "nt" and hwnd and ctypes.windll.user32.IsWindow(hwnd):
        WM_CLOSE = 0x0010
        ctypes.windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass
    _ACTIVE_SESSION["process"] = None
    _ACTIVE_SESSION["hwnd"] = 0
    _ACTIVE_SESSION["target"] = ""
    return "[OK] 已關閉 Windows 遠端桌面連線視窗。"


# ── Layer B: 7 Structured Read-Only Inspection Tools (via RDP Clipboard) ──


@mcp.tool()
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
    return _run_rdp_clip_command(cmd, rdp_host, rdp_port, rdp_user, rdp_password)


@mcp.tool()
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
    return _run_rdp_clip_command(cmd, rdp_host, rdp_port, rdp_user, rdp_password)


@mcp.tool()
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
    return _run_rdp_clip_command(cmd, rdp_host, rdp_port, rdp_user, rdp_password)


@mcp.tool()
def win_service_status(
    service_name: str,
    rdp_host: str = "",
    rdp_port: str = "",
    rdp_user: str = "",
    rdp_password: str = "",
) -> str:
    """查詢遠端 Windows 主機特定服務（Windows Service）的執行狀態、PID 與啟動設定（唯讀）。"""
    try:
        clean_svc = _validate_service_name(service_name)
    except ValueError as e:
        return f"[ERROR] {e}"

    cmd = f'echo === SC_QUERYEX & sc.exe queryex "{clean_svc}" & echo === SC_QC & sc.exe qc "{clean_svc}"'
    return _run_rdp_clip_command(cmd, rdp_host, rdp_port, rdp_user, rdp_password)


@mcp.tool()
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
    clean_log = _VALID_EVENT_LOGS.get(log_name.strip().lower())
    if not clean_log:
        allowed = ", ".join(_VALID_EVENT_LOGS.values())
        return f"[ERROR] 非法的 log_name 參數：{log_name!r}（僅允許 {allowed}）"

    clean_lvl = _VALID_EVENT_LEVELS.get(level.strip().lower())
    if not clean_lvl:
        return f"[ERROR] 非法的 level 參數：{level!r}（僅允許 critical, error, warning, info 或 1~4）"

    safe_lines = max(1, min(int(lines), 100))
    cmd = f'wevtutil.exe qe "{clean_log}" "/q:*[System[(Level={clean_lvl})]]" /c:{safe_lines} /rd:true /f:text'
    return _run_rdp_clip_command(cmd, rdp_host, rdp_port, rdp_user, rdp_password)


@mcp.tool()
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
    return _run_rdp_clip_command(cmd, rdp_host, rdp_port, rdp_user, rdp_password)


@mcp.tool()
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
        clean_path = _validate_win_config_path(file_path)
    except ValueError as e:
        return f"[ERROR] {e}"

    safe_lines = max(1, min(int(max_lines), 500))
    cmd = (
        "powershell -NoProfile -NonInteractive -Command "
        f"\"Get-Content -LiteralPath '{clean_path}' -TotalCount {safe_lines}\""
    )
    return _run_rdp_clip_command(cmd, rdp_host, rdp_port, rdp_user, rdp_password)


if __name__ == "__main__":
    mcp.run()
