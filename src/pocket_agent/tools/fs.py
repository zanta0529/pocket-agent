import datetime
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
            "to read a specific range. Lines are 1-indexed. Large files default to max 500 lines. "
            "If unsure of the path, use list_dir or find_by_name first to avoid errors. "
            "If truncated, do NOT rewrite blindly. Use grep_search to find keywords or use start_line/end_line."
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
        "_is_safe_read": True,
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
        "name": "append_to_file",
        "description": "Append content to the end of an existing file.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative path to the file."},
                "content": {"type": "string", "description": "The content to append."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "type": "function",
        "name": "replace_file_content",
        "description": (
            "Safely replace a specific target string within a file. The target must match EXACTLY "
            "(including whitespace). If allow_multiple is true, all occurrences will be replaced. "
            "If you encounter a mismatch error, DO NOT guess again. "
            "Use read_file with start_line/end_line to capture the exact spaces and indentation first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative path to the file."},
                "target": {"type": "string", "description": "The exact string sequence to replace."},
                "replacement": {"type": "string", "description": "The new content to replace the target with."},
                "allow_multiple": {
                    "type": "boolean",
                    "description": "If true, replaces all occurrences. If false, errors on multiple matches.",
                },
                "start_line": {
                    "type": "integer",
                    "description": "Optional start line to narrow the search scope (1-indexed).",
                },
                "end_line": {
                    "type": "integer",
                    "description": "Optional end line to narrow the search scope (1-indexed).",
                },
            },
            "required": ["path", "target", "replacement"],
        },
    },
    {
        "type": "function",
        "name": "list_dir",
        "description": "List the contents (files and folders) of a directory as a tree structure.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative path to the directory."},
                "depth": {
                    "type": "integer",
                    "description": "Maximum depth of the directory tree to display (default: 2).",
                },
            },
            "required": ["path"],
        },
        "_is_safe_read": True,
    },
    {
        "type": "function",
        "name": "get_file_info",
        "description": "Get basic information about a file, such as size, line count, and last modification time.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Absolute or relative path to the file."}},
            "required": ["path"],
        },
        "_is_safe_read": True,
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
        "_is_safe_read": True,
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
        "_is_safe_read": True,
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
    sensitive_exact = {".git", ".pocket-agent"}
    for part in target.parts:
        if part in sensitive_exact or part.startswith(".env"):
            raise ValueError(f"安全限制：禁止存取敏感目錄或檔案 ({part})")

    # 工作區監禁 (Workspace Jail)
    # workspace_dirs 是使用者明確授權的工作目錄，優先加入；
    # cwd 作為後備，僅在沒有任何 workspace_dirs 時才是唯一基準。
    allowed_roots: list[Path] = []
    if workspace_dirs:
        for wd in workspace_dirs:
            try:
                allowed_roots.append(Path(wd).expanduser().resolve())
            except Exception:
                continue

    # 無論 workspace_dirs 是否存在，cwd 都作為備援允許根
    try:
        allowed_roots.append(Path.cwd().resolve())
    except Exception:
        pass

    is_safe = False
    for root in allowed_roots:
        try:
            target.relative_to(root)
            is_safe = True
            break
        except ValueError:
            pass

    if not is_safe:
        allowed_display = ", ".join(str(r) for r in allowed_roots)
        raise ValueError(
            f"安全限制：路徑 {target} 超出允許的工作區範圍。"
            f"允許的根目錄為：{allowed_display}。"
            "如需存取其他目錄，請使用 /add-dir 指令將其加入工作區。"
        )

    return target


