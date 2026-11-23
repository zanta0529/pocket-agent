"""SQLite 唯讀 SQL 語法驗證策略（Strategy Pattern）。"""

import re

from mcp_servers._shared.db_base import SqlGuardStrategy

_FORBIDDEN_SQLITE_RE = re.compile(
    r"(?i)\b(?:INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|TRUNCATE|ATTACH|DETACH|VACUUM|REINDEX|PRAGMA)\b"
)
_ALLOWED_START_RE = re.compile(r"^(?:EXPLAIN\s+(?:QUERY\s+PLAN\s+)?)?(?:SELECT|WITH)\b", re.IGNORECASE)


class SqliteSqlGuard(SqlGuardStrategy):
    """SQLite 單一唯讀 SELECT / WITH / EXPLAIN QUERY PLAN 驗證策略。"""

    def normalize_comments(self, sql: str) -> str:
        """移除 SQL 開頭的單行 (--) 與多行 (/* */) 註解。"""
        s = sql.strip()
        while s:
            if s.startswith("--"):
                nl = s.find("\n")
                s = s[nl + 1 :].strip() if nl != -1 else ""
            elif s.startswith("/*"):
                end = s.find("*/")
                if end == -1:
                    return ""
                s = s[end + 2 :].strip()
            else:
                break
        return s

    def validate_select(self, query: str) -> str:
        """驗證單一唯讀 SELECT / WITH / EXPLAIN 查詢語句，禁止多重語句與危險關鍵字。"""
        stripped = self.normalize_comments(query)
        while stripped.endswith(";"):
            stripped = stripped[:-1].strip()
        if not stripped:
            raise ValueError("查詢語句不可為空。")
        if ";" in stripped:
            raise ValueError("禁止執行多重 SQL 語句（不可包含分號 `;`）。")
        if not _ALLOWED_START_RE.match(stripped):
            raise ValueError("基於安全考量，僅允許執行以 SELECT、WITH 或 EXPLAIN SELECT 開頭之唯讀查詢。")
        if _FORBIDDEN_SQLITE_RE.search(stripped):
            raise ValueError("查詢包含不允許的寫入或管理關鍵字，僅允許純唯讀查詢。")
        return stripped


SQLITE_SQL_GUARD = SqliteSqlGuard()
