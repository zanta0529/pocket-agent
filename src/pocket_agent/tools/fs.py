import logging
from pathlib import Path
from typing import Callable

log = logging.getLogger("pocket-agent")

FS_TOOLS = [
    {
        "type": "function",
        "name": "read_file",
        "description": (
            "Read the contents of a file. Optionally provide start_line and end_line "
            "to read a specific range. Lines are 1-indexed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative path to the file."},
                "start_line": {"type": "integer", "description": "Start line number (inclusive)."},
                "end_line": {"type": "integer", "description": "End line number (inclusive)."},
            },
            "required": ["path"],
        },
    },
    {
        "type": "function",
        "name": "write_to_file",
        "description": "Create a new file or completely overwrite an existing one with new content.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative path to the file."},
                "content": {"type": "string", "description": "The full content to write."},
                "overwrite": {
                    "type": "boolean",
                    "description": "Must be true if you intend to overwrite an existing file.",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "type": "function",
        "name": "replace_file_content",
        "description": (
            "Safely replace a specific target string within a file. The target must match EXACTLY "
            "(including whitespace) and should be unique within the file."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative path to the file."},
                "target": {"type": "string", "description": "The exact string sequence to replace."},
                "replacement": {"type": "string", "description": "The new content to replace the target with."},
            },
            "required": ["path", "target", "replacement"],
        },
    },
    {
        "type": "function",
        "name": "list_dir",
        "description": "List the contents (files and folders) of a directory.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Absolute or relative path to the directory."}},
            "required": ["path"],
        },
    },
    {
        "type": "function",
        "name": "find_by_name",
        "description": "Recursively search for files matching a glob pattern (e.g., '*.py') in a directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory to search in."},
                "pattern": {"type": "string", "description": "Glob pattern to match file names."},
            },
            "required": ["path", "pattern"],
        },
    },
    {
        "type": "function",
        "name": "grep_search",
        "description": "Search for a specific text query within all files in a directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory to search in."},
                "query": {"type": "string", "description": "Text query to search for."},
            },
            "required": ["path", "query"],
        },
    },
]


def is_fs_tool(name: str) -> bool:
    return any(t["name"] == name for t in FS_TOOLS)


def _validate_safe_path(requested_path: str, workspace_dirs: list[str] | None = None) -> Path:
    """解析路徑並驗證是否在安全的工作區內，同時阻擋敏感檔案。"""
    try:
        # 使用 strict=False 允許解析尚不存在的檔案路徑
        target = Path(requested_path).expanduser().resolve(strict=False)
    except Exception as e:
        raise ValueError(f"無效的路徑格式: {e}")

    # 黑名單：防止存取敏感設定或版本控制資料
    sensitive_names = {".env", ".git", ".pocket-agent"}
    for part in target.parts:
        if part in sensitive_names:
            raise ValueError(f"安全限制：禁止存取敏感目錄或檔案 ({part})")

    # 工作區監禁 (Workspace Jail)
    allowed_roots = [Path.cwd().resolve()]
    if workspace_dirs:
        for wd in workspace_dirs:
            try:
                allowed_roots.append(Path(wd).expanduser().resolve())
            except Exception:
                continue

    is_safe = False
    for root in allowed_roots:
        try:
            target.relative_to(root)
            is_safe = True
            break
        except ValueError:
            pass

    if not is_safe:
        raise ValueError(f"安全限制：路徑 {target} 超出允許的工作區範圍")

    return target


