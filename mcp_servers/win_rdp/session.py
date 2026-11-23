"""Windows RDP 工作階段管理、.rdp 生成與剪貼簿 Enveloped Sentinel 通道（Adapter Pattern）。"""

import ctypes
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from mcp_servers._shared import CredentialCache, tty_prompt
from mcp_servers.win_rdp import security, win32_api

CREDS_CACHE = CredentialCache(ttl_seconds=600.0)
ACTIVE_SESSION: dict[str, Any] = {
    "process": None,
    "pid": 0,
    "exe_path": "",
    "hwnd": 0,
    "target": "",
}


def resolve_connection_params(
    rdp_host: str = "",
    rdp_port: str = "",
    rdp_user: str = "",
    rdp_password: str = "",
) -> tuple[str, str, str, str]:
    """依「注入參數 → 記憶體快取 → 環境變數 → 終端機互動詢問」順序取得 RDP 連線四要素。"""
    with CREDS_CACHE.lock:
        cached_host = CREDS_CACHE.get("WIN_HOST")
        cached_port = CREDS_CACHE.get("WIN_RDP_PORT")
        cached_user = CREDS_CACHE.get("WIN_RDP_USER")

        host = rdp_host.strip() or cached_host or (os.getenv("WIN_HOST") or "").strip()
        port = rdp_port.strip() or cached_port or (os.getenv("WIN_RDP_PORT") or "").strip()
        user = rdp_user.strip() or cached_user or (os.getenv("WIN_RDP_USER") or "").strip()

        if not host:
            host = tty_prompt("請輸入 Windows 主機 IP 或位址 (WIN_HOST): ").strip()
        if not port:
            port = tty_prompt("請輸入 RDP 連接埠 (WIN_RDP_PORT) [預設 3389]: ").strip() or "3389"
        if not user:
            user = tty_prompt(f"請輸入 {host} 的 Windows 登入帳號 (WIN_RDP_USER): ").strip()

        if not host:
            raise ValueError("未提供 Windows 主機位址 (WIN_HOST)。")
        if not user:
            raise ValueError("未提供 Windows 登入帳號 (WIN_RDP_USER)。")

        security.validate_host(host)
        security.validate_user(user)
        if not port.isdigit() or not (1 <= int(port) <= 65535):
            raise ValueError(f"非法的 WIN_RDP_PORT：{port!r}（必須為 1~65535 之間的整數）")
        port = str(int(port))

        target = (host, port, user)
        cached_target = (cached_host, cached_port, cached_user)
        env_target = (
            (os.getenv("WIN_HOST") or "").strip(),
            (os.getenv("WIN_RDP_PORT") or "3389").strip(),
            (os.getenv("WIN_RDP_USER") or "").strip(),
        )

        password = rdp_password
        if not password and target == cached_target:
            password = CREDS_CACHE.get("WIN_RDP_PASSWORD")
        if not password and target == env_target:
            password = os.getenv("WIN_RDP_PASSWORD") or ""

        if not password:
            password = tty_prompt(f"請輸入 {user}@{host} 的 Windows 登入密碼: ", is_password=True)
        if not password:
            raise ValueError("未提供 Windows 登入密碼 (WIN_RDP_PASSWORD)；本工具僅允許帳號密碼驗證。")

        CREDS_CACHE.update(
            {
                "WIN_HOST": host,
                "WIN_RDP_PORT": port,
                "WIN_RDP_USER": user,
                "WIN_RDP_PASSWORD": password,
            }
        )
        return host, port, user, password


def parse_resolution(res_str: str | None = None) -> tuple[int, int]:
    raw = (res_str or os.getenv("WIN_RDP_RESOLUTION") or "1280x800").strip().lower()
    match = re.match(r"^(\d{3,4})x(\d{3,4})$", raw)
    if not match:
        return 1280, 800
    return max(640, min(int(match.group(1)), 3840)), max(480, min(int(match.group(2)), 2160))


def build_rdp_file_content(host: str, port: str, resolution: str | None = None) -> str:
    """產生強制關閉智慧卡／WebAuthn／磁碟掛載、僅開啟剪貼簿通道且零帳密落盤之 .rdp 設定內容。"""
    w, h = parse_resolution(resolution)
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


