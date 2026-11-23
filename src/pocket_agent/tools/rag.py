import itertools
import logging
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

from pocket_agent.core.cancellation import check_cancelled
from pocket_agent.system.constants import (
    DEFAULT_RAG_MAX_FILE_LINES,
    DEFAULT_RAG_TOP_K,
    MAX_FILE_SEARCH_SIZE_BYTES,
    MAX_RAG_TOP_K,
    USER_DATA_DIR_NAME,
)
from pocket_agent.system.i18n import t
from pocket_agent.system.paths import format_display_path, user_dir
from pocket_agent.tools.fs import is_sensitive_part, wrap_external_content

log = logging.getLogger("pocket-agent")

_RAG_ALLOWED_EXTS = frozenset({".py", ".js", ".ts", ".html", ".css", ".md", ".json", ".yml", ".yaml", ".sh"})
_RAG_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        USER_DATA_DIR_NAME.lower(),
        "dist",
        "build",
        ".agents",
    }
)

_RAG_SYNC_LOCK = threading.Lock()
_last_sync_state: int | None = None


def _resolve_workspace_roots(workspace_dirs: list[str] | None) -> list[Path]:
    if not workspace_dirs:
        try:
            return [Path.cwd().resolve()]
        except Exception:
            return []
    roots: list[Path] = []
    for wd in workspace_dirs:
        try:
            rp = Path(wd).expanduser().resolve()
            if rp.exists():
                roots.append(rp)
        except Exception:
            continue
    return roots


def _collect_indexable_files(workspace_dirs: list[str] | None) -> list[tuple[Path, float]]:
    """單次掃描工作區可索引之原始碼檔案與 mtime（跳過敏感檔、symlink、排除目錄與超大檔）。"""
    items: list[tuple[Path, float]] = []
    seen: set[Path] = set()
    for root in _resolve_workspace_roots(workspace_dirs):
        for p in root.rglob("*"):
            check_cancelled()
            if any(is_sensitive_part(part) or part.lower() in _RAG_EXCLUDED_DIRS for part in p.parts):
                continue
            if p.is_symlink() or not p.is_file() or p.suffix.lower() not in _RAG_ALLOWED_EXTS:
                continue
            try:
                resolved = p.resolve()
                if resolved in seen:
                    continue
                stat = p.stat()
                if stat.st_size > MAX_FILE_SEARCH_SIZE_BYTES:
                    continue
                seen.add(resolved)
                items.append((resolved, stat.st_mtime))
            except OSError:
                continue
    items.sort(key=lambda pair: str(pair[0]))
    return items


def _compute_fingerprint(files_with_mtime: list[tuple[Path, float]]) -> int:
    return hash(tuple((str(p), mt) for p, mt in files_with_mtime))


def _get_codebase_state(workspace_dirs: list[str] | None) -> int:
    """計算程式碼庫的狀態指紋 (基於原始碼檔案的最後修改時間)。"""
    return _compute_fingerprint(_collect_indexable_files(workspace_dirs))


def _get_db_path() -> Path:
    db_dir = user_dir() / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "codebase_index.db"


def _sync_codebase_index_unlocked(
    files_with_mtime: list[tuple[Path, float]],
    fingerprint: int,
) -> None:
    global _last_sync_state
    db_path = _get_db_path()

    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS code_fts USING fts5(path, content)")
        conn.execute("DELETE FROM code_fts")

        for p, _mt in files_with_mtime:
            check_cancelled()
            try:
                with p.open("r", encoding="utf-8", errors="ignore") as f:
                    head_lines = [line.rstrip("\r\n") for line in itertools.islice(f, DEFAULT_RAG_MAX_FILE_LINES)]
                truncated_content = "\n".join(head_lines)
                conn.execute(
                    "INSERT INTO code_fts (path, content) VALUES (?, ?)",
                    (str(p), truncated_content),
                )
            except Exception:
                continue
        conn.commit()

    _last_sync_state = fingerprint
    log.info("RAG Codebase 索引同步完成 (%d 個檔案)", len(files_with_mtime))


def sync_codebase_index(workspace_dirs: list[str] | None = None) -> None:
    """將 workspace 中的原始碼同步至 SQLite FTS5。"""
    with _RAG_SYNC_LOCK:
        files = _collect_indexable_files(workspace_dirs)
        _sync_codebase_index_unlocked(files, _compute_fingerprint(files))


def sync_index_if_needed(workspace_dirs: list[str] | None = None) -> None:
    with _RAG_SYNC_LOCK:
        files = _collect_indexable_files(workspace_dirs)
        current_state = _compute_fingerprint(files)
        if _last_sync_state == current_state and _get_db_path().exists():
            return
        _sync_codebase_index_unlocked(files, current_state)


def _resolve_top_k(raw_top_k: Any) -> int:
    if raw_top_k is None:
        return DEFAULT_RAG_TOP_K
    try:
        val = int(raw_top_k)
    except TypeError, ValueError:
        return DEFAULT_RAG_TOP_K
    if val <= 0:
        return DEFAULT_RAG_TOP_K
    return max(1, min(val, MAX_RAG_TOP_K))


def call_indexed_search(args: dict[str, Any], workspace_dirs: list[str] | None = None) -> str:
    """提供給 Agent 使用的精準檢索工具。"""
    query = str(args.get("query") or "").strip()
    if not query:
        return f"[ERROR] {t('rag_tool.missing_query')}"

    words = re.findall(r"\w+", query)
    if not words:
        return f"[ERROR] {t('rag_tool.invalid_keywords')}"

    top_k = _resolve_top_k(args.get("top_k"))
    sync_index_if_needed(workspace_dirs)

    db_path = _get_db_path()
    fts_query = " OR ".join(f'"{w}"' for w in words)
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.execute(
                "SELECT path, snippet(code_fts, 1, '>>>', '<<<', '...', 10) "
                "FROM code_fts WHERE code_fts MATCH ? ORDER BY rank LIMIT ?",
                (fts_query, top_k),
            )
            rows = cursor.fetchall()

        if not rows:
            return t("rag_tool.no_results", query=query)

        results: list[str] = []
        for raw_path, snippet in rows:
            display_path = format_display_path(str(raw_path)) or str(raw_path)
            results.append(t("rag_tool.result_item", path=display_path, snippet=snippet))

        inner = f"{t('rag_tool.result_header')}\n" + "-" * 40 + "\n" + "\n".join(results)
        return wrap_external_content(inner)
    except sqlite3.OperationalError as e:
        log.warning("FTS5 查詢失敗: %s", e)
        return f"[ERROR] {t('rag_tool.search_failed', error=e)}"


INDEXED_SEARCH_TOOL = {
    "type": "function",
    "name": "indexed_search",
    "description": (
        "Semantic codebase search (RAG). Highly recommended over `grep_search` when you need to find "
        "concepts, functions, or keywords across the entire project. It uses local FTS5 indexing to quickly "
        "return the most relevant code snippets without overflowing your context window."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The concept, keyword, or function name to search for."},
            "top_k": {"type": "integer", "description": "Number of top results to return (default 5, max 10)."},
        },
        "required": ["query"],
    },
    "_is_safe_read": True,
}

RAG_TOOLS = [INDEXED_SEARCH_TOOL]


def is_rag_tool(name: str) -> bool:
    return any(tool["name"] == name for tool in RAG_TOOLS)
