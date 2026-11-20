import datetime
import hashlib
import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from pocket_agent.core.cancellation import check_cancelled
from pocket_agent.system.constants import (
    DEFAULT_READ_FILE_LINES,
    DEFAULT_SEARCH_MAX_RESULTS,
    DEFAULT_TOOL_OUTPUT_CHARS,
    MAX_FILE_SEARCH_SIZE_BYTES,
    MAX_READ_FILE_BYTES,
    REPLACE_DIAGNOSTIC_MAX_MATCHES,
    REPLACE_SCOPE_DRIFT_LINES,
    TOOL_NOT_FOUND_PREFIX,
    TOOL_OVERWRITE_REQUIRED_PREFIX,
    TOOL_TARGET_NOT_FOUND_PREFIX,
    USER_DATA_DIR_NAME,
)
from pocket_agent.system.i18n import t
from pocket_agent.system.paths import format_display_path

log = logging.getLogger("pocket-agent")

# 向後相容別名 (對齊 constants.py)
MAX_READ_LINES = DEFAULT_READ_FILE_LINES
MAX_READ_CHARS = DEFAULT_TOOL_OUTPUT_CHARS
SEARCH_MAX_RESULTS = DEFAULT_SEARCH_MAX_RESULTS

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
        "description": (
            "Create a new file or completely overwrite an existing file with new content. "
            "For partial edits to an existing file, prefer replace_file_content. "
            "When overwriting an existing file, set overwrite=true "
            "(required in Accept-edits mode; in manual confirmation mode, "
            "the user will be prompted directly to confirm overwriting)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative path to the file."},
                "content": {"type": "string", "description": "The full content to write."},
                "overwrite": {
                    "type": "boolean",
                    "description": (
                        "Set to true to overwrite an existing file in full "
                        "(required when overwriting in Accept-edits mode)."
                    ),
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


SCRATCH_TOOLS = [
    {
        "type": "function",
        "name": "write_scratch_note",
        "description": (
            "Write or append research notes to the isolated sandbox scratch directory "
            "(never modifies workspace files and requires no confirmation)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Relative filename under scratch/notes/ (e.g., 'research_123.md').",
                },
                "content": {
                    "type": "string",
                    "description": "Markdown or plain text note content.",
                },
                "append": {
                    "type": "boolean",
                    "description": "If true, append to existing note instead of overwriting.",
                },
            },
            "required": ["filename", "content"],
        },
    }
]


def is_scratch_tool(name: str) -> bool:
    return any(t["name"] == name for t in SCRATCH_TOOLS)


def _resolve_scratch_note_path(filename: str) -> Path:
    """解析並驗證沙盒筆記路徑，強制隔離於 scratch_dir()/notes 之下。"""
    from pocket_agent.system.paths import scratch_dir

    cleaned = str(filename or "").strip().strip("'\"")
    if not cleaned:
        raise ValueError("filename 不可為空")
    rel = Path(cleaned)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError("安全限制：僅允許相對於沙盒筆記目錄的檔案路徑")
    for part in rel.parts:
        if part.lower().startswith(".env") or part.lower() == ".git":
            raise ValueError(f"安全限制：禁止使用敏感檔名 ({part})")

    notes_root = (scratch_dir() / "notes").resolve(strict=False)
    target = (notes_root / rel).resolve(strict=False)
    if not target.is_relative_to(notes_root) or target == notes_root:
        raise ValueError("安全限制：路徑超出沙盒筆記目錄範圍")
    return target