def _get_ignore_spec(target_dir: Path):
    """嘗試讀取目標目錄的 .gitignore 並回傳 pathspec 的 PathSpec 物件。若無則回傳 None。"""
    try:
        import pathspec
    except ImportError:
        return None

    gitignore_path = target_dir / ".gitignore"
    if not gitignore_path.exists():
        # 往上層找
        curr = target_dir
        while curr.parent != curr:
            curr = curr.parent
            if (curr / ".gitignore").exists():
                gitignore_path = curr / ".gitignore"
                break

    if gitignore_path.exists():
        try:
            with open(gitignore_path, "r", encoding="utf-8") as f:
                return pathspec.PathSpec.from_lines(pathspec.patterns.GitWildMatchPattern, f)
        except Exception:
            return None
    return None


def _backup_file(target: Path) -> None:
    """時光機機制：在修改檔案前進行無感備份 (Native I/O)，不觸發 Shell 子行程。"""
    if not target.exists() or not target.is_file():
        return

    try:
        import shutil
        import time

        from pocket_agent.system.paths import user_dir

        backup_dir = user_dir() / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        backup_name = f"{timestamp}_{target.name}"
        backup_path = backup_dir / backup_name

        shutil.copy2(target, backup_path)
        log.info(f"Time Machine: 已備份 {target.name} 至 {backup_path}")

        # 記錄最新的備份映射以便 /undo 指令還原
        mapping_file = backup_dir / "latest_backup_map.json"

        import json

        mapping = {}
        if mapping_file.exists():
            try:
                mapping = json.loads(mapping_file.read_text(encoding="utf-8"))
            except Exception:
                mapping = {}

        mapping[str(target.resolve())] = str(backup_path.resolve())
        mapping_file.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")

    except Exception as e:
        log.warning(f"Time Machine: 備份檔案失敗 ({target}): {e}")


