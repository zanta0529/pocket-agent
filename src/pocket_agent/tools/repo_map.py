"""輕量級 Python AST 專案結構地圖產生器 (Pocket Repo Map)。

借鏡 Aider 經典 Repo Map 架構，利用 Python 原生 ast 模組提取專案所有模組之
類別定義與函式簽名（省略內部實作代碼），並透過 SQLite 與 mtime 進行增量快取，
最後依動態 Token 預算（預設約 1,024 tokens）組裝為專案鳥瞰地圖注入 Prompt。
"""

import ast
import logging
import sqlite3
from pathlib import Path
from typing import Any

from pocket_agent.core.context_budget import estimate_tokens
from pocket_agent.system.config import config
from pocket_agent.system.constants import (
    DEFAULT_REPO_MAP_BUDGET_TOKENS,
    MAX_FILE_SEARCH_SIZE_BYTES,
    USER_DATA_DIR_NAME,
)
from pocket_agent.system.paths import user_dir

log = logging.getLogger("pocket-agent")

# 向後相容別名 (對齊 constants.py)
DEFAULT_REPO_MAP_BUDGET = DEFAULT_REPO_MAP_BUDGET_TOKENS

_IGNORE_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        "node_modules",
        ".pytest_cache",
        ".ruff_cache",
        "build",
        "dist",
        ".agents",
        USER_DATA_DIR_NAME.lower(),
    }
)


class _AstSignatureExtractor(ast.NodeVisitor):
    """走訪 AST 僅萃取類別與函式定義簽名，忽略實作細節。"""

    def __init__(self):
        self.lines: list[str] = []
        self._indent = 0

    def _pad(self) -> str:
        return "  " * self._indent

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:
        bases = [ast.unparse(b) for b in node.bases]
        bases_str = f"({', '.join(bases)})" if bases else ""
        self.lines.append(f"{self._pad()}class {node.name}{bases_str}:")
        self._indent += 1
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.visit(item)
        self._indent -= 1

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        self._record_func(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        self._record_func(node, is_async=True)

    def _record_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef, is_async: bool) -> None:
        prefix = "async def " if is_async else "def "
        # 參數列表格式化
        args_str = ast.unparse(node.args)
        returns_str = f" -> {ast.unparse(node.returns)}" if node.returns else ""
        self.lines.append(f"{self._pad()}{prefix}{node.name}({args_str}){returns_str}: ...")