def call_scratch_tool(name: str, args: dict) -> str:
    """執行沙盒專用筆記工具（僅寫入 scratch_dir()/notes，不碰觸工作區）。"""
    if name != "write_scratch_note":
        return f"[ERROR] 未知的沙盒工具名稱: {name}"
    try:
        filename = args.get("filename", "")
        content = args.get("content")
        if content is None:
            return "[ERROR] 缺少必填參數: content"
        append = bool(args.get("append", False))
        target = _resolve_scratch_note_path(str(filename))
        target.parent.mkdir(parents=True, exist_ok=True)
        text = str(content)
        if append and target.exists():
            with open(target, "a", encoding="utf-8") as f:
                f.write(text)
            return f"[SUCCESS] 已附加沙盒研究筆記至：{target}"
        target.write_text(text, encoding="utf-8")
        return f"[SUCCESS] 已將沙盒研究筆記寫入：{target}"
    except ValueError as e:
        log.warning("Scratch Tool %s 執行失敗: %s", name, e)
        return f"[ERROR] 沙盒筆記寫入失敗: {e}"
    except Exception as e:
        log.exception("Scratch Tool %s 執行發生未預期錯誤", name)
        return f"[ERROR] 沙盒筆記寫入發生未預期錯誤: {e}"


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
            raise ValueError(t("fs_tool.sensitive_path_blocked", part=USER_DATA_DIR_NAME))

    user_dir_name_lower = USER_DATA_DIR_NAME.lower()
    parts_lower = [p.lower() for p in target.parts]
    for idx, part in enumerate(target.parts):
        lower_part = parts_lower[idx]
        if lower_part == ".git" or lower_part.startswith(".env"):
            raise ValueError(t("fs_tool.sensitive_path_blocked", part=part))
        # 僅當目標不在合法全域使用者目錄內時，才阻擋與使用者資料目錄同名之敏感目錄
        if lower_part == user_dir_name_lower and not is_in_global:
            if idx + 1 < len(parts_lower) and parts_lower[idx + 1] == "skills":
                raise ValueError(t("fs_tool.sensitive_path_skill_hint", part=part))
            raise ValueError(t("fs_tool.sensitive_path_blocked", part=part))

    # 工作區監禁 (Workspace Jail)
    # 將啟動目錄 (CWD) 與全域使用者目錄 (user_dir) 作為基礎工作區無條件加入
    allowed_roots: list[Path] = []
    try:
        allowed_roots.append(Path.cwd().resolve())
    except Exception:
        pass

    if global_user_dir is not None:
        allowed_roots.append(global_user_dir)

    try:
        from pocket_agent.system.paths import bundle_dir

        bundled_skills = (bundle_dir() / "skills").resolve()
        if bundled_skills.exists():
            allowed_roots.append(bundled_skills)
    except Exception:
        pass

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


_BACKUP_MAP_LOCK = threading.Lock()


