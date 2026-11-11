"""附件轉換：全部在本地擷取文字/編碼，嵌入 messages（不經任何遠端上傳 API）。

這裡的模型 client 指向 ChatGPT 的 codex 後端（chatgpt.com/backend-api/codex），
不是標準 OpenAI Platform API，並不保證支援 Files API；過去曾經呼叫
client.files.create() 上傳大檔，若後端不支援該端點，這個沒有 timeout
的呼叫可能整個卡住、且 Stop 鍵按了也沒用（cancel_requested 不會在這裡被檢查）。
改成本地處理後就不再有任何會卡住的網路呼叫。
"""

import logging
import mimetypes
from pathlib import Path
from typing import Callable, Optional

from openai import AsyncOpenAI

from pocket_agent.core.cancellation import run_sync

log = logging.getLogger("pocket-agent")

# 支援 vision 的圖片類型
IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

# 走純文字讀取的文件類型（PDF 另外用專門的解析器，見下方）
TEXT_DOCUMENT_TYPES = {
    ".txt",
    ".md",
    ".csv",
    ".json",
    ".py",
    ".js",
    ".ts",
    ".html",
    ".css",
    ".c",
    ".cpp",
    ".h",
    ".java",
    ".rb",
    ".go",
    ".rs",
}

# 嵌入內容的字數上限（含說明文字），避免單一附件灌爆 context
MAX_DOC_CHARS = 60_000

# PDF 內嵌圖片：張數與邊長上限，避免圖表多的 PDF 把單輪對話灌爆
MAX_PDF_IMAGES = 12
MAX_IMAGE_DIMENSION = 1568  # 對齊常見 vision 模型的建議上限
MIN_IMAGE_DIMENSION = 32  # 小於這個尺寸多半是裝飾用小圖示，略過


