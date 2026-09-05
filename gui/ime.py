"""Windows IME 協助：讀取組字（preedit）狀態。

Tk 在 Windows 上遇到輸入法切換（Shift、Ctrl+Space 等）時，會直接丟棄
還沒 commit 的組字內容。這裡用 imm32 API 讀出組字字串，讓 GUI 在
內容被丟棄時手動補回輸入框。非 Windows 平台皆為 no-op。

注意：不要用 ImmNotifyIME(NI_COMPOSITIONSTR, CPS_COMPLETE) 強制 commit——
Windows 10/11 的 TSF 架構輸入法（新版微軟注音/拼音）會把它當成「取消」，
直接清空組字。
"""

import sys

GCS_COMPSTR = 0x0008
NI_COMPOSITIONSTR = 0x0015
CPS_COMPLETE = 0x0001

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.windll.user32
    _imm32 = ctypes.windll.imm32

    _user32.GetFocus.restype = wintypes.HWND
    _imm32.ImmGetContext.argtypes = [wintypes.HWND]
    _imm32.ImmGetContext.restype = ctypes.c_void_p
    _imm32.ImmReleaseContext.argtypes = [wintypes.HWND, ctypes.c_void_p]
    _imm32.ImmGetCompositionStringW.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    _imm32.ImmNotifyIME.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]

    def get_composition() -> str:
        """回傳目前焦點視窗未 commit 的組字字串（沒有則空字串）。"""
        hwnd = _user32.GetFocus()
        if not hwnd:
            return ""
        himc = _imm32.ImmGetContext(hwnd)
        if not himc:
            return ""
        try:
            nbytes = _imm32.ImmGetCompositionStringW(himc, GCS_COMPSTR, None, 0)
            if nbytes <= 0:
                return ""
            buf = ctypes.create_unicode_buffer(nbytes // 2 + 1)
            _imm32.ImmGetCompositionStringW(himc, GCS_COMPSTR, buf, nbytes)
            return ctypes.wstring_at(buf, nbytes // 2)
        finally:
            _imm32.ImmReleaseContext(hwnd, himc)

    def has_composition() -> bool:
        """目前焦點視窗是否有未 commit 的組字。"""
        return bool(get_composition())

else:

    def get_composition() -> str:
        return ""

    def has_composition() -> bool:
        return False