def _write_backup_map_atomic(mapping_file: Path, mapping: dict[str, str]) -> None:
    """以原子替換方式寫入備份映射檔，避免讀寫競爭導致 JSON 損毀。"""
    import json
    import tempfile

    mapping_file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".latest_backup_map_", suffix=".tmp", dir=str(mapping_file.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(mapping, f, ensure_ascii=False)
        os.replace(tmp_path, mapping_file)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _backup_file(target: Path) -> None:
    """時光機機制：在修改檔案前進行無感備份 (Native I/O)，不觸發 Shell 子行程。"""
    check_cancelled()
    if not target.exists() or not target.is_file():
        return

    try:
        import json
        import secrets
        import shutil
        import time

        from pocket_agent.system.paths import user_dir

        backup_dir = user_dir() / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)

        # 使用不可碰撞的備份名稱：時間戳 + 隨機後綴 + 原始路徑雜湊
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        random_suffix = secrets.token_hex(4)
        # 為區分不同目錄的同名檔案，使用相對路徑的 hash
        try:
            from pocket_agent.system.paths import user_dir as user_dir_func

            rel = target.resolve().relative_to(user_dir_func().resolve())
            path_hash = hashlib.sha256(str(rel).encode()).hexdigest()[:8]
        except Exception:
            path_hash = hashlib.sha256(str(target).encode()).hexdigest()[:8]

        backup_name = f"{timestamp}_{random_suffix}_{path_hash}_{target.name}"
        backup_path = backup_dir / backup_name

        shutil.copy2(target, backup_path)
        log.info(f"Time Machine: 已備份 {target.name} 至 {backup_path}")

        # 記錄最新的備份映射以便 /undo 指令還原
        mapping_file = backup_dir / "latest_backup_map.json"
        with _BACKUP_MAP_LOCK:
            mapping: dict[str, str] = {}
            if mapping_file.exists():
                try:
                    loaded = json.loads(mapping_file.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        mapping = loaded
                except Exception:
                    mapping = {}

            mapping[str(target.resolve())] = str(backup_path.resolve())
            _write_backup_map_atomic(mapping_file, mapping)

    except Exception as e:
        log.warning(f"Time Machine: 備份檔案失敗 ({target}): {e}")


def _evaluate_fs_confirmation(
    on_confirm: Callable[[str], bool | str | tuple[bool, str]] | None,
    action_desc: str,
    op_name: str,
) -> str | None:
    """執行人工確認，僅當回傳值為 True 時放行（回傳 None），否則回傳拒絕訊息。"""
    if on_confirm is None:
        return f"[ERROR] 缺乏使用者確認機制，拒絕執行{op_name}操作"
    try:
        decision = on_confirm(action_desc)
    except Exception:
        log.exception("FS confirm callback 失敗")
        decision = False

    if decision is True:
        return None
    if isinstance(decision, str) and decision.strip():
        return (
            f"[使用者拒絕執行此檔案{op_name}操作，並提供後續指示：\n"
            f"「{decision.strip()}」\n"
            "請立即並嚴格遵循使用者的上述指示繼續處理任務，切勿再次嘗試執行違背該指示的操作。]"
        )
    if isinstance(decision, tuple) and len(decision) == 2 and decision[0] is not True:
        custom_inst = str(decision[1]).strip()
        if custom_inst:
            return (
                f"[使用者拒絕執行此檔案{op_name}操作，並提供後續指示：\n"
                f"「{custom_inst}」\n"
                "請立即並嚴格遵循使用者的上述指示繼續處理任務，切勿再次嘗試執行違背該指示的操作。]"
            )
    return f"[使用者拒絕執行此檔案{op_name}操作]"


def _resolve_skill_reference_path(path_str: str, workspace_dirs: list[str] | None = None) -> Path | None:
    """若相對路徑在目前工作區不存在，嘗試在已註冊之合法技能目錄中尋找對應檔案。

    支援格式：
    1. 包含技能名稱：<skill_name>/reference/ref1.md
    2. 省略技能名稱：reference/ref1.md 或 ref1.md
    """
    cleaned = path_str.strip().strip("'\"")
    if not cleaned:
        return None

    path_obj = Path(cleaned)
    if path_obj.is_absolute() or any(p in ("..", "/", "\\") for p in path_obj.parts):
        return None

    try:
        from pocket_agent.extensions.skills import REPO_SKILLS_DIR, USER_SKILLS_DIR, _get_workspace_skill_dirs

        search_roots = [USER_SKILLS_DIR, REPO_SKILLS_DIR] + _get_workspace_skill_dirs(workspace_dirs)
    except Exception:
        return None

    target_rel = path_obj
    if len(target_rel.parts) > 1 and target_rel.parts[0].lower() == "skills":
        target_rel = Path(*target_rel.parts[1:])

    # 1. 直接匹配：若路徑開頭即為某技能名稱（如 abc/reference/ref1.md）
    for root in search_roots:
        if not root.exists() or not root.is_dir():
            continue
        try:
            candidate = (root / target_rel).resolve()
            if candidate.exists() and candidate.is_relative_to(root.resolve()):
                return candidate
        except Exception:
            continue

    # 2. 次層比對：若路徑省略技能名稱，直接指向內部子路徑（如 reference/ref1.md）
    for root in search_roots:
        if not root.exists() or not root.is_dir():
            continue
        try:
            for skill_dir in root.iterdir():
                if skill_dir.is_dir():
                    candidate = (skill_dir / target_rel).resolve()
                    if candidate.exists() and candidate.is_relative_to(skill_dir.resolve()):
                        return candidate
        except Exception:
            continue

    return None


@dataclass(frozen=True)
class _ReplacePlan:
    """封裝 replace_file_content 多策略匹配與診斷結果。"""

    matched: bool
    new_content: str = ""
    diagnostic: str = ""


def _find_exact_match_locations(content: str, target_str: str) -> list[tuple[int, int]]:
    """回傳 target_str 在全文中所有精確匹配的 (start_line, end_line)（1-based）。"""
    locations: list[tuple[int, int]] = []
    pos = 0
    step = max(len(target_str), 1)
    while True:
        idx = content.find(target_str, pos)
        if idx == -1:
            break
        start_ln = content.count("\n", 0, idx) + 1
        end_ln = content.count("\n", 0, idx + max(len(target_str) - 1, 0)) + 1
        locations.append((start_ln, max(start_ln, end_ln)))
        pos = idx + step
    return locations


def _find_trailing_ws_spans(lines: list[str], target_str: str) -> list[tuple[int, int]]:
    """忽略每行行尾空白與換行差異，回傳相符區間 [start_idx, end_idx)。"""
    target_lines = target_str.splitlines()
    if not target_lines:
        return []
    norm_target = [line.rstrip(" \t") for line in target_lines]
    if len(norm_target) == 1 and not norm_target[0]:
        return []

    norm_lines = [line.rstrip("\r\n").rstrip(" \t") for line in lines]
    span_len = len(norm_target)
    if span_len > len(norm_lines):
        return []

    spans: list[tuple[int, int]] = []
    idx = 0
    limit = len(norm_lines) - span_len
    while idx <= limit:
        if norm_lines[idx : idx + span_len] == norm_target:
            spans.append((idx, idx + span_len))
            idx += span_len
        else:
            idx += 1
    return spans


def _replace_line_spans(
    lines: list[str],
    spans: list[tuple[int, int]],
    target_str: str,
    replacement: str,
) -> str:
    """依行區間替換命中段落，並於必要時保留原末尾換行符號。"""
    target_ends_with_nl = target_str.endswith(("\n", "\r"))
    rep_ends_with_nl = replacement.endswith(("\n", "\r"))

    parts: list[str] = []
    cursor = 0
    for s_idx, e_idx in spans:
        parts.extend(lines[cursor:s_idx])
        span_text = "".join(lines[s_idx:e_idx])
        rep_text = replacement
        if span_text.endswith("\n") and not target_ends_with_nl and not rep_ends_with_nl:
            rep_text = replacement + "\n"
        parts.append(rep_text)
        cursor = e_idx
    parts.extend(lines[cursor:])
    return "".join(parts)


def _apply_replace_in_slice(
    lines: list[str],
    start_idx: int,
    end_idx: int,
    target_str: str,
    replacement: str,
    allow_multiple: bool,
    *,
    allow_ambiguous: bool = True,
) -> tuple[str | None, str | None]:
    """在指定行切片嘗試精確匹配與忽略行尾空白兩階段替換。"""
    prefix = "".join(lines[:start_idx])
    scope_lines = lines[start_idx:end_idx]
    scope = "".join(scope_lines)
    suffix = "".join(lines[end_idx:])

    exact_count = scope.count(target_str)
    if exact_count == 1 or (exact_count > 1 and allow_multiple):
        return prefix + scope.replace(target_str, replacement) + suffix, None
    if exact_count > 1:
        if not allow_ambiguous:
            return None, None
        return (
            None,
            f"[ERROR] 在指定的行數範圍內找到 {exact_count} 處相符的 target 字串，"
            "請設定 allow_multiple=true 或再縮小範圍以確保唯一性。",
        )

    ws_spans = _find_trailing_ws_spans(scope_lines, target_str)
    if len(ws_spans) == 1 or (len(ws_spans) > 1 and allow_multiple):
        new_scope = _replace_line_spans(scope_lines, ws_spans, target_str, replacement)
        return prefix + new_scope + suffix, None
    if len(ws_spans) > 1:
        if not allow_ambiguous:
            return None, None
        return (
            None,
            f"[ERROR] 在指定的行數範圍內找到 {len(ws_spans)} 處相符的 target 字串，"
            "請設定 allow_multiple=true 或再縮小範圍以確保唯一性。",
        )
    return None, None


def _format_match_locations(locations: list[tuple[int, int]]) -> str:
    """將行號區間清單格式化為簡潔的位置字串。"""
    shown = [f"L{s}" if s == e else f"L{s}-L{e}" for s, e in locations[:REPLACE_DIAGNOSTIC_MAX_MATCHES]]
    suffix = ", ..." if len(locations) > REPLACE_DIAGNOSTIC_MAX_MATCHES else ""
    return ", ".join(shown) + suffix


def _find_all_match_locations(content: str, lines: list[str], target_str: str) -> list[tuple[int, int]]:
    """搜尋全文所有精確或忽略行尾空白匹配的 1-based 行號區間。"""
    exact_locs = _find_exact_match_locations(content, target_str)
    if exact_locs:
        return exact_locs
    return [(s_idx + 1, e_idx) for s_idx, e_idx in _find_trailing_ws_spans(lines, target_str)]


def _resolve_replace_content(
    content: str,
    target_str: str,
    replacement: str,
    start_line: int | None,
    end_line: int | None,
    allow_multiple: bool,
) -> _ReplacePlan:
    """依序執行精確、容錯空白、有界偏移自動對齊與診斷回報四階段策略。"""
    lines = content.splitlines(keepends=True)
    total_lines = len(lines)

    if start_line or end_line:
        start_idx = min(total_lines, max(0, int(start_line) - 1)) if start_line else 0
        end_idx = min(total_lines, max(start_idx, int(end_line))) if end_line else total_lines

        new_content, err = _apply_replace_in_slice(
            lines,
            start_idx,
            end_idx,
            target_str,
            replacement,
            allow_multiple,
        )
        if err is not None:
            return _ReplacePlan(matched=False, diagnostic=err)
        if new_content is not None:
            return _ReplacePlan(matched=True, new_content=new_content)

        # 指定行數未命中時，僅於非多重替換且上下有界偏移範圍內唯一相符才自動對齊
        exp_start = max(0, start_idx - REPLACE_SCOPE_DRIFT_LINES)
        exp_end = min(total_lines, end_idx + REPLACE_SCOPE_DRIFT_LINES)
        if not allow_multiple and (exp_start, exp_end) != (start_idx, end_idx):
            aligned_content, _ = _apply_replace_in_slice(
                lines,
                exp_start,
                exp_end,
                target_str,
                replacement,
                allow_multiple=False,
                allow_ambiguous=False,
            )
            if aligned_content is not None:
                return _ReplacePlan(matched=True, new_content=aligned_content)

        locations = _find_all_match_locations(content, lines, target_str)
        if locations:
            msg = t(
                "fs_tool.replace_target_not_found_elsewhere",
                start=start_idx + 1,
                end=end_idx,
                total_lines=total_lines,
                locations=_format_match_locations(locations),
            )
        else:
            msg = t(
                "fs_tool.replace_target_not_found_in_range",
                start=start_idx + 1,
                end=end_idx,
                total_lines=total_lines,
            )
        return _ReplacePlan(matched=False, diagnostic=f"{TOOL_TARGET_NOT_FOUND_PREFIX} {msg}")

    exact_count = content.count(target_str)
    if exact_count == 1 or (exact_count > 1 and allow_multiple):
        return _ReplacePlan(matched=True, new_content=content.replace(target_str, replacement))
    if exact_count > 1:
        return _ReplacePlan(
            matched=False,
            diagnostic=(
                f"[ERROR] 找到 {exact_count} 處相符的 target 字串。"
                "請設定 allow_multiple=true、提供更多上下文，或指定 start_line/end_line 確保唯一性。"
            ),
        )

    ws_spans = _find_trailing_ws_spans(lines, target_str)
    if len(ws_spans) == 1 or (len(ws_spans) > 1 and allow_multiple):
        return _ReplacePlan(
            matched=True,
            new_content=_replace_line_spans(lines, ws_spans, target_str, replacement),
        )
    if len(ws_spans) > 1:
        return _ReplacePlan(
            matched=False,
            diagnostic=(
                f"[ERROR] 找到 {len(ws_spans)} 處相符的 target 字串。"
                "請設定 allow_multiple=true、提供更多上下文，或指定 start_line/end_line 確保唯一性。"
            ),
        )

    msg = t("fs_tool.replace_target_not_found", total_lines=total_lines)
    return _ReplacePlan(matched=False, diagnostic=f"{TOOL_TARGET_NOT_FOUND_PREFIX} {msg}")


def call_fs_tool(
    name: str,
    args: dict,
    on_confirm: Callable[[str], bool | str | tuple[bool, str]] | None,
    workspace_dirs: list[str] | None = None,
    *,
    require_overwrite_flag: bool = False,
) -> str:
    """執行檔案操作工具。"""
    try:
        path_str = args.get("path") or args.get("target_file") or args.get("file") or args.get("target") or ""
        if not path_str:
            return "[ERROR] 缺少必填參數 'path'"

        target = None
        # 唯讀操作支援技能參考路徑回退解析
        if name in ("read_file", "get_file_info", "list_dir"):
            try:
                candidate_target = _validate_safe_path(path_str, workspace_dirs)
                if candidate_target.exists():
                    target = candidate_target
            except ValueError:
                pass

            if target is None:
                skill_path = _resolve_skill_reference_path(path_str, workspace_dirs)
                if skill_path is not None:
                    try:
                        target = _validate_safe_path(str(skill_path), workspace_dirs)
                    except ValueError:
                        pass

        if target is None:
            target = _validate_safe_path(path_str, workspace_dirs)

        if name == "list_dir":
            display_path = (
                str(target.relative_to(Path.cwd().resolve()))
                if target.is_relative_to(Path.cwd().resolve())
                else path_str
            )
            if not target.exists():
                return f"{TOOL_NOT_FOUND_PREFIX} {t('fs_tool.dir_not_found', path=display_path)}"
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
                return f"{TOOL_NOT_FOUND_PREFIX} {t('fs_tool.path_not_found', path=path_str)}"

            stat = target.stat()
            size_bytes = stat.st_size
            mtime = datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")

            display_path = (
                str(target.relative_to(Path.cwd().resolve()))
                if target.is_relative_to(Path.cwd().resolve())
                else path_str
            )
            info = [f"路徑: {display_path}"]
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
            display_path = (
                str(target.relative_to(Path.cwd().resolve()))
                if target.is_relative_to(Path.cwd().resolve())
                else path_str
            )
            if not target.exists():
                return f"{TOOL_NOT_FOUND_PREFIX} {t('fs_tool.file_not_found', path=display_path)}"
            if target.is_dir():
                return f"[ERROR] {display_path} 是一個目錄。請改用 'list_dir' 工具來查看該目錄內容。"
            if not target.is_file():
                return f"[ERROR] 不是一般檔案: {display_path}"

            try:
                fsize = target.stat().st_size
                if isinstance(fsize, int) and fsize > MAX_READ_FILE_BYTES:
                    return (
                        f"[ERROR] 檔案過大 ({fsize:,} Bytes，超過 {MAX_READ_FILE_BYTES // (1024 * 1024)} MB 安全上限)。"
                        "請改用 `search_files` 進行關鍵字檢索。"
                    )
            except OSError:
                pass

            start = args.get("start_line")
            end = args.get("end_line")
            char_offset = args.get("char_offset")
            char_offset = max(0, int(char_offset)) if char_offset is not None else 0

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
            overwrite = bool(args.get("overwrite", False))
            display_path = format_display_path(str(target)) or path_str

            if target.exists() and target.is_dir():
                return f"[ERROR] {t('fs_tool.write_target_is_dir', path=display_path)}"

            existed = target.exists()
            if existed and require_overwrite_flag and not overwrite:
                return f"{TOOL_OVERWRITE_REQUIRED_PREFIX} {t('fs_tool.overwrite_required', path=display_path)}"

            # 人工確認
            action_desc = f"覆寫既有檔案：{display_path}" if existed else f"建立新檔：{display_path}"
            refusal = _evaluate_fs_confirmation(on_confirm, action_desc, "寫入")
            if refusal is not None:
                return refusal

            check_cancelled()
            _backup_file(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            if existed:
                return f"[SUCCESS] 成功覆寫檔案 {target}"
            return f"[SUCCESS] 成功建立檔案 {target}"

        elif name == "append_to_file":
            content = args.get("content", "")

            if not target.exists():
                return "[ERROR] 檔案不存在，無法附加。請使用 write_to_file 建立新檔。"

            refusal = _evaluate_fs_confirmation(on_confirm, f"附加內容至檔案：{target.name}", "寫入")
            if refusal is not None:
                return refusal

            check_cancelled()
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
            plan = _resolve_replace_content(
                content,
                target_str,
                replacement,
                start_line,
                end_line,
                allow_multiple,
            )
            if not plan.matched:
                return plan.diagnostic
            new_content = plan.new_content

            # 人工確認
            refusal = _evaluate_fs_confirmation(on_confirm, f"局部修改檔案：{target.name}", "替換")
            if refusal is not None:
                return refusal

            check_cancelled()
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
                    if len(results) >= DEFAULT_SEARCH_MAX_RESULTS:
                        results.append(
                            f"... (結果過多，已自動截斷前 {DEFAULT_SEARCH_MAX_RESULTS} 筆)\n"
                            "[系統建議] 若要尋找特定內容，請利用 search_files 設定 `query`，\n"
                            "或利用 `file_pattern` 限縮範圍。"
                        )
                        break
                    continue

                # 如果有 query，進行全文檢索
                # DoS 防護：略過超過 MAX_FILE_SEARCH_SIZE_BYTES 的大檔
                try:
                    if p.stat().st_size > MAX_FILE_SEARCH_SIZE_BYTES:
                        continue
                except Exception:
                    continue

                try:
                    lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
                    for i, line in enumerate(lines):
                        if query in line:
                            results.append(f"{rel_path}:{i + 1}: {line.strip()}")
                            if len(results) >= DEFAULT_SEARCH_MAX_RESULTS:
                                results.append(
                                    f"... (結果過多，已自動截斷前 {DEFAULT_SEARCH_MAX_RESULTS} 筆)\n"
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