def verify_active_session(hwnd: int, expected_target: str | None = None) -> bool:
    """驗證當前 ACTIVE_SESSION 的視窗、行程、映像路徑與目標鍵是否有效且一致。"""
    active_hwnd = int(ACTIVE_SESSION.get("hwnd") or 0)
    active_pid = int(ACTIVE_SESSION.get("pid") or 0)
    active_exe = str(ACTIVE_SESSION.get("exe_path") or "")
    active_proc = ACTIVE_SESSION.get("process")
    active_target = str(ACTIVE_SESSION.get("target") or "")
    if not active_hwnd or int(hwnd) != active_hwnd:
        return False
    if expected_target is not None and active_target != expected_target:
        return False
    if active_proc is None or active_proc.poll() is not None:
        return False
    return win32_api.verify_rdp_hwnd_identity(hwnd, active_pid, active_exe, active_proc)


def focus_rdp_window(hwnd: int) -> bool:
    if os.name != "nt" or not hwnd:
        return False
    if not verify_active_session(hwnd):
        return False
    user32 = ctypes.windll.user32
    SW_RESTORE = 9
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.25)
    if user32.IsIconic(hwnd):
        return False
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.15)
    if int(user32.GetForegroundWindow()) != int(hwnd):
        return False
    return verify_active_session(hwnd)


def send_vk_combo(hwnd: int, combo_str: str) -> None:
    win32_api.send_vk_combo(hwnd, combo_str, focus_fn=focus_rdp_window)


def send_unicode_text(hwnd: int, text: str) -> None:
    win32_api.send_unicode_text(hwnd, text, focus_fn=focus_rdp_window)


def disconnect_session() -> str:
    """關閉目前由 MCP 管理的 Windows 遠端桌面（mstsc.exe）視窗並清除工作階段狀態。"""
    proc = ACTIVE_SESSION.get("process")
    hwnd = ACTIVE_SESSION.get("hwnd") or 0
    if os.name == "nt" and hwnd and ctypes.windll.user32.IsWindow(hwnd):
        WM_CLOSE = 0x0010
        ctypes.windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass
    ACTIVE_SESSION.update({"process": None, "pid": 0, "exe_path": "", "hwnd": 0, "target": ""})
    return "[OK] 已關閉 Windows 遠端桌面連線視窗。"


def ensure_rdp_session(
    rdp_host: str = "",
    rdp_port: str = "",
    rdp_user: str = "",
    rdp_password: str = "",
) -> tuple[int, str]:
    """確保目標 Windows 主機的 mstsc.exe 遠端桌面視窗已啟動並通過行程身分驗證。"""
    host, port, user, password = resolve_connection_params(rdp_host, rdp_port, rdp_user, rdp_password)
    if os.name != "nt":
        raise RuntimeError(
            "目前純 Port 3389 GUI/剪貼簿橋接模式僅支援於 Windows 主機（需使用系統內建 mstsc.exe 與 Win32 API）。"
        )

    mstsc_path = win32_api.get_trusted_mstsc_path()
    target_key = f"{user}@{host}:{port}"
    existing_hwnd = int(ACTIVE_SESSION.get("hwnd") or 0)
    if existing_hwnd and verify_active_session(existing_hwnd, expected_target=target_key):
        return existing_hwnd, host

    if ACTIVE_SESSION.get("process") is not None or ACTIVE_SESSION.get("hwnd"):
        disconnect_session()

    try:
        timeout = max(5, min(int(os.getenv("WIN_RDP_TIMEOUT") or "15"), 60))
    except ValueError:
        timeout = 15

    rdp_content = build_rdp_file_content(host, port)
    tmp_dir = Path(tempfile.mkdtemp(prefix="pocket_rdp_"))
    rdp_file = tmp_dir / "session.rdp"
    rdp_file.write_text(rdp_content, encoding="utf-8")

    try:
        win32_api.set_ephemeral_rdp_credential(host, user, password)
        proc = subprocess.Popen([mstsc_path, str(rdp_file)])
        proc_pid = int(getattr(proc, "pid", 0) or 0)
        ACTIVE_SESSION.update(
            {
                "process": proc,
                "pid": proc_pid,
                "exe_path": mstsc_path,
                "hwnd": 0,
                "target": target_key,
            }
        )

        deadline = time.time() + timeout
        hwnd = 0
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            hwnd = win32_api.find_rdp_hwnd_for_process(proc_pid, mstsc_path, proc)
            if hwnd:
                break
            time.sleep(0.4)

        if not hwnd:
            disconnect_session()
            CREDS_CACHE.clear()
            raise RuntimeError(
                f"等待 mstsc.exe (PID={proc_pid}) 遠端桌面視窗連線至 {host}:{port} 逾時（{timeout} 秒）。"
                "為確保安全，拒絕接管無法驗證來源的既有視窗；請確認 Port 3389 可達且帳密正確。"
            )

        time.sleep(2.0)
        if not win32_api.verify_rdp_hwnd_identity(hwnd, proc_pid, mstsc_path, proc):
            disconnect_session()
            CREDS_CACHE.clear()
            raise RuntimeError("遠端桌面視窗於握手期間失效或未通過行程身分驗證。")
        ACTIVE_SESSION["hwnd"] = hwnd
        return hwnd, host
    finally:
        win32_api.delete_ephemeral_rdp_credential(host)
        shutil.rmtree(tmp_dir, ignore_errors=True)


