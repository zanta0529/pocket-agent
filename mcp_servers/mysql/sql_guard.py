"""MySQL / MariaDB 唯讀 SQL 語法驗證策略（Strategy Pattern）。"""

import re

from mcp_servers._shared import t
from mcp_servers._shared.db_base import SqlGuardStrategy

_FORBIDDEN_MYSQL_RE = re.compile(
    r"(?i)\b(?:INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|REPLACE|RENAME"
    r"|GRANT|REVOKE|CALL|EXECUTE|PREPARE|DEALLOCATE|HANDLER|LOAD\s+DATA|LOAD\s+XML"
    r"|INTO\s+OUTFILE|INTO\s+DUMPFILE|INTO\s+@"
    r"|FOR\s+UPDATE|FOR\s+SHARE|LOCK\s+IN\s+SHARE\s+MODE|LOCK\s+TABLES|UNLOCK\s+TABLES"
    r"|SET\s+GLOBAL|SET\s+SESSION|SET\s+PERSIST|SET\s+@@"
    r")\b|\b(?:SLEEP|BENCHMARK|GET_LOCK|RELEASE_LOCK|RELEASE_ALL_LOCKS"
    r"|IS_FREE_LOCK|IS_USED_LOCK|LOAD_FILE|WAIT_FOR_EXECUTED_GTID_SET"
    r"|SOURCE_POS_WAIT|MASTER_POS_WAIT)\s*\("
)
_ALLOWED_START_RE = re.compile(r"(?i)^(?:EXPLAIN\s+(?:FORMAT\s*=\s*\w+\s+)?)?(?:SELECT|WITH)\b")
_SELECT_OR_WITH_START_RE = re.compile(r"(?i)^(?:SELECT|WITH)\b")


class MysqlSqlGuard(SqlGuardStrategy):
    """具引號狀態機與字面值遮蔽之 MySQL/MariaDB 單一唯讀查詢驗證策略。"""

    def strip_comments_and_mask_literals(self, sql: str) -> tuple[str, str]:
        """同時產出移除註解之可執行 SQL 與遮蔽字串字面值之語法骨架（避免誤殺或繞過）。"""
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
                    if c == "\\" and quote in ("'", '"'):
                        raise ValueError(t("mysql_mcp.sql_backslash_escape_blocked"))
                    if c == quote:
                        if i + 1 < n and sql[i + 1] == quote:
                            raw_out.append(quote + quote)
                            if quote == "`":
                                masked_out.append(quote + quote)
                            i += 2
                            continue
                        raw_out.append(quote)
                        masked_out.append(quote)
                        i += 1
                        closed = True
                        break
                    raw_out.append(c)
                    if quote == "`":
                        masked_out.append(c)
                    i += 1
                if not closed:
                    raise ValueError(t("mysql_mcp.sql_unclosed_quote"))
                continue

            if ch == "/" and i + 1 < n and sql[i + 1] == "*":
                end_idx = sql.find("*/", i + 2)
                if end_idx == -1:
                    raise ValueError(t("mysql_mcp.sql_unclosed_comment"))
                body = sql[i + 2 : end_idx]
                if body.startswith("!") or re.match(r"^[mM]!", body):
                    raise ValueError(t("mysql_mcp.sql_executable_comment_blocked"))
                raw_out.append(" ")
                masked_out.append(" ")
                i = end_idx + 2
                continue

            if ch == "#":
                newline_idx = sql.find("\n", i + 1)
                raw_out.append(" ")
                masked_out.append(" ")
                if newline_idx == -1:
                    break
                i = newline_idx
                continue

            if ch == "-" and i + 1 < n and sql[i + 1] == "-":
                if i + 2 < n and not (sql[i + 2].isspace() or ord(sql[i + 2]) <= 32):
                    raise ValueError(t("mysql_mcp.sql_dash_comment_space_required"))
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
        """具引號狀態之 SQL 註解掃描與正規化（拒絕可執行註解與引號內反斜線跳脫）。"""
        normalized, _ = self.strip_comments_and_mask_literals(sql)
        return normalized

    def _validate_core(self, query: str) -> tuple[str, str]:
        normalized, masked = self.strip_comments_and_mask_literals(query)
        clean_raw = normalized.strip()
        clean_masked = masked.strip()
        if not clean_raw or not clean_masked:
            raise ValueError(t("mysql_mcp.sql_empty"))

        raw_no_semi = clean_raw.rstrip("; \t\r\n").strip()
        masked_no_semi = clean_masked.rstrip("; \t\r\n").strip()
        if not raw_no_semi or not masked_no_semi:
            raise ValueError(t("mysql_mcp.sql_empty"))
        if ";" in masked_no_semi:
            raise ValueError(t("mysql_mcp.sql_multi_statement_blocked"))

        if not _ALLOWED_START_RE.match(masked_no_semi):
            raise ValueError(t("mysql_mcp.sql_only_readonly_allowed"))

        if _FORBIDDEN_MYSQL_RE.search(masked_no_semi):
            raise ValueError(t("mysql_mcp.sql_forbidden_keyword"))

        return raw_no_semi, masked_no_semi

    def validate_select(self, query: str) -> str:
        """驗證 SQL 查詢是否為純唯讀單一 SELECT / WITH / EXPLAIN 語句。"""
        raw_no_semi, _ = self._validate_core(query)
        return raw_no_semi

    def validate_explainable_select(self, query: str) -> str:
        """驗證 SQL 查詢為純 SELECT 或 WITH 語句（供 mysql_explain_query 使用，禁止巢狀 EXPLAIN）。"""
        raw_no_semi, masked_no_semi = self._validate_core(query)
        if not _SELECT_OR_WITH_START_RE.match(masked_no_semi):
            raise ValueError(t("mysql_mcp.sql_explain_only_select_with"))
        return raw_no_semi


MYSQL_SQL_GUARD = MysqlSqlGuard()
