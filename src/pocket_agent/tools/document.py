import logging
from pathlib import Path

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
    }
]


def is_document_tool(name: str) -> bool:
    return any(t["name"] == name for t in DOCUMENT_TOOLS)


def call_document_tool(name: str, args: dict, workspace_dirs: list[str] | None = None) -> str:
    if name != "read_document":
        return f"[ERROR] 未知的工具名稱: {name}"

    path_str = args.get("path")
    if not path_str:
        return "[ERROR] 缺少必填參數 'path'"

    # 驗證路徑與套用工作區監禁 (Workspace Jail)
    try:
        from pocket_agent.tools.fs import _validate_safe_path, wrap_external_content

        target = _validate_safe_path(path_str, workspace_dirs)
    except Exception as e:
        return f"[ERROR] {e}"

    if not target.exists() or not target.is_file():
        return f"[ERROR] 檔案不存在或不是一般檔案: {target}"

    ext = target.suffix.lower()
    try:
        if ext == ".pdf":
            res = _read_pdf(target)
        elif ext == ".docx":
            res = _read_docx(target)
        elif ext == ".pptx":
            res = _read_pptx(target)
        else:
            return (
                f"[ERROR] 檔案格式不支援。read_document 僅支援 .pdf, .docx, .pptx，"
                f"目前副檔名為 {ext}。如果是純文字檔，請改用 read_file。"
            )
        if res.startswith("[ERROR]"):
            return res
        return wrap_external_content(res)
    except Exception as e:
        log.exception(f"讀取文件 {target} 失敗")
        return f"[ERROR] 讀取文件失敗: {e}"


def _read_pdf(path: Path) -> str:
    # 優先嘗試使用 pymupdf4llm (效果最佳，支援表格與 Markdown)
    try:
        import pymupdf4llm  # type: ignore

        return pymupdf4llm.to_markdown(str(path))
    except ImportError:
        pass

    # 備案：使用 pypdf
    try:
        from pypdf import PdfReader
    except ImportError:
        return (
            "[ERROR] 尚未安裝 pymupdf4llm 或 pypdf 套件。為了獲得最佳解析效果（包含表格），"
            "強烈建議使用 run_command 執行： pip install pymupdf4llm"
        )

    reader = PdfReader(str(path))
    content = []
    for i, page in enumerate(reader.pages):
        content.append(f"--- 第 {i + 1} 頁 ---")
        text = page.extract_text()
        if text:
            content.append(text.strip())
    return "\n".join(content)


def _read_docx(path: Path) -> str:
    try:
        from docx import Document
    except ImportError:
        return "[ERROR] 尚未安裝 python-docx 套件，請使用 run_command 執行 pip install python-docx"

    doc = Document(str(path))
    content = []
    for p in doc.paragraphs:
        if p.text.strip():
            content.append(p.text.strip())

    # 嘗試抽取表格文字
    for i, table in enumerate(doc.tables):
        content.append(f"\n--- 表格 {i + 1} ---")
        for row in table.rows:
            row_data = [cell.text.strip().replace("\n", " ") for cell in row.cells]
            content.append(" | ".join(row_data))

    return "\n".join(content)


def _read_pptx(path: Path) -> str:
    try:
        from pptx import Presentation
    except ImportError:
        return "[ERROR] 尚未安裝 python-pptx 套件，請使用 run_command 執行 pip install python-pptx"

    prs = Presentation(str(path))
    content = []
    for i, slide in enumerate(prs.slides):
        content.append(f"\n--- 投影片 {i + 1} ---")
        for shape in slide.shapes:
            if hasattr(shape, "text") and shape.text.strip():
                content.append(shape.text.strip())
    return "\n".join(content)
