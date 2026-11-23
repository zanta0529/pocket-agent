"""Windows Win32 Credential、HWND 身分綁定、剪貼簿與前景保護輸入轉接器（Adapter Pattern）。"""

import ctypes
import os
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

from mcp_servers.win_rdp import security

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


def set_ephemeral_rdp_credential(host: str, user: str, password: str) -> None:
    """在 Windows 記憶體工作階段（CRED_PERSIST_SESSION）寫入短暫 TERMSRV 憑證供 mstsc 握手讀取。"""
    if os.name != "nt":
        return
    advapi32 = ctypes.windll.advapi32
    target_name = f"TERMSRV/{host}"
    pw_bytes = password.encode("utf-16-le")
    blob_buf = (ctypes.c_byte * len(pw_bytes)).from_buffer_copy(pw_bytes)

    cred = _CREDENTIALW()
    cred.Flags = 0
    cred.Type = 2
    cred.TargetName = ctypes.cast(ctypes.create_unicode_buffer(target_name), wintypes.LPWSTR)
    cred.CredentialBlobSize = len(pw_bytes)
    cred.CredentialBlob = ctypes.cast(blob_buf, ctypes.POINTER(ctypes.c_byte))
    cred.Persist = 1
    cred.UserName = ctypes.cast(ctypes.create_unicode_buffer(user), wintypes.LPWSTR)

    advapi32.CredWriteW(ctypes.byref(cred), 0)


def delete_ephemeral_rdp_credential(host: str) -> None:
    """握手結束後立即從 Windows Credential Manager 移除 TERMSRV 憑證。"""
    if os.name != "nt":
        return
    advapi32 = ctypes.windll.advapi32
    target_name = f"TERMSRV/{host}"
    for cred_type in (2, 1):
        advapi32.CredDeleteW(ctypes.create_unicode_buffer(target_name), cred_type, 0)


def get_trusted_mstsc_path() -> str:
    """僅綁定 Windows 系統目錄下的可信 mstsc.exe，不信任 PATH 中的同名執行檔。"""
    sys_root = (os.environ.get("SystemRoot") or r"C:\Windows").strip()
    mstsc_candidate = Path(sys_root) / "System32" / "mstsc.exe"
    if not mstsc_candidate.is_file():
        raise RuntimeError("找不到系統內建的遠端桌面客戶端 (mstsc.exe)。")
    return os.path.normpath(str(mstsc_candidate))


def get_window_pid(hwnd: int) -> int:
    if os.name != "nt" or not hwnd:
        return 0
    user32 = ctypes.windll.user32
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def get_process_image_path(pid: int) -> str:
    if os.name != "nt" or pid <= 0:
        return ""
    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h_proc:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(1024)
        ok = kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size))
        return buf.value if ok else ""
    finally:
        kernel32.CloseHandle(h_proc)


def get_window_class_name(hwnd: int) -> str:
    if os.name != "nt" or not hwnd:
        return ""
    user32 = ctypes.windll.user32
    class_buf = ctypes.create_unicode_buffer(128)
    length = user32.GetClassNameW(hwnd, class_buf, 128)
    return class_buf.value if length > 0 else ""


def verify_rdp_hwnd_identity(hwnd: int, expected_pid: int, expected_exe: str, proc: Any = None) -> bool:
    """驗證 HWND 是否屬於本工具啟動之 mstsc.exe 且類別為 TscShellContainerClass。"""
    if os.name != "nt" or not hwnd or expected_pid <= 0 or not expected_exe:
        return False
    if proc is not None and proc.poll() is not None:
        return False
    user32 = ctypes.windll.user32
    if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd):
        return False
    if get_window_class_name(hwnd) != "TscShellContainerClass":
        return False
    win_pid = get_window_pid(hwnd)
    if win_pid != int(expected_pid):
        return False
    actual_exe = get_process_image_path(win_pid)
    if not actual_exe:
        return False
    return os.path.normcase(os.path.normpath(actual_exe)) == os.path.normcase(os.path.normpath(expected_exe))