def run_rdp_clip_command(
    remote_cmd: str,
    rdp_host: str = "",
    rdp_port: str = "",
    rdp_user: str = "",
    rdp_password: str = "",
) -> str:
    """透過 RDP 視窗 Win+R 與剪貼簿 Enveloped Sentinel 通道取回唯讀巡檢文字，結束立即還原剪貼簿。"""
    orig_clipboard = ""
    try:
        hwnd, host = ensure_rdp_session(rdp_host, rdp_port, rdp_user, rdp_password)
        try:
            timeout = max(5, min(int(os.getenv("WIN_RDP_TIMEOUT") or "15"), 60))
        except ValueError:
            timeout = 15

        orig_clipboard = win32_api.get_clipboard_text()
        token = uuid.uuid4().hex
        start_tag = f"__PKT_CLIP_START_{token}__"
        end_tag = f"__PKT_CLIP_END_{token}__"
        wait_marker = f"__PKT_WAIT_{token}__"
        win32_api.set_clipboard_text(wait_marker)

        send_vk_combo(hwnd, "win+r")
        time.sleep(0.45)
        escaped_cmd = (
            remote_cmd.replace("^", "^^").replace("&", "^&").replace("|", "^|").replace("<", "^<").replace(">", "^>")
        )
        wrapped_cmd = f'cmd.exe /c "(echo {start_tag} & {escaped_cmd} & echo {end_tag}) 2>&1 | clip"'
        send_unicode_text(hwnd, wrapped_cmd)
        time.sleep(0.15)
        send_vk_combo(hwnd, "enter")

        deadline = time.time() + timeout
        result_text = None
        while time.time() < deadline:
            time.sleep(0.4)
            current = win32_api.get_clipboard_text()
            if current and start_tag in current and end_tag in current:
                s_idx = current.find(start_tag) + len(start_tag)
                e_idx = current.find(end_tag)
                result_text = current[s_idx:e_idx].strip()
                break

        if result_text is None:
            return (
                f"[ERROR] 透過 RDP 剪貼簿通道於 {host} 執行唯讀巡檢逾時（{timeout} 秒）。\n"
                "可能原因：\n"
                "1. 遠端桌面尚停留在登入確認橫幅（Legal Notice）或鎖定畫面。\n"
                "2. 目標 Windows 主機群組原則停用了剪貼簿重導向（CLIPRDR）。\n"
                "建議改呼叫 `win_rdp_screenshot` 檢視目前遠端桌面畫面狀態。"
            )
        return security.redact_and_truncate(result_text)
    except ValueError as exc:
        CREDS_CACHE.clear()
        return f"[ERROR] [CONN_AUTH] 參數驗證失敗: {exc}"
    except Exception as exc:
        msg = str(exc)
        tag = "[CONN_AUTH] " if "遠端桌面視窗連線" in msg else ""
        return f"[ERROR] {tag}RDP 遠端巡檢執行異常: {exc}"
    finally:
        if orig_clipboard is not None:
            win32_api.set_clipboard_text(orig_clipboard)
