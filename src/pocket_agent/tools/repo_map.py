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
from pocket_agent.system.paths import user_dir

log = logging.getLogger("pocket-agent")

DEFAULT_REPO_MAP_BUDGET = 1024

_IGNORE_DIRS = {
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
}


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
    """提取單一 Python 檔案的類別與函式結構簽名；解析失敗回傳空字串。"""
    try:
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


class RepoMapGenerator:
    """專案結構鳥瞰圖產生器，具備 SQLite 增量快取與動態 Token 預算控制。"""

    def __init__(self, db_path: Path | None = None):
        if db_path is not None:
            self.db_path = db_path
        else:
            db_dir = user_dir() / "db"
            db_dir.mkdir(parents=True, exist_ok=True)
            self.db_path = db_dir / "repo_map_cache.db"
        self._init_db()

    def _init_db(self) -> None:
        try:
            with sqlite3.connect(self.db_path) as conn:
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
        except sqlite3.OperationalError as e:
            log.warning("Repo Map 快取資料庫初始化異常: %s", e)

    def _get_cached_or_parse(self, file_path: Path, conn: sqlite3.Connection) -> str:
        str_path = str(file_path.resolve())
        try:
            current_mtime = file_path.stat().st_mtime
        except OSError:
            return ""

        cursor = conn.execute("SELECT mtime, skeleton FROM file_ast_cache WHERE path = ?", (str_path,))
        row = cursor.fetchone()
        if row and abs(row[0] - current_mtime) < 0.001:
            return row[1]

        skeleton = extract_file_skeleton(file_path)
        conn.execute(
            "INSERT OR REPLACE INTO file_ast_cache (path, mtime, skeleton) VALUES (?, ?, ?)",
            (str_path, current_mtime, skeleton),
        )
        return skeleton

    def build_repo_map(self, root_dirs: list[Path | str], budget_tokens: int | None = None) -> str:
        """掃描目標目錄產生動態預算之 Repo Map Markdown 字串。"""
        if budget_tokens is None:
            cfg_budget = config.get("system.repo_map_budget")
            budget_tokens = int(cfg_budget) if cfg_budget is not None else DEFAULT_REPO_MAP_BUDGET

        if budget_tokens <= 0:
            return ""

        py_files: list[Path] = []
        for r in root_dirs:
            rp = Path(r).resolve()
            if not rp.exists():
                continue
            if rp.is_file() and rp.suffix == ".py":
                py_files.append(rp)
                continue
            # 遍歷收集 Python 檔案，排除無關目錄
            for p in rp.rglob("*.py"):
                if any(ignored in p.parts for ignored in _IGNORE_DIRS):
                    continue
                py_files.append(p)

        if not py_files:
            return ""

        # 排序：__init__ 與核心目錄模組排在前面
        def file_rank(p: Path) -> tuple[int, str]:
            stem = p.stem.lower()
            name_weight = 0 if stem in ("__init__", "base", "main", "core") else 1
            return (name_weight, str(p))

        py_files.sort(key=file_rank)

        entries: list[tuple[str, str]] = []  # (rel_path_str, skeleton)
        cwd = Path.cwd().resolve()

        try:
            with sqlite3.connect(self.db_path) as conn:
                for f in py_files:
                    skeleton = self._get_cached_or_parse(f, conn)
                    if skeleton.strip():
                        try:
                            rel = str(f.relative_to(cwd))
                        except ValueError:
                            rel = f.name
                        entries.append((rel, skeleton))
                conn.commit()
        except sqlite3.OperationalError as e:
            log.warning("Repo Map 快取存取失敗，使用即時解析: %s", e)
            for f in py_files:
                skeleton = extract_file_skeleton(f)
                if skeleton.strip():
                    entries.append((f.name, skeleton))

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

        return header + "\n".join(selected_blocks)


# 全域單例
default_repo_map = RepoMapGenerator()