def call_fs_tool(
    name: str, args: dict, on_confirm: Callable[[str], bool] | None, workspace_dirs: list[str] | None = None
) -> str:
    """執行檔案操作工具。"""
    try:
        path_str = args.get("path") or args.get("target_file") or args.get("file") or args.get("target") or ""
        if not path_str:
            return "[ERROR] 缺少必填參數 'path'"

        target = _validate_safe_path(path_str, workspace_dirs)

        if name == "list_dir":
            if not target.exists():
                return f"[ERROR] 目錄不存在: {target}"
            if target.is_file():
                return f"[ERROR] {target} 是一個檔案而非目錄。請改用 'read_file' 工具來讀取內容。"
            if not target.is_dir():
                return f"[ERROR] 路徑不是一個目錄: {target}"

            max_depth = args.get("depth", 2)
            if not isinstance(max_depth, int) or max_depth < 1:
                max_depth = 2

            def build_tree(dir_path: Path, current_depth: int, prefix: str = "") -> list[str]:
                if current_depth > max_depth:
                    return [f"{prefix}└── ... (已達深度限制 {max_depth})"]

                try:
                    entries = list(dir_path.iterdir())
                except Exception as e:
                    return [f"{prefix}└── [ERROR 讀取失敗: {e}]"]

                # 目錄在前，檔案在後，並照名稱排序
                entries.sort(key=lambda x: (not x.is_dir(), x.name.lower()))
                tree_lines = []
                count = len(entries)
                for i, entry in enumerate(entries):
                    is_last = i == count - 1
                    connector = "└── " if is_last else "├── "
                    child_prefix = "    " if is_last else "│   "

                    if entry.is_dir():
                        tree_lines.append(f"{prefix}{connector}[DIR] {entry.name}/")
                        tree_lines.extend(build_tree(entry, current_depth + 1, prefix + child_prefix))
                    else:
                        tree_lines.append(f"{prefix}{connector}{entry.name}")
                return tree_lines

            lines = [f"[DIR] {target.name}/"] + build_tree(target, 1)
            return "\n".join(lines)

        elif name == "get_file_info":
            if not target.exists():
                return f"[ERROR] 檔案或目錄不存在: {target}"

            stat = target.stat()
            size_bytes = stat.st_size
            mtime = datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")

            info = [f"路徑: {target}"]
            if target.is_dir():
                info.append("類型: 目錄")
            else:
                info.append("類型: 一般檔案")
                info.append(f"大小: {size_bytes} Bytes")
                try:
                    with target.open("r", encoding="utf-8") as f:
                        line_count = sum(1 for _ in f)
                    info.append(f"總行數: {line_count}")
                except UnicodeDecodeError:
                    info.append("總行數: (無法讀取，可能為二進位檔案)")
                except Exception as e:
                    info.append(f"總行數: (讀取失敗: {e})")

            info.append(f"最後修改時間: {mtime}")
            return "\n".join(info)

        elif name == "read_file":
            if not target.exists():
                return f"[ERROR] 檔案不存在: {target}"
            if target.is_dir():
                return f"[ERROR] {target} 是一個目錄。請改用 'list_dir' 工具來查看該目錄內容。"
            if not target.is_file():
                return f"[ERROR] 不是一般檔案: {target}"

            content = target.read_text(encoding="utf-8")
            lines = content.splitlines(keepends=True)

            start = args.get("start_line")
            end = args.get("end_line")
            total_lines = len(lines)

            # 預設行為與截斷保護
            if start is None and end is None:
                max_lines = 500
                if total_lines > max_lines:
                    sliced = lines[:max_lines]
                    inner = "\n".join(f"{i + 1:4}| {line.rstrip()}" for i, line in enumerate(sliced))
                    inner += (
                        f"\n\n...（內容過長，共 {total_lines} 行，目前僅顯示前 {max_lines} 行。"
                        "請使用 start_line/end_line 繼續讀取）"
                    )
                    return f"<external_content>\n{inner}\n</external_content>"
                else:
                    inner = "\n".join(f"{i + 1:4}| {line.rstrip()}" for i, line in enumerate(lines))
                    return f"<external_content>\n{inner}\n</external_content>"

            start_idx = max(0, int(start) - 1) if start else 0
            end_idx = min(total_lines, int(end)) if end else total_lines

            sliced = lines[start_idx:end_idx]
            inner = "\n".join(f"{start_idx + i + 1:4}| {line.rstrip()}" for i, line in enumerate(sliced))
            return f"<external_content>\n{inner}\n</external_content>"

        elif name == "write_to_file":
            content = args.get("content", "")
            overwrite = args.get("overwrite", False)

            if target.exists() and not overwrite:
                return f"[ERROR] 檔案 {target} 已存在。如欲覆寫請設定 overwrite=true，或使用 replace_file_content。"

            # 人工確認
            action_desc = f"修改檔案：{target.name}" if target.exists() else f"建立新檔：{target.name}"
            if on_confirm and not on_confirm(action_desc):
                return "[使用者拒絕執行此檔案寫入操作]"

            _backup_file(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return f"[SUCCESS] 成功寫入檔案 {target}"

        elif name == "append_to_file":
            content = args.get("content", "")

            if not target.exists():
                return "[ERROR] 檔案不存在，無法附加。請使用 write_to_file 建立新檔。"

            if on_confirm and not on_confirm(f"附加內容至檔案：{target.name}"):
                return "[使用者拒絕執行此檔案寫入操作]"

            _backup_file(target)
            with target.open("a", encoding="utf-8") as f:
                f.write(content)
            return f"[SUCCESS] 成功附加內容至檔案 {target}"

        elif name == "replace_file_content":
            if not target.exists() or not target.is_file():
                return f"[ERROR] 檔案不存在: {target}"

            target_str = args.get("target", "")
            replacement = args.get("replacement", "")
            start_line = args.get("start_line")
            end_line = args.get("end_line")
            allow_multiple = args.get("allow_multiple", False)

            if not target_str:
                return "[ERROR] 缺少必填參數 'target'"

            content = target.read_text(encoding="utf-8")

            if start_line or end_line:
                lines = content.splitlines(keepends=True)
                total_lines = len(lines)
                start_idx = max(0, int(start_line) - 1) if start_line else 0
                end_idx = min(total_lines, int(end_line)) if end_line else total_lines

                # 範圍外的內容
                prefix_content = "".join(lines[:start_idx])
                target_scope = "".join(lines[start_idx:end_idx])
                suffix_content = "".join(lines[end_idx:])

                count = target_scope.count(target_str)
                if count == 0:
                    return f"[ERROR] 在指定的行數範圍內 ({start_idx + 1}-{end_idx}) 找不到 target 字串。"
                elif count > 1 and not allow_multiple:
                    return (
                        f"[ERROR] 在指定的行數範圍內找到 {count} 處相符的 target 字串，"
                        "請設定 allow_multiple=true 或再縮小範圍以確保唯一性。"
                    )

                new_scope = target_scope.replace(target_str, replacement)
                new_content = prefix_content + new_scope + suffix_content
            else:
                count = content.count(target_str)
                if count == 0:
                    return "[ERROR] 找不到指定的 target 字串。請確保字元與空白完全相符。"
                elif count > 1 and not allow_multiple:
                    return (
                        f"[ERROR] 找到 {count} 處相符的 target 字串。"
                        "請設定 allow_multiple=true、提供更多上下文，或指定 start_line/end_line 確保唯一性。"
                    )
                new_content = content.replace(target_str, replacement)

            # 人工確認
            if on_confirm and not on_confirm(f"局部修改檔案：{target.name}"):
                return "[使用者拒絕執行此檔案替換操作]"

            _backup_file(target)
            target.write_text(new_content, encoding="utf-8")
            return f"[SUCCESS] 成功替換檔案 {target} 的局部內容"

        elif name == "find_by_name":
            if not target.exists() or not target.is_dir():
                return f"[ERROR] 目錄不存在: {target}"
            pattern = args.get("pattern", "")
            if not pattern:
                return "[ERROR] 缺少必填參數 'pattern'"

            results = []
            excludes = {".git", "node_modules", "venv", "__pycache__", ".pocket-agent"}
            sensitive_exact = {".git", ".pocket-agent"}
            spec = _get_ignore_spec(target)

            for p in target.rglob(pattern):
                if any(ex in p.parts for ex in excludes):
                    continue
                if any(part.startswith(".env") or part in sensitive_exact for part in p.parts):
                    continue

                if p.is_symlink():
                    try:
                        resolved = p.resolve(strict=False)
                        _validate_safe_path(str(resolved), workspace_dirs)
                    except Exception:
                        continue

                rel_path = str(p.relative_to(target))
                if spec and spec.match_file(rel_path):
                    continue

                results.append(rel_path)
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
            sensitive_exact = {".git", ".pocket-agent"}
            spec = _get_ignore_spec(target)

            for p in target.rglob("*"):
                if any(part.startswith(".env") or part in sensitive_exact for part in p.parts):
                    continue

                if p.is_symlink():
                    try:
                        resolved = p.resolve(strict=False)
                        _validate_safe_path(str(resolved), workspace_dirs)
                    except Exception:
                        continue

                if not p.is_file() or any(ex in p.parts for ex in excludes):
                    continue

                rel_path = str(p.relative_to(target))
                if spec and spec.match_file(rel_path):
                    continue

                # DoS 防護：略過超過 5 MB 的大檔
                try:
                    if p.stat().st_size > 5 * 1024 * 1024:
                        continue
                except Exception:
                    continue

                try:
                    lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
                    for i, line in enumerate(lines):
                        if query in line:
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

    except ValueError as e:
        log.warning(f"FS Tool {name} 執行失敗: {e}")
        return f"[ERROR] 檔案操作失敗: {e}"
    except Exception as e:
        log.exception(f"FS Tool {name} 執行發生未預期錯誤")
        return f"[ERROR] 檔案操作發生未預期錯誤: {e}"
