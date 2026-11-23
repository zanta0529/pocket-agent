import logging
from collections.abc import Callable
from pathlib import Path

from pocket_agent.core.cancellation import OperationCancelled, check_cancelled
from pocket_agent.system.constants import (
    DEFAULT_TOOL_OUTPUT_CHARS,
    MAX_READ_FILE_BYTES,
    TOOL_NOT_FOUND_PREFIX,
)
from pocket_agent.system.i18n import t
from pocket_agent.tools.fs import validate_safe_path, wrap_external_content

log = logging.getLogger("pocket-agent")

DOCUMENT_TOOLS = [
    {
        "type": "function",
        "name": "read_document",
        "description": "Read and extract text from binary document files (.pdf, .docx, .pptx).",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative path to the document file."},
            },
            "required": ["path"],
        },
        "_is_safe_read": True,
    }
]


def is_document_tool(name: str) -> bool:
    return any(tool["name"] == name for tool in DOCUMENT_TOOLS)


def _read_pdf(path: Path) -> str:
    check_cancelled()
    # 優先嘗試使用 pymupdf4llm (效果最佳，支援表格與 Markdown)，失敗時降級至 pypdf
    try:
        import pymupdf4llm  # type: ignore

        return pymupdf4llm.to_markdown(str(path))
    except OperationCancelled:
        raise
    except ImportError:
        pass
    except Exception as e:
        log.debug("pymupdf4llm 解析失敗，降級至 pypdf (%s): %s", path, e)

    try:
        from pypdf import PdfReader
    except ImportError:
        return f"[ERROR] {t('document_tool.missing_pdf_deps')}"

    reader = PdfReader(str(path))
    content: list[str] = []
    for i, page in enumerate(reader.pages):
        check_cancelled()
        content.append(t("document_tool.page_header", page=i + 1))
        text = page.extract_text()
        if text:
            content.append(text.strip())
    return "\n".join(content)


def _read_docx(path: Path) -> str:
    try:
        from docx import Document
    except ImportError:
        return f"[ERROR] {t('document_tool.missing_docx_dep')}"

    check_cancelled()
    doc = Document(str(path))
    content: list[str] = []
    for p in doc.paragraphs:
        check_cancelled()
        if p.text.strip():
            content.append(p.text.strip())

    # 抽取表格文字
    for i, table in enumerate(doc.tables):
        check_cancelled()
        content.append(t("document_tool.table_header", index=i + 1))
        for row in table.rows:
            row_data = [cell.text.strip().replace("\n", " ") for cell in row.cells]
            content.append(" | ".join(row_data))

    return "\n".join(content)


def _read_pptx(path: Path) -> str:
    try:
        from pptx import Presentation
    except ImportError:
        return f"[ERROR] {t('document_tool.missing_pptx_dep')}"

    check_cancelled()
    prs = Presentation(str(path))
    content: list[str] = []
    for i, slide in enumerate(prs.slides):
        check_cancelled()
        content.append(t("document_tool.slide_header", index=i + 1))
        for shape in slide.shapes:
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    row_data = [cell.text.strip().replace("\n", " ") for cell in row.cells]
                    content.append(" | ".join(row_data))
            elif hasattr(shape, "text") and shape.text.strip():
                content.append(shape.text.strip())
        if getattr(slide, "has_notes_slide", False):
            notes_frame = getattr(slide.notes_slide, "notes_text_frame", None)
            notes_text = getattr(notes_frame, "text", "") if notes_frame is not None else ""
            if notes_text and notes_text.strip():
                content.append(f"{t('document_tool.notes_header')}\n{notes_text.strip()}")
    return "\n".join(content)


_READERS: dict[str, Callable[[Path], str]] = {
    ".pdf": _read_pdf,
    ".docx": _read_docx,
    ".pptx": _read_pptx,
}


def call_document_tool(name: str, args: dict, workspace_dirs: list[str] | None = None) -> str:
    if name != "read_document":
        return f"[ERROR] {t('document_tool.unknown_tool', name=name)}"

    path_str = args.get("path")
    if not path_str:
        return f"[ERROR] {t('document_tool.missing_path')}"

    # 驗證路徑與套用工作區監禁 (Workspace Jail)
    try:
        target = validate_safe_path(str(path_str), workspace_dirs)
    except Exception as e:
        return f"[ERROR] {e}"

    if not target.exists():
        return f"{TOOL_NOT_FOUND_PREFIX} {t('fs_tool.file_not_found', path=path_str)}"
    if not target.is_file():
        return f"[ERROR] {t('document_tool.not_regular_file', path=target)}"

    try:
        fsize = target.stat().st_size
        if isinstance(fsize, int) and fsize > MAX_READ_FILE_BYTES:
            limit_mb = MAX_READ_FILE_BYTES // (1024 * 1024)
            return f"[ERROR] {t('document_tool.file_too_large', size=fsize, limit_mb=limit_mb)}"
    except OSError:
        pass

    ext = target.suffix.lower()
    reader_fn = _READERS.get(ext)
    if reader_fn is None:
        return f"[ERROR] {t('document_tool.unsupported_format', ext=ext)}"

    try:
        res = reader_fn(target)
        if res.startswith("[ERROR]"):
            return res
        if len(res) > DEFAULT_TOOL_OUTPUT_CHARS:
            res = res[:DEFAULT_TOOL_OUTPUT_CHARS] + t(
                "document_tool.truncated_notice",
                max_chars=DEFAULT_TOOL_OUTPUT_CHARS,
            )
        return wrap_external_content(res)
    except OperationCancelled:
        raise
    except Exception as e:
        log.exception("讀取文件 %s 失敗", target)
        return f"[ERROR] {t('document_tool.read_failed', error=e)}"