def find_rdp_hwnd_for_process(expected_pid: int, expected_exe: str, proc: Any = None) -> int:
    """尋找屬於指定 mstsc.exe PID、可信路徑且類別為 TscShellContainerClass 之 HWND。"""
    if os.name != "nt" or expected_pid <= 0 or not expected_exe:
        return 0
    user32 = ctypes.windll.user32
    found_hwnd = 0
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _enum_cb(hwnd: int, _lparam: int) -> bool:
        nonlocal found_hwnd
        if verify_rdp_hwnd_identity(hwnd, expected_pid, expected_exe, proc):
            found_hwnd = hwnd
            return False
        return True

    user32.EnumWindows(WNDENUMPROC(_enum_cb), 0)
    return found_hwnd


def get_client_rect_screen(hwnd: int) -> tuple[int, int, int, int]:
    """取得視窗 Client 區域在螢幕上的 (left, top, width, height)。"""
    user32 = ctypes.windll.user32
    rect = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y, max(1, rect.right - rect.left), max(1, rect.bottom - rect.top)


def get_clipboard_text() -> str:
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


def set_clipboard_text(text: str) -> None:
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


def send_vk_combo(hwnd: int, combo_str: str, *, focus_fn: Any) -> None:
    norm = combo_str.strip().lower().replace(" ", "")
    if norm in security.BLOCKED_HOTKEYS:
        raise ValueError(f"安全限制：禁止發送系統保護組合鍵 {combo_str!r}。")

    parts = [p for p in norm.split("+") if p]
    if not parts:
        raise ValueError("未指定有效的按鍵名稱。")

    vks: list[int] = []
    for part in parts:
        if part in _VK_MAP:
            vks.append(_VK_MAP[part])
        elif len(part) == 1 and part.isalnum():
            vks.append(ord(part.upper()))
        else:
            raise ValueError(f"不支援的按鍵名稱：{part!r}")

    if not focus_fn(hwnd):
        raise RuntimeError("無法將遠端桌面視窗置於前景，已中止按鍵發送以保護本機視窗。")

    user32 = ctypes.windll.user32
    if int(user32.GetForegroundWindow()) != int(hwnd):
        raise RuntimeError("遠端桌面視窗失去前景焦點，已中止按鍵發送以保護本機視窗。")

    KEYEVENTF_KEYUP = 0x0002
    pressed_vks: list[int] = []
    try:
        for vk in vks:
            if int(user32.GetForegroundWindow()) != int(hwnd):
                raise RuntimeError("偵測到前景視窗焦點轉移，已緊急中止按鍵發送以杜絕注入本機視窗。")
            user32.keybd_event(vk, 0, 0, 0)
            pressed_vks.append(vk)
            time.sleep(0.03)
    finally:
        for vk in reversed(pressed_vks):
            try:
                user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
            except Exception:
                pass
            time.sleep(0.03)


def send_unicode_text(hwnd: int, text: str, *, focus_fn: Any) -> None:
    if not focus_fn(hwnd):
        raise RuntimeError("無法將遠端桌面視窗置於前景，已中止文字輸入以保護本機視窗。")

    user32 = ctypes.windll.user32
    if int(user32.GetForegroundWindow()) != int(hwnd):
        raise RuntimeError("遠端桌面視窗失去前景焦點，已中止文字輸入以保護本機視窗。")

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
        if int(user32.GetForegroundWindow()) != int(hwnd):
            raise RuntimeError("偵測到前景視窗焦點轉移，已緊急中止文字輸入以杜絕注入本機視窗。")
        code = ord(ch)
        inp_down = INPUT(type=1, u=INPUT_UNION(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, 0)))
        inp_up = INPUT(type=1, u=INPUT_UNION(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0)))
        user32.SendInput(1, ctypes.byref(inp_down), ctypes.sizeof(INPUT))
        user32.SendInput(1, ctypes.byref(inp_up), ctypes.sizeof(INPUT))
        time.sleep(0.01)
