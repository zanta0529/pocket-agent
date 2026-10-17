import datetime
import logging
import re
from pathlib import Path
from typing import Callable

log = logging.getLogger("pocket-agent")

_EXTERNAL_TAG_RE = re.compile(r"<\s*/?\s*external_content(?:\s+[^>]*)?\s*>", re.IGNORECASE)


def wrap_external_content(text: str, source: str | None = None) -> str:
    """將外部不可信內容包裹於 <external_content> 標籤中，並跳脫內嵌的偽造標籤。"""
    safe_text = _EXTERNAL_TAG_RE.sub("[escaped_tag]", text)
    open_tag = f'<external_content source="{source}">' if source else "<external_content>"
    return f"{open_tag}\n{safe_text}\n</external_content>"


FS_TOOLS = [
    {
        "type": "function",
        "name": "read_file",
        "description": (
            "Read the contents of a file. Optionally provide start_line and end_line "
            "to read a specific range. "
            "WARNING: Reading entire large files may cause context overflow. "
            "Always prefer using grep_search first, "
            "or use start_line/end_line to read only the necessary chunks. "
            "If truncated, do NOT rewrite blindly."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative path to the file."},
                "start_line": {"type": "integer", "description": "Start line number (inclusive)."},
                "end_line": {"type": "integer", "description": "End line number (inclusive)."},
                "char_offset": {
                    "type": "integer",
                    "description": "Optional character offset within start_line to continue reading long lines.",
                },
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
        "name": "search_files",
        "description": (
            "Search for files by name/pattern and/or text content in a directory. "
            "Use this for exploring codebases, reading documentation, or finding files. "
            "If only file_pattern is provided, it returns matching file names. "
            "If query is provided, it performs a full-text search, optionally filtered by file_pattern."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Directory or specific file to search in. Defaults to the current working directory (.)."
                    ),
                },
                "query": {
                    "type": "string",
                    "description": "Text query to search for inside files. Leave empty to only search by file name.",
                },
                "file_pattern": {
                    "type": "string",
                    "description": "Glob pattern to filter files (e.g., '*.py', '*.md').",
                },
            },
            "required": [],
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

    try:
        from pocket_agent.system.paths import user_dir

        global_user_dir: Path | None = user_dir().resolve()
    except Exception:
        global_user_dir = None

    # 黑名單：防止存取敏感設定或版本控制資料
    is_in_global = global_user_dir is not None and target.is_relative_to(global_user_dir)

    # 全域目錄 (~/.pocket-agent) 中僅允許 workspaces, scratch, backups, skills 等沙盒與產物
    # 根目錄下的系統設定檔 (如 allowlist.json, mcp_config.yaml, config.json) 均屬敏感保護範圍
    if is_in_global and global_user_dir is not None:
        rel = target.relative_to(global_user_dir)
        allowed_global_subdirs = {"workspaces", "scratch", "backups", "skills"}
        if len(rel.parts) == 0 or rel.parts[0] not in allowed_global_subdirs:
            raise ValueError("安全限制：禁止存取敏感目錄或檔案 (.pocket-agent)")

    for part in target.parts:
        if part == ".git" or part.startswith(".env"):
            raise ValueError(f"安全限制：禁止存取敏感目錄或檔案 ({part})")
        # 僅當目標不在合法全域使用者目錄內時，才阻擋名為 .pocket-agent 的目錄
        if part == ".pocket-agent" and not is_in_global:
            raise ValueError(f"安全限制：禁止存取敏感目錄或檔案 ({part})")

    # 工作區監禁 (Workspace Jail)
    # 將啟動目錄 (CWD) 與全域使用者目錄 (user_dir) 作為基礎工作區無條件加入
    allowed_roots: list[Path] = []
    try:
        allowed_roots.append(Path.cwd().resolve())
    except Exception:
        pass

    if global_user_dir is not None:
        allowed_roots.append(global_user_dir)

    # workspace_dirs 是使用者明確授權的額外工作目錄，疊加進去
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
                    raw_entries = list(dir_path.iterdir())
                except Exception as e:
                    return [f"{prefix}└── [ERROR 讀取失敗: {e}]"]

                entries: list[Path] = []
                for entry in raw_entries:
                    if entry.name == ".git" or entry.name.startswith(".env"):
                        continue
                    if entry.is_symlink():
                        try:
                            resolved_link = entry.resolve(strict=False)
                            _validate_safe_path(str(resolved_link), workspace_dirs)
                        except Exception:
                            continue
                    entries.append(entry)

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

            MAX_READ_FILE_BYTES = 10 * 1024 * 1024  # 10 MB DoS 防護上限
            try:
                fsize = target.stat().st_size
                if isinstance(fsize, int) and fsize > MAX_READ_FILE_BYTES:
                    return (
                        f"[ERROR] 檔案過大 ({fsize:,} Bytes，超過 10 MB 安全上限)。"
                        "請改用 `search_files` 進行關鍵字檢索。"
                    )
            except OSError:
                pass

            start = args.get("start_line")
            end = args.get("end_line")
            char_offset = args.get("char_offset")
            char_offset = max(0, int(char_offset)) if char_offset is not None else 0

            MAX_READ_LINES = 500
            MAX_READ_CHARS = 12000

            start_idx = max(0, int(start) - 1) if start else 0
            req_end_idx = int(end) if end else (MAX_READ_LINES if start is None else None)

            sliced: list[str] = []
            total_lines = 0
            with target.open("r", encoding="utf-8") as f:
                for idx, line in enumerate(f):
                    total_lines += 1
                    if idx >= start_idx and (req_end_idx is None or idx < req_end_idx):
                        sliced.append(line)

            # 逐行組合並檢查字元數上限
            formatted_lines = []
            chars_count = 0
            char_truncated = False
            is_single_line_cut = False
            cut_end_char = 0
            last_rendered_line = start_idx

            for i, line in enumerate(sliced):
                current_line_no = start_idx + i + 1
                stripped = line.rstrip()
                if i == 0 and char_offset > 0:
                    stripped = stripped[char_offset:]
                    line_prefix = f"{current_line_no:4}| [+{char_offset}] "
                else:
                    line_prefix = f"{current_line_no:4}| "

                avail_chars = MAX_READ_CHARS - chars_count - len(line_prefix) - 1

                if avail_chars <= 0:
                    char_truncated = True
                    break

                if len(stripped) > avail_chars:
                    line_str = line_prefix + stripped[:avail_chars]
                    formatted_lines.append(line_str)
                    chars_count += len(line_str) + 1
                    last_rendered_line = current_line_no
                    char_truncated = True
                    is_single_line_cut = True
                    cut_end_char = (char_offset if i == 0 else 0) + avail_chars
                    break

                line_str = line_prefix + stripped
                formatted_lines.append(line_str)
                chars_count += len(line_str) + 1
                last_rendered_line = current_line_no

            inner = "\n".join(formatted_lines)

            # 截斷提示
            notice_parts = []
            if char_truncated:
                notice_parts.append(
                    f"...（內容過長，已達單次讀取上限 {MAX_READ_CHARS} 字元，"
                    f"僅呈現至第 {last_rendered_line} 行 / 共 {total_lines} 行。）"
                )
                if is_single_line_cut:
                    notice_parts.append(
                        f"[系統提示] 第 {last_rendered_line} 行為超長單行，已被字元截斷。"
                        f"若要閱讀該行剩餘內容，請設定 `start_line={last_rendered_line}`, "
                        f"`char_offset={cut_end_char}`；"
                        f"若要閱讀後續行，可設定 `start_line={last_rendered_line + 1}`；"
                        "若要尋找特定字串，請優先改用 `search_files` 進行精準檢索。"
                    )
                else:
                    notice_parts.append(
                        f"[系統建議] 若要繼續閱讀後續內容，請呼叫 `read_file` 並設定 "
                        f"`start_line={last_rendered_line + 1}`；"
                        "若要尋找特定字串，請改用 `search_files` 進行精準檢索。"
                    )
            elif start is None and end is None and total_lines > MAX_READ_LINES:
                notice_parts.append(f"...（內容過長，共 {total_lines} 行，已自動截斷前 {MAX_READ_LINES} 行。）")
                notice_parts.append(
                    "[系統建議] 若要繼續閱讀後續內容，請呼叫 `read_file` 並設定 "
                    f"`start_line={MAX_READ_LINES + 1}`；"
                    "若要尋找特定字串，請改用 `search_files` 進行精準檢索。"
                )

            if notice_parts:
                inner += "\n\n" + "\n".join(notice_parts)

            return wrap_external_content(inner)

        elif name == "write_to_file":
            content = args.get("content", "")
            overwrite = args.get("overwrite", False)

            if target.exists() and not overwrite:
                return f"[ERROR] 檔案 {target} 已存在。如欲覆寫請設定 overwrite=true，或使用 replace_file_content。"

            # 人工確認
            action_desc = f"修改檔案：{target.name}" if target.exists() else f"建立新檔：{target.name}"
            if on_confirm is None:
                return "[ERROR] 缺乏使用者確認機制，拒絕執行寫入操作"
            if not on_confirm(action_desc):
                return "[使用者拒絕執行此檔案寫入操作]"

            _backup_file(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return f"[SUCCESS] 成功寫入檔案 {target}"

        elif name == "append_to_file":
            content = args.get("content", "")

            if not target.exists():
                return "[ERROR] 檔案不存在，無法附加。請使用 write_to_file 建立新檔。"

            if on_confirm is None:
                return "[ERROR] 缺乏使用者確認機制，拒絕執行寫入操作"
            if not on_confirm(f"附加內容至檔案：{target.name}"):
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
            if on_confirm is None:
                return "[ERROR] 缺乏使用者確認機制，拒絕執行替換操作"
            if not on_confirm(f"局部修改檔案：{target.name}"):
                return "[使用者拒絕執行此檔案替換操作]"

            _backup_file(target)
            target.write_text(new_content, encoding="utf-8")
            return f"[SUCCESS] 成功替換檔案 {target} 的局部內容"

        elif name == "search_files":
            if not target.exists():
                return f"[ERROR] 目錄或檔案不存在: {target}"

            query = args.get("query", "")
            file_pattern = args.get("file_pattern", "")

            results = []
            excludes = {".git", "node_modules", "venv", "__pycache__"}
            spec = _get_ignore_spec(target) if target.is_dir() else None

            try:
                from pocket_agent.system.paths import user_dir

                global_user_dir: Path | None = user_dir().resolve()
            except Exception:
                global_user_dir = None

            # 決定搜尋範圍
            if target.is_file():
                paths_to_search = [target]
            else:
                glob_pattern = file_pattern if file_pattern else "*"
                paths_to_search = target.rglob(glob_pattern)

            for p in paths_to_search:
                p_in_global = global_user_dir is not None and p.is_relative_to(global_user_dir)
                if any(part.startswith(".env") or part == ".git" for part in p.parts):
                    continue
                if p_in_global:
                    rel_g = p.relative_to(global_user_dir)
                    allowed_global_subdirs = {"workspaces", "scratch", "backups", "skills"}
                    if len(rel_g.parts) == 0 or rel_g.parts[0] not in allowed_global_subdirs:
                        continue
                elif any(part == ".pocket-agent" for part in p.parts):
                    continue

                if p.is_symlink():
                    try:
                        resolved = p.resolve(strict=False)
                        _validate_safe_path(str(resolved), workspace_dirs)
                    except Exception:
                        continue

                if not p.is_file() or any(ex in p.parts for ex in excludes):
                    continue

                if target.is_file():
                    rel_path = target.name
                else:
                    rel_path = str(p.relative_to(target))
                    if spec and spec.match_file(rel_path):
                        continue

                # 如果沒有給 query，就只是搜檔名
                if not query:
                    results.append(rel_path)
                    if len(results) >= 100:
                        results.append(
                            "... (結果過多，已自動截斷前 100 筆)\n"
                            "[系統建議] 若要尋找特定內容，請利用 search_files 設定 `query`，\n"
                            "或利用 `file_pattern` 限縮範圍。"
                        )
                        break
                    continue

                # 如果有 query，進行全文檢索
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
                                results.append(
                                    "... (結果過多，已自動截斷前 100 筆)\n"
                                    "[系統建議] 若要繼續閱讀特定檔案，"
                                    "請使用 `read_file` 並設定 `start_line`/`end_line`；\n"
                                    "若要精準搜尋，請在 `search_files` 設定更精確的 `query` 或 `file_pattern`。"
                                )
                                return "搜尋結果：\n" + "\n".join(results)
                except Exception:
                    continue

            if not results:
                if query:
                    return f"找不到包含 '{query}' 的內容。"
                else:
                    return f"找不到符合 {file_pattern} 的檔案。"
            return "搜尋結果：\n" + "\n".join(results)

        return f"[ERROR] 未知的工具名稱: {name}"

    except ValueError as e:
        log.warning(f"FS Tool {name} 執行失敗: {e}")
        return f"[ERROR] 檔案操作失敗: {e}"
    except Exception as e:
        log.exception(f"FS Tool {name} 執行發生未預期錯誤")
        return f"[ERROR] 檔案操作發生未預期錯誤: {e}"
