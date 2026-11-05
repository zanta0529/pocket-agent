"""跨平台剪貼簿內容萃取與記憶體截圖快取模組。"""

import hashlib
import io
import logging
import platform
import subprocess
import urllib.parse
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageGrab

from pocket_agent.system.paths import scratch_dir

log = logging.getLogger("pocket-agent")


def get_clipboard_image() -> Image.Image | None:
    """嘗試從系統剪貼簿讀取記憶體點陣圖。"""
    try:
        data = ImageGrab.grabclipboard()
        if isinstance(data, Image.Image):
            return data
    except Exception as e:
        log.debug("PIL ImageGrab.grabclipboard 讀取失敗：%s", e)

    # Linux (Wayland / X11) 後備機制
    if platform.system() == "Linux":
        for cmd in [
            ["wl-paste", "--type", "image/png"],
            ["xclip", "-selection", "clipboard", "-target", "image/png"],
        ]:
            try:
                proc = subprocess.run(cmd, capture_output=True, check=True)
                if proc.stdout and proc.stdout.startswith(b"\x89PNG"):
                    return Image.open(io.BytesIO(proc.stdout))
            except Exception:
                continue

    return None


def save_clipboard_image(image: Image.Image) -> Path:
    """將剪貼簿圖片儲存至 scratch/clipboard 目錄，並依內容雜湊避免重複產生實體檔案。"""
    target_dir = scratch_dir() / "clipboard"
    target_dir.mkdir(parents=True, exist_ok=True)

    buf = io.BytesIO()
    image.save(buf, format="PNG")
    png_bytes = buf.getvalue()
    img_hash = hashlib.sha256(png_bytes).hexdigest()[:8]

    # 若目錄中已有相同雜湊的截圖檔案，直接重用既有檔案
    for existing in target_dir.glob(f"*_{img_hash}.png"):
        if existing.is_file() and existing.stat().st_size == len(png_bytes):
            return existing

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_path = target_dir / f"screenshot_{timestamp}_{img_hash}.png"
    file_path.write_bytes(png_bytes)
    return file_path


def get_clipboard_attachments() -> tuple[list[str], bool]:
    """從剪貼簿擷取附件。

    優先順序：
    1. 實體檔案清單（例如從 Windows 檔案總管複製檔案，CF_HDROP = 15）
    2. 剪貼簿記憶體截圖（例如 Win+Shift+S 區域截圖或瀏覽器複製圖片）
    3. 純文字路徑（包含 file:// URI 或現存檔案路徑）

    回傳：
        (檔案路徑清單, 是否為記憶體截圖暫存檔)
    """
    sys_name = platform.system()

    # 1. 優先檢查是否為實體檔案清單 (從檔案總管複製)
    if sys_name == "Windows":
        try:
            import win32clipboard

            win32clipboard.OpenClipboard()
            try:
                if win32clipboard.IsClipboardFormatAvailable(15):  # CF_HDROP
                    paths = win32clipboard.GetClipboardData(15)
                    valid = [str(Path(p).resolve()) for p in paths if Path(p).is_file()]
                    if valid:
                        return valid, False
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            pass

    # 2. 檢查是否有記憶體截圖
    img = get_clipboard_image()
    if img is not None:
        try:
            saved_path = save_clipboard_image(img)
            return [str(saved_path.resolve())], True
        except Exception as e:
            log.warning("儲存剪貼簿截圖失敗：%s", e)

    # 3. 檢查純文字路徑
    text_data = ""
    if sys_name == "Windows":
        try:
            import win32clipboard

            win32clipboard.OpenClipboard()
            try:
                if win32clipboard.IsClipboardFormatAvailable(13):  # CF_UNICODETEXT
                    text_data = win32clipboard.GetClipboardData(13)
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            pass
    elif sys_name == "Darwin":
        try:
            text_data = subprocess.run(["pbpaste"], capture_output=True, text=True, check=True).stdout
        except Exception:
            pass
    elif sys_name == "Linux":
        for cmd in [
            ["wl-paste"],
            ["xclip", "-selection", "clipboard", "-o"],
            ["xsel", "--clipboard", "--output"],
        ]:
            try:
                text_data = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
                if text_data:
                    break
            except Exception:
                continue

    if text_data:
        valid_paths = []
        for line in text_data.splitlines():
            clean_line = line.strip(" \t\r\n\"'")
            if clean_line.startswith("file://"):
                clean_line = urllib.parse.unquote(clean_line[7:])
            if not clean_line:
                continue
            try:
                pth = Path(clean_line).expanduser().resolve()
                if pth.is_file():
                    valid_paths.append(str(pth))
            except Exception:
                pass
        if valid_paths:
            return valid_paths, False

    return [], False
