import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

from pocket_agent.system.paths import user_dir

log = logging.getLogger("pocket-agent")

_last_sync_state = None


def _get_codebase_state(workspace_dirs: list[str] | None) -> int:
    """計算程式碼庫的狀態指紋 (基於原始碼檔案的最後修改時間)。"""
    if not workspace_dirs:
        try:
            workspace_dirs = [str(Path.cwd().resolve())]
        except Exception:
            return 0

    state_items = []
    # 限制掃描的副檔名，避免掃描到大圖或編譯產物
    allowed_exts = {".py", ".js", ".ts", ".html", ".css", ".md", ".json", ".yml", ".yaml", ".sh"}
    excludes = {".git", "node_modules", "venv", "__pycache__", ".pocket-agent", "dist", "build"}

    for wd in workspace_dirs:
        root = Path(wd)
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if p.is_file() and p.suffix in allowed_exts:
                if any(ex in p.parts for ex in excludes):
                    continue
                state_items.append((str(p), p.stat().st_mtime))

    return hash(tuple(sorted(state_items)))


def _get_db_path() -> Path:
    db_dir = user_dir() / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "codebase_index.db"


def sync_codebase_index(workspace_dirs: list[str] | None = None):
    """將 workspace 中的原始碼同步至 SQLite FTS5。"""
    global _last_sync_state
    db_path = _get_db_path()

    if db_path.exists():
        try:
            db_path.unlink()
        except OSError as e:
            log.warning("無法刪除舊的 codebase_index.db: %s", e)

    allowed_exts = {".py", ".js", ".ts", ".html", ".css", ".md", ".json", ".yml", ".yaml", ".sh"}
    excludes = {".git", "node_modules", "venv", "__pycache__", ".pocket-agent", "dist", "build"}

    if not workspace_dirs:
        try:
            workspace_dirs = [str(Path.cwd().resolve())]
        except Exception:
            workspace_dirs = []

    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS code_fts USING fts5(path, content)")
        conn.execute("DELETE FROM code_fts")

        for wd in workspace_dirs:
            root = Path(wd)
            if not root.exists():
                continue
            for p in root.rglob("*"):
                if p.is_file() and p.suffix in allowed_exts:
                    if any(ex in p.parts for ex in excludes):
                        continue
                    try:
                        content = p.read_text(encoding="utf-8", errors="ignore")
                        # 避免單一檔案過大撐爆 index，限制前 2000 行
                        lines = content.splitlines()[:2000]
                        truncated_content = "\n".join(lines)
                        conn.execute(
                            "INSERT INTO code_fts (path, content) VALUES (?, ?)", (str(p.resolve()), truncated_content)
                        )
                    except Exception:
                        pass
        conn.commit()

    _last_sync_state = _get_codebase_state(workspace_dirs)
    log.info("RAG Codebase 索引同步完成")


def sync_index_if_needed(workspace_dirs: list[str] | None = None):
    global _last_sync_state
    current_state = _get_codebase_state(workspace_dirs)
    if _last_sync_state == current_state and _get_db_path().exists():
        return
    sync_codebase_index(workspace_dirs)


def call_indexed_search(args: dict[str, Any], workspace_dirs: list[str] | None = None) -> str:
    """提供給 Agent 使用的精準檢索工具。"""
    query = args.get("query", "")
    if not query:
        return "[ERROR] 缺少必填參數 'query'"

    top_k = args.get("top_k", 5)

    sync_index_if_needed(workspace_dirs)

    db_path = _get_db_path()
    with sqlite3.connect(db_path) as conn:
        words = re.findall(r"\w+", query)
        if not words:
            return "[ERROR] 搜尋字串不包含有效的關鍵字"

        fts_query = " OR ".join(words)
        try:
            # 使用 snippet 擷取最相關的片段
            cursor = conn.execute(
                "SELECT path, snippet(code_fts, 1, '>>>', '<<<', '...', 10) "
                "FROM code_fts WHERE code_fts MATCH ? ORDER BY rank LIMIT ?",
                (fts_query, top_k),
            )
            results = []
            for row in cursor.fetchall():
                path = row[0]
                snippet = row[1]
                results.append(f"檔案: {path}\n相關片段:\n{snippet}\n")

            if not results:
                return f"找不到包含 '{query}' 的相關程式碼。"

            inner = "【RAG 檢索結果】\n" + "-" * 40 + "\n" + "\n".join(results)
            return f"<external_content>\n{inner}\n</external_content>"
        except sqlite3.OperationalError as e:
            log.warning("FTS5 查詢失敗: %s", e)
            return f"[ERROR] 檢索失敗: {e}"


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
}
