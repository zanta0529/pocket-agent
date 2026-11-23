"""Windows RDP Layer A 視覺截圖與滑鼠鍵盤互動工具群組（Command / Registrar Pattern）。"""

import ctypes
import tempfile
import time
from pathlib import Path
from typing import Any

from mcp_servers.win_rdp import security, session, win32_api


def win_rdp_screenshot(
    monitor_wait_sec: float = 0.5,
    rdp_host: str = "",
    rdp_port: str = "",
    rdp_user: str = "",
    rdp_password: str = "",
) -> str:
    """擷取目前 Windows 遠端桌面（mstsc.exe）視窗畫面並儲存為 PNG 圖檔，回傳圖檔路徑與視窗尺寸。"""
    try:
        hwnd, host = session.ensure_rdp_session(rdp_host, rdp_port, rdp_user, rdp_password)
        wait_s = max(0.0, min(float(monitor_wait_sec), 5.0))
        if wait_s > 0:
            time.sleep(wait_s)

        if not session.verify_active_session(hwnd):
            return "[ERROR] 遠端桌面視窗工作階段驗證失敗，已拒絕擷取畫面。"
        if not session.focus_rdp_window(hwnd):
            return "[ERROR] 無法將遠端桌面視窗置於前景，已中止擷取遠端桌面畫面。"
        if not session.verify_active_session(hwnd):
            return "[ERROR] 遠端桌面視窗於對焦後身分失效，已中止擷取畫面。"

        _, _, width, height = win32_api.get_client_rect_screen(hwnd)

        from PIL import ImageGrab

        img = ImageGrab.grab(window=hwnd)
        if img is None:
            raise RuntimeError("指定視窗擷取回傳空影像。")
        if not session.verify_active_session(hwnd):
            return "[ERROR] 擷取期間遠端桌面視窗工作階段狀態變更，已拒絕儲存截圖。"

        out_dir = Path(tempfile.gettempdir()) / "pocket_agent_rdp_shots"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"rdp_{host}_{int(time.time())}.png"
        img.save(out_path, format="PNG")
        return (
            f"[OK] 已擷取 {host} 遠端桌面畫面。\n"
            f"- 圖檔路徑: {out_path}\n"
            f"- 視窗客戶區尺寸: {width}x{height} (座標範圍 x: 0~{width - 1}, y: 0~{height - 1})"
        )
    except ValueError as exc:
        session.CREDS_CACHE.clear()
        return f"[ERROR] [CONN_AUTH] 參數驗證失敗: {exc}"
    except Exception as exc:
        msg = str(exc)
        tag = "[CONN_AUTH] " if "遠端桌面視窗連線" in msg else ""
        return f"[ERROR] {tag}擷取遠端桌面畫面失敗: {exc}"


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

        hwnd, host = session.ensure_rdp_session(rdp_host, rdp_port, rdp_user, rdp_password)
        if not session.focus_rdp_window(hwnd):
            return "[ERROR] 無法將遠端桌面視窗置於前景，已中止滑鼠操作。"

        left, top, width, height = win32_api.get_client_rect_screen(hwnd)
        security.validate_mouse_bounds(int(x), int(y), width, height)

        user32 = ctypes.windll.user32
        user32.SetCursorPos(left + int(x), top + int(y))
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
    except ValueError as exc:
        return f"[ERROR] 參數驗證失敗: {exc}"
    except Exception as exc:
        return f"[ERROR] 遠端桌面滑鼠操作失敗: {exc}"


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

        hwnd, host = session.ensure_rdp_session(rdp_host, rdp_port, rdp_user, rdp_password)
        if act == "type_text":
            if not text:
                raise ValueError("type_text 動作必須提供非空的 text 參數。")
            session.send_unicode_text(hwnd, text)
            return f"[OK] 已於 {host} 遠端桌面視窗輸入文字（共 {len(text)} 字元）。"

        if not keys:
            raise ValueError(f"{act} 動作必須提供非空的 keys 參數（例如 'enter' 或 'win+r'）。")
        session.send_vk_combo(hwnd, keys)
        return f"[OK] 已於 {host} 遠端桌面視窗發送按鍵 {keys!r}。"
    except ValueError as exc:
        return f"[ERROR] 參數驗證失敗: {exc}"
    except Exception as exc:
        return f"[ERROR] 遠端桌面鍵盤操作失敗: {exc}"


def win_rdp_disconnect() -> str:
    """關閉目前由 MCP 管理的 Windows 遠端桌面（mstsc.exe）視窗並清除連線快取。"""
    return session.disconnect_session()


def register(mcp: Any) -> None:
    """註冊 Windows RDP Layer A 視覺與輸入工具至 MCPServer。"""
    for fn in (win_rdp_screenshot, win_rdp_mouse_action, win_rdp_keyboard_action, win_rdp_disconnect):
        mcp.tool()(fn)