def call_fs_tool(
    name: str, args: dict, on_confirm: Callable[[str], bool] | None, workspace_dirs: list[str] | None = None
) -> str:
    """執行檔案操作工具。"""
    try:
        path_str = args.get("path", "")
        if not path_str:
            return "[ERROR] 缺少必填參數 'path'"

        target = _validate_safe_path(path_str, workspace_dirs)

        if name == "list_dir":
            if not target.exists():
                return f"[ERROR] 目錄不存在: {target}"
            if not target.is_dir():
                return f"[ERROR] 路徑不是一個目錄: {target}"

            items = []
            for item in target.iterdir():
                kind = "DIR" if item.is_dir() else "FILE"
                items.append(f"[{kind}] {item.name}")
            if not items:
                return f"目錄 {target} 為空。"
            return "\n".join(items)

        elif name == "read_file":
            if not target.exists() or not target.is_file():
                return f"[ERROR] 檔案不存在或不是一般檔案: {target}"

            content = target.read_text(encoding="utf-8")
            lines = content.splitlines(keepends=True)

            start = args.get("start_line")
            end = args.get("end_line")

            # 若未指定範圍，回傳整個檔案並附帶行號
            if start is None and end is None:
                return "\n".join(f"{i + 1:4}| {line.rstrip()}" for i, line in enumerate(lines))

            start_idx = max(0, int(start) - 1) if start else 0
            end_idx = min(len(lines), int(end)) if end else len(lines)

            sliced = lines[start_idx:end_idx]
            return "\n".join(f"{start_idx + i + 1:4}| {line.rstrip()}" for i, line in enumerate(sliced))

        elif name == "write_to_file":
            content = args.get("content", "")
            overwrite = args.get("overwrite", False)

            if target.exists() and not overwrite:
                return f"[ERROR] 檔案 {target} 已存在。如欲覆寫請設定 overwrite=true，或使用 replace_file_content。"

            # 人工確認
            action_desc = f"修改檔案：{target.name}" if target.exists() else f"建立新檔：{target.name}"
            if on_confirm and not on_confirm(action_desc):
                return "[使用者拒絕執行此檔案寫入操作]"

            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return f"[SUCCESS] 成功寫入檔案 {target}"

        elif name == "replace_file_content":
            if not target.exists() or not target.is_file():
                return f"[ERROR] 檔案不存在: {target}"

            target_str = args.get("target", "")
            replacement = args.get("replacement", "")

            if not target_str:
                return "[ERROR] 缺少必填參數 'target'"

            content = target.read_text(encoding="utf-8")

            count = content.count(target_str)
            if count == 0:
                return "[ERROR] 找不到指定的 target 字串。請確保字元與空白完全相符。"
            elif count > 1:
                return f"[ERROR] 找到 {count} 處相符的 target 字串。請提供更多上下文以確保唯一性。"

            # 人工確認
            if on_confirm and not on_confirm(f"局部修改檔案：{target.name}"):
                return "[使用者拒絕執行此檔案替換操作]"

            new_content = content.replace(target_str, replacement)
            target.write_text(new_content, encoding="utf-8")
            return f"[SUCCESS] 成功替換檔案 {target} 的局部內容"

        elif name == "find_by_name":
            if not target.exists() or not target.is_dir():
                return f"[ERROR] 目錄不存在: {target}"
            pattern = args.get("pattern", "")
            if not pattern:
                return "[ERROR] 缺少必填參數 'pattern'"

            results = []
            # 排除常見不相關的目錄
            excludes = {".git", "node_modules", "venv", "__pycache__", ".pocket-agent"}
            for p in target.rglob(pattern):
                if any(ex in p.parts for ex in excludes):
                    continue
                results.append(str(p.relative_to(target)))
                if len(results) >= 50:
                    results.append("... (結果過多，僅顯示前 50 筆)")
                    break

            if not results:
                return f"找不到符合 {pattern} 的檔案。"
            return "搜尋結果：\n" + "\n".join(results)

        elif name == "grep_search":
            if not target.exists() or not target.is_dir():
                return f"[ERROR] 目錄不存在: {target}"
            query = args.get("query", "")
            if not query:
                return "[ERROR] 缺少必填參數 'query'"

            results = []
            excludes = {".git", "node_modules", "venv", "__pycache__", ".pocket-agent"}
            # 遍歷所有檔案 (僅限一般檔案，且避開排除名單)
            for p in target.rglob("*"):
                if not p.is_file() or any(ex in p.parts for ex in excludes):
                    continue
                try:
                    lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
                    for i, line in enumerate(lines):
                        if query in line:
                            rel_path = p.relative_to(target)
                            results.append(f"{rel_path}:{i + 1}: {line.strip()}")
                            if len(results) >= 100:
                                results.append("... (結果過多，僅顯示前 100 筆)")
                                return "搜尋結果：\n" + "\n".join(results)
                except Exception:
                    continue

            if not results:
                return f"找不到包含 '{query}' 的內容。"
            return "搜尋結果：\n" + "\n".join(results)

        return f"[ERROR] 未知的工具名稱: {name}"

    except Exception as e:
        log.exception(f"FS Tool {name} 執行失敗")
        return f"[ERROR] 檔案操作失敗: {e}"
