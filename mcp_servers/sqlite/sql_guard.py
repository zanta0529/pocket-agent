"""SQLite 唯讀 SQL 語法驗證策略（Strategy Pattern）。"""

import re

from mcp_servers._shared import t
from mcp_servers._shared.db_base import SqlGuardStrategy

_FORBIDDEN_SQLITE_RE = re.compile(
    r"(?i)\b(?:INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|TRUNCATE"
    r"|ATTACH|DETACH|VACUUM|REINDEX|PRAGMA|ANALYZE"
    r"|BEGIN|COMMIT|ROLLBACK|SAVEPOINT|RELEASE)\b"
    r"|\b(?:LOAD_EXTENSION|ZEROBLOB|RANDOMBLOB|READFILE|WRITEFILE|FTS3_TOKENIZER|EDIT)\s*\("
)
_ALLOWED_START_RE = re.compile(r"(?i)^(?:EXPLAIN\s+(?:QUERY\s+PLAN\s+)?)?(?:SELECT|WITH)\b")
_SELECT_OR_WITH_START_RE = re.compile(r"(?i)^(?:SELECT|WITH)\b")


class SqliteSqlGuard(SqlGuardStrategy):
    """具引號狀態機與字面值遮蔽之 SQLite 單一唯讀 SELECT / WITH / EXPLAIN 驗證策略。"""

    def strip_comments_and_mask_literals(self, sql: str) -> tuple[str, str]:
        """同時產出移除註解之可執行 SQL 與遮蔽字串／識別字內容之語法骨架。"""
        raw_out: list[str] = []
        masked_out: list[str] = []
        i = 0
        n = len(sql)

        while i < n:
            ch = sql[i]

            if ch in ("'", '"', "`"):
                quote = ch
                raw_out.append(quote)
                masked_out.append(quote)
                i += 1
                closed = False
                while i < n:
                    c = sql[i]
                    if c == quote:
                        if i + 1 < n and sql[i + 1] == quote:
                            raw_out.append(quote + quote)
                            i += 2
                            continue
                        raw_out.append(quote)
                        masked_out.append(quote)
                        i += 1
                        closed = True
                        break
                    raw_out.append(c)
                    i += 1
                if not closed:
                    raise ValueError(t("sqlite_mcp.sql_unclosed_quote"))
                continue

            if ch == "[":
                end_idx = sql.find("]", i + 1)
                if end_idx == -1:
                    raise ValueError(t("sqlite_mcp.sql_unclosed_identifier"))
                raw_out.append(sql[i : end_idx + 1])
                masked_out.append("[]")
                i = end_idx + 1
                continue

            if ch == "/" and i + 1 < n and sql[i + 1] == "*":
                end_idx = sql.find("*/", i + 2)
                if end_idx == -1:
                    raise ValueError(t("sqlite_mcp.sql_unclosed_comment"))
                raw_out.append(" ")
                masked_out.append(" ")
                i = end_idx + 2
                continue

            if ch == "-" and i + 1 < n and sql[i + 1] == "-":
                newline_idx = sql.find("\n", i + 2)
                raw_out.append(" ")
                masked_out.append(" ")
                if newline_idx == -1:
                    break
                i = newline_idx
                continue

            raw_out.append(ch)
            masked_out.append(ch)
            i += 1

        return "".join(raw_out), "".join(masked_out)

    def normalize_comments(self, sql: str) -> str:
        """具引號狀態之 SQL 註解掃描與正規化，同時保留字面值與識別字邊界。"""
        normalized, _ = self.strip_comments_and_mask_literals(sql)
        return normalized

    def _validate_core(self, query: str) -> tuple[str, str]:
        normalized, masked = self.strip_comments_and_mask_literals(query)
        clean_raw = normalized.strip()
        clean_masked = masked.strip()
        if not clean_raw or not clean_masked:
            raise ValueError(t("sqlite_mcp.sql_empty"))

        raw_no_semi = clean_raw.rstrip("; \t\r\n").strip()
        masked_no_semi = clean_masked.rstrip("; \t\r\n").strip()
        if not raw_no_semi or not masked_no_semi:
            raise ValueError(t("sqlite_mcp.sql_empty"))
        if ";" in masked_no_semi:
            raise ValueError(t("sqlite_mcp.sql_multi_statement_blocked"))
        if not _ALLOWED_START_RE.match(masked_no_semi):
            raise ValueError(t("sqlite_mcp.sql_only_readonly_allowed"))
        if _FORBIDDEN_SQLITE_RE.search(masked_no_semi):
            raise ValueError(t("sqlite_mcp.sql_forbidden_keyword"))
        return raw_no_semi, masked_no_semi

    def validate_select(self, query: str) -> str:
        """驗證單一唯讀 SELECT / WITH / EXPLAIN 查詢語句，禁止多重語句與危險關鍵字。"""
        raw_no_semi, _ = self._validate_core(query)
        return raw_no_semi

    def validate_explainable_select(self, query: str) -> str:
        """驗證純 SELECT 或 WITH 查詢語句（供 sqlite_explain_query 使用，禁止巢狀 EXPLAIN）。"""
        raw_no_semi, masked_no_semi = self._validate_core(query)
        if not _SELECT_OR_WITH_START_RE.match(masked_no_semi):
            raise ValueError(t("sqlite_mcp.sql_explain_only_select_with"))
        return raw_no_semi


SQLITE_SQL_GUARD = SqliteSqlGuard()