class FileUploader:
    def __init__(self, client: AsyncOpenAI, cancel_check: Optional[Callable[[], bool]] = None):
        self.client = client
        self.cancel_check = cancel_check or (lambda: False)

    async def upload(self, path: str, workspace_dirs: list[str] | None = None) -> dict | list[dict]:
        """
        轉換附件，回傳可放入 messages content 的字典，或多個字典組成的 list
        （PDF 內嵌圖片時，會是「文字說明 + 數張圖片」）。
        單一附件處理失敗不拋例外——回傳說明性文字，讓對話能繼續進行。
        """
        try:
            return await self._convert(path, workspace_dirs=workspace_dirs)
        except Exception as e:
            log.exception("附件處理失敗：%s", path)
            name = Path(path).name
            return {
                "type": "input_text",
                "text": f"[附件「{name}」處理失敗，已略過：{e}]",
            }

    async def _convert(self, path: str, workspace_dirs: list[str] | None = None) -> dict | list[dict]:
        from pocket_agent.tools.fs import _validate_safe_path

        p = _validate_safe_path(path, workspace_dirs)
        if not p.exists():
            raise FileNotFoundError(f"找不到檔案或目錄：{p}")

        if p.is_dir():
            return await run_sync(self._read_directory, p)

        ext = p.suffix.lower()

        if ext in IMAGE_TYPES:
            return await self._read_image(p)
        elif ext == ".pdf":
            # 逐頁解析可能較慢，丟到 thread 避免卡住共用的 asyncio event loop
            return await run_sync(self._read_pdf, p)
        else:
            # TEXT_DOCUMENT_TYPES 或未知副檔名，都嘗試當文字讀（容錯路徑）
            return await run_sync(self._read_text_file, p)

    async def _read_image(self, p: Path) -> dict:
        """圖片轉 base64 data URL，直接嵌入 message。"""
        import base64

        mime = mimetypes.guess_type(str(p))[0] or "image/jpeg"
        b64 = base64.b64encode(p.read_bytes()).decode()
        return {
            "type": "input_image",
            "image_url": f"data:{mime};base64,{b64}",
        }

    @staticmethod
    def _truncate(text: str) -> tuple[str, bool]:
        if len(text) <= MAX_DOC_CHARS:
            return text, False
        return text[:MAX_DOC_CHARS], True

    def _read_pdf(self, p: Path) -> list[dict]:
        """用 pypdf 逐頁擷取文字＋內嵌圖片（PDF 是二進位格式，不能當純文字讀）。"""
        from pypdf import PdfReader

        reader = PdfReader(str(p))
        page_count = len(reader.pages)

        pages_text = []
        image_parts: list[dict] = []
        images_skipped = 0

        for page in reader.pages:
            if self.cancel_check():
                pages_text.append("\n\n... (讀取已由使用者中斷) ...\n\n")
                break

            pages_text.append(page.extract_text() or "")

            try:
                page_images = list(page.images)
            except Exception:
                page_images = []

            for img in page_images:
                if len(image_parts) >= MAX_PDF_IMAGES:
                    images_skipped += 1
                    continue
                part = self._pdf_image_to_part(img)
                if part is not None:
                    image_parts.append(part)
                else:
                    images_skipped += 1

        text = "\n\n".join(pages_text).strip()

        if not text and not image_parts:
            text = "（此 PDF 擷取不到文字也沒有內嵌圖片，可能是掃描影像檔，需要 OCR。）"
        elif not text:
            text = "（此 PDF 沒有文字層，內容以下方擷取到的內嵌圖片呈現。）"

        text, truncated = self._truncate(text)
        note = f"[{p.name}]（PDF，共 {page_count} 頁"
        details = []
        if truncated:
            details.append("文字過長已截斷")
        if image_parts:
            details.append(f"含 {len(image_parts)} 張內嵌圖片")
        if images_skipped:
            details.append(f"另有 {images_skipped} 張圖片因數量/尺寸限制未附上")
        note += ("，" + "；".join(details)) if details else ""
        note += "）"

        parts: list[dict] = [{"type": "input_text", "text": f"{note}\n{text}"}]
        parts.extend(image_parts)
        return parts

    @staticmethod
    def _pdf_image_to_part(img) -> dict | None:
        """把 pypdf 的內嵌圖片物件轉成 input_image；濾掉太小的裝飾圖、過大的縮圖。"""
        import base64
        import io

        try:
            from PIL import Image

            pil_img = Image.open(io.BytesIO(img.data))
            pil_img.load()
        except Exception:
            return None

        width, height = pil_img.size
        if width < MIN_IMAGE_DIMENSION or height < MIN_IMAGE_DIMENSION:
            return None  # 多半是裝飾用小圖示/線條，略過

        if max(width, height) > MAX_IMAGE_DIMENSION:
            scale = MAX_IMAGE_DIMENSION / max(width, height)
            pil_img = pil_img.resize(
                (max(1, round(width * scale)), max(1, round(height * scale))),
                Image.LANCZOS,
            )

        if pil_img.mode not in ("RGB", "L"):
            pil_img = pil_img.convert("RGB")

        buf = io.BytesIO()
        pil_img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        return {"type": "input_image", "image_url": f"data:image/png;base64,{b64}"}

    def _read_text_file(self, p: Path) -> dict:
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = p.read_text(errors="replace")

        text, truncated = self._truncate(text)
        suffix_note = "\n…（內容過長已截斷）" if truncated else ""
        return {"type": "input_text", "text": f"[{p.name}]\n```\n{text}{suffix_note}\n```"}

    def _read_directory(self, p: Path) -> dict:
        import os

        tree_lines = []
        count = 0
        MAX_FILES = 200
        for root, dirs, files in os.walk(p):
            if self.cancel_check():
                tree_lines.append("... (目錄讀取已由使用者中斷) ...")
                break
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            level = str(root).replace(str(p), "").count(os.sep)
            indent = " " * 4 * level
            tree_lines.append(f"{indent}{os.path.basename(root)}/")
            subindent = " " * 4 * (level + 1)
            for f in files:
                if f.startswith("."):
                    continue
                tree_lines.append(f"{subindent}{f}")
                count += 1
            if count > MAX_FILES:
                tree_lines.append(f"{subindent}... (檔案過多，已截斷)")
                break

        tree_str = "\n".join(tree_lines)
        return {"type": "input_text", "text": f"[{p.name} (目錄結構)]\n```\n{tree_str}\n```"}