def extract_file_skeleton(py_path: Path) -> str:
    """提取單一 Python 檔案的類別與函式結構簽名；解析失敗或檔案過大回傳空字串。"""
    try:
        if py_path.stat().st_size > MAX_FILE_SEARCH_SIZE_BYTES:
            return ""
        source = py_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(py_path))
        extractor = _AstSignatureExtractor()
        for stmt in tree.body:
            if isinstance(stmt, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                extractor.visit(stmt)
        return "\n".join(extractor.lines)
    except Exception as e:
        log.debug("無法解析 AST: %s: %s", py_path, e)
        return ""


def _format_rel_path(file_path: Path, cwd: Path, root_paths: list[Path]) -> str:
    """優先回傳相對於 cwd 的路徑；若不在 cwd 下則回傳相對於所屬 root_dir 的路徑。"""
    if file_path.is_relative_to(cwd):
        return str(file_path.relative_to(cwd))
    for rp in root_paths:
        if rp.is_dir() and file_path.is_relative_to(rp):
            return str(file_path.relative_to(rp))
    return file_path.name


class RepoMapGenerator:
    """專案結構鳥瞰圖產生器，具備 SQLite 增量快取與動態 Token 預算控制。"""

    def __init__(self, db_path: Path | None = None):
        self._explicit_db_path: Path | None = db_path
        self._initialized_db_path: Path | None = None
        self._map_cache: dict[tuple[int, int], str] = {}
        if db_path is not None:
            self._ensure_db(db_path)

    @property
    def db_path(self) -> Path:
        if self._explicit_db_path is not None:
            return self._explicit_db_path
        db_dir = user_dir() / "db"
        db_dir.mkdir(parents=True, exist_ok=True)
        return db_dir / "repo_map_cache.db"

    @db_path.setter
    def db_path(self, value: Path | None) -> None:
        self._explicit_db_path = value
        self._initialized_db_path = None

    def _ensure_db(self, target_db: Path | None = None) -> Path:
        path = target_db if target_db is not None else self.db_path
        if self._initialized_db_path == path and path.is_file():
            return path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(path) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS file_ast_cache (
                        path TEXT PRIMARY KEY,
                        mtime REAL,
                        skeleton TEXT
                    )
                    """
                )
                conn.commit()
            self._initialized_db_path = path
        except sqlite3.OperationalError as e:
            log.warning("Repo Map 快取資料庫初始化異常: %s", e)
        return path

    def _init_db(self) -> None:
        self._ensure_db()

    @staticmethod
    def _prune_stale_in_conn(
        conn: sqlite3.Connection,
    ) -> tuple[int, list[tuple[str, float, str]]]:
        """於既有 SQLite 連線中清除磁碟已不存在之孤兒路徑，回傳 (刪除筆數, 有效列清單)。"""
        rows = conn.execute("SELECT path, mtime, skeleton FROM file_ast_cache").fetchall()
        stale_paths: list[tuple[str]] = []
        valid_rows: list[tuple[str, float, str]] = []
        for row in rows:
            if Path(row[0]).exists():
                valid_rows.append(row)
            else:
                stale_paths.append((row[0],))
        if stale_paths:
            conn.executemany("DELETE FROM file_ast_cache WHERE path = ?", stale_paths)
        return len(stale_paths), valid_rows

    def _get_cached_or_parse(
        self,
        file_path: Path,
        conn: sqlite3.Connection,
        db_cache: dict[str, tuple[float, str]] | None = None,
        current_mtime: float | None = None,
    ) -> str:
        str_path = str(file_path.resolve())
        if current_mtime is None:
            try:
                current_mtime = file_path.stat().st_mtime
            except OSError:
                return ""

        if db_cache is not None and str_path in db_cache:
            cached_mtime, cached_skeleton = db_cache[str_path]
            if abs(cached_mtime - current_mtime) < 0.001:
                return cached_skeleton
        else:
            cursor = conn.execute("SELECT mtime, skeleton FROM file_ast_cache WHERE path = ?", (str_path,))
            row = cursor.fetchone()
            if row and abs(row[0] - current_mtime) < 0.001:
                return row[1]

        skeleton = extract_file_skeleton(file_path)
        conn.execute(
            "INSERT OR REPLACE INTO file_ast_cache (path, mtime, skeleton) VALUES (?, ?, ?)",
            (str_path, current_mtime, skeleton),
        )
        if db_cache is not None:
            db_cache[str_path] = (current_mtime, skeleton)
        return skeleton

    def prune_stale_entries(self, *, vacuum: bool = False) -> int:
        """清除快取資料庫中已不存在於磁碟之孤兒路徑紀錄，回傳已刪除筆數。"""
        target_db = self.db_path
        if not target_db.is_file():
            return 0
        try:
            with sqlite3.connect(target_db) as conn:
                deleted, _ = self._prune_stale_in_conn(conn)
                if deleted == 0:
                    return 0
                conn.commit()
                if vacuum or deleted >= 100:
                    conn.execute("VACUUM")
            self._map_cache.clear()
            log.info("Repo Map 已清除 %d 筆孤兒快取紀錄", deleted)
            return deleted
        except sqlite3.OperationalError as e:
            log.warning("Repo Map 孤兒快取清理失敗: %s", e)
            return 0

    def build_repo_map(self, root_dirs: list[Path | str], budget_tokens: int | None = None) -> str:
        """掃描目標目錄產生動態預算之 Repo Map Markdown 字串。"""
        if budget_tokens is None:
            cfg_budget = config.get("system.repo_map_budget")
            try:
                budget_tokens = int(cfg_budget) if cfg_budget is not None else DEFAULT_REPO_MAP_BUDGET
            except TypeError, ValueError:
                budget_tokens = DEFAULT_REPO_MAP_BUDGET

        if budget_tokens <= 0:
            return ""

        py_files: list[Path] = []
        root_paths: list[Path] = []
        seen_files: set[Path] = set()
        for r in root_dirs:
            try:
                rp = Path(r).resolve()
            except OSError:
                continue
            if not rp.exists():
                continue
            root_paths.append(rp)
            if rp.is_file() and rp.suffix == ".py":
                if rp not in seen_files:
                    seen_files.add(rp)
                    py_files.append(rp)
                continue
            # 遍歷收集 Python 檔案，排除無關目錄與 symlink
            for p in rp.rglob("*.py"):
                if any(part.lower() in _IGNORE_DIRS for part in p.parts):
                    continue
                if p.is_symlink():
                    continue
                try:
                    resolved_p = p.resolve()
                except OSError:
                    continue
                if resolved_p not in seen_files:
                    seen_files.add(resolved_p)
                    py_files.append(resolved_p)

        if not py_files:
            return ""

        # 排序：__init__ 與核心目錄模組排在前面
        def file_rank(p: Path) -> tuple[int, str]:
            stem = p.stem.lower()
            name_weight = 0 if stem in ("__init__", "base", "main", "core") else 1
            return (name_weight, str(p))

        py_files.sort(key=file_rank)

        file_mtimes: list[tuple[Path, float]] = []
        for f in py_files:
            try:
                stat = f.stat()
                if stat.st_size > MAX_FILE_SEARCH_SIZE_BYTES:
                    continue
                file_mtimes.append((f, stat.st_mtime))
            except OSError:
                continue

        if not file_mtimes:
            return ""

        state_fingerprint = hash(tuple((str(f), mt) for f, mt in file_mtimes))
        cache_key = (state_fingerprint, budget_tokens)
        if cache_key in self._map_cache:
            return self._map_cache[cache_key]

        entries: list[tuple[str, str]] = []  # (rel_path_str, skeleton)
        cwd = Path.cwd().resolve()

        try:
            target_db = self._ensure_db()
            with sqlite3.connect(target_db) as conn:
                _, valid_rows = self._prune_stale_in_conn(conn)
                db_cache = {r[0]: (r[1], r[2]) for r in valid_rows}
                for f, mt in file_mtimes:
                    skeleton = self._get_cached_or_parse(f, conn, db_cache=db_cache, current_mtime=mt)
                    if skeleton.strip():
                        entries.append((_format_rel_path(f, cwd, root_paths), skeleton))
                conn.commit()
        except sqlite3.OperationalError as e:
            log.warning("Repo Map 快取存取失敗，使用即時解析: %s", e)
            for f, _mt in file_mtimes:
                skeleton = extract_file_skeleton(f)
                if skeleton.strip():
                    entries.append((_format_rel_path(f, cwd, root_paths), skeleton))

        if not entries:
            return ""

        # 二元搜尋 / 貪婪截斷填裝至預算上限
        selected_blocks: list[str] = []
        current_tokens = 0

        header = "## 專案核心結構鳥瞰圖 (Pocket Repo Map)\n以下為工作區核心模組與公開介面骨架（簽名摘要）：\n"
        current_tokens += estimate_tokens(header)

        for rel_path, skeleton in entries:
            block = f"{rel_path}:\n"
            for line in skeleton.splitlines():
                block += f"  {line}\n"

            block_tokens = estimate_tokens(block)
            if current_tokens + block_tokens > budget_tokens:
                # 超過預算則停止塞入更多模組，保留頭部最具代表性之架構
                break
            selected_blocks.append(block)
            current_tokens += block_tokens

        if not selected_blocks:
            return ""

        result_map = header + "\n".join(selected_blocks)
        self._map_cache[cache_key] = result_map
        return result_map


# 全域單例
default_repo_map = RepoMapGenerator()
