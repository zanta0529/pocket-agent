"""Microsoft SQL Server (T-SQL) 唯讀 SQL 語法驗證策略（Strategy Pattern）。"""

import re

from mcp_servers._shared import t
from mcp_servers._shared.db_base import SqlGuardStrategy

_FORBIDDEN_MSSQL_RE = re.compile(
    r"(?i)\b(?:INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|MERGE"
    r"|GRANT|REVOKE|DENY|EXEC|EXECUTE|DECLARE|SET|USE"
    r"|BACKUP|RESTORE|SHUTDOWN|RECONFIGURE|DBCC|KILL|REVERT|CHECKPOINT"
    r"|BEGIN\s+TRAN(?:SACTION)?|COMMIT|ROLLBACK|SAVE\s+TRAN(?:SACTION)?"
    r"|BULK|OPENROWSET|OPENDATASOURCE|OPENQUERY|OPENXML"
    r"|INTO|WAITFOR|UPDATETEXT|WRITETEXT|READTEXT"
    r"|NEXT\s+VALUE\s+FOR|SEQUENCE"
    r"|UPDLOCK|XLOCK|TABLOCKX|HOLDLOCK|SERIALIZABLE|PAGLOCK"
    r"|SP_EXECUTESQL|XP_[\w]+|SP_OA[\w]*|SP_CONFIGURE|SP_ADD[\w]*"
    r"|DM_OS_ENUMERATE_FILESYSTEM|DM_OS_FILE_EXISTS"
    r"|FN_GET_AUDIT_FILE|FN_TRACE_GETTABLE|FN_XE_FILE_TARGET_READ_FILE)\b"
)
_ALLOWED_START_RE = re.compile(r"(?i)^(?:SELECT|WITH)\b")


class MssqlSqlGuard(SqlGuardStrategy):
    """支援 T-SQL 巢狀註解、雙軌字面值遮罩與 [...] / \"...\" 識別字之唯讀查詢驗證策略。"""

    def strip_comments_and_mask_literals(self, sql: str) -> tuple[str, str]:
        """同時產出移除註解之乾淨 T-SQL 與遮蔽字串字面值之語法骨架（防繞過亦防誤攔）。"""
        raw_out: list[str] = []
        masked_out: list[str] = []
        i = 0
        n = len(sql)

        while i < n:
            if sql[i : i + 2] == "/*":
                nesting = 1
                i += 2
                while i < n and nesting > 0:
                    if sql[i : i + 2] == "/*":
                        nesting += 1
                        i += 2
                    elif sql[i : i + 2] == "*/":
                        nesting -= 1
                        i += 2
                    else:
                        i += 1
                if nesting > 0:
                    raise ValueError(t("mssql_mcp.sql_unclosed_comment"))
                raw_out.append(" ")
                masked_out.append(" ")
                continue

            if sql[i : i + 2] == "--":
                i += 2
                while i < n and sql[i] not in "\r\n":
                    i += 1
                raw_out.append(" ")
                masked_out.append(" ")
                continue

            if sql[i] == "'" or (sql[i] in "nN" and i + 1 < n and sql[i + 1] == "'"):
                if sql[i] in "nN":
                    raw_out.append(sql[i])
                    masked_out.append("N")
                    i += 1
                raw_out.append("'")
                masked_out.append("'")
                i += 1
                closed = False
                while i < n:
                    if sql[i] == "'":
                        if i + 1 < n and sql[i + 1] == "'":
                            raw_out.append("''")
                            i += 2
                        else:
                            raw_out.append("'")
                            masked_out.append("'")
                            i += 1
                            closed = True
                            break
                    else:
                        raw_out.append(sql[i])
                        i += 1
                if not closed:
                    raise ValueError(t("mssql_mcp.sql_unclosed_quote"))
                continue

            if sql[i] == "[":
                raw_out.append("[")
                masked_out.append("[")
                i += 1
                closed = False
                while i < n:
                    if sql[i] == "]":
                        if i + 1 < n and sql[i + 1] == "]":
                            raw_out.append("]]")
                            masked_out.append("]]")
                            i += 2
                        else:
                            raw_out.append("]")
                            masked_out.append("]")
                            i += 1
                            closed = True
                            break
                    else:
                        raw_out.append(sql[i])
                        masked_out.append(sql[i])
                        i += 1
                if not closed:
                    raise ValueError(t("mssql_mcp.sql_unclosed_bracket"))
                continue

            if sql[i] == '"':
                raw_out.append('"')
                masked_out.append('"')
                i += 1
                closed = False
                while i < n:
                    if sql[i] == '"':
                        if i + 1 < n and sql[i + 1] == '"':
                            raw_out.append('""')
                            masked_out.append('""')
                            i += 2
                        else:
                            raw_out.append('"')
                            masked_out.append('"')
                            i += 1
                            closed = True
                            break
                    else:
                        raw_out.append(sql[i])
                        masked_out.append(sql[i])
                        i += 1
                if not closed:
                    raise ValueError(t("mssql_mcp.sql_unclosed_quote"))
                continue

            raw_out.append(sql[i])
            masked_out.append(sql[i])
            i += 1

        return "".join(raw_out), "".join(masked_out)

    def normalize_comments(self, sql: str) -> str:
        """在進行關鍵字安全檢查前，正規化移除註解並保留字串常值邊界，防範混淆繞過。"""
        normalized, _ = self.strip_comments_and_mask_literals(sql)
        return normalized

    def validate_select(self, query: str) -> str:
        """驗證 T-SQL 查詢是否為純唯讀單一 SELECT / WITH 語句。"""
        raw_norm, masked_norm = self.strip_comments_and_mask_literals(query)
        raw_clean = raw_norm.strip()
        masked_clean = masked_norm.strip()
        if not raw_clean or not masked_clean:
            raise ValueError(t("mssql_mcp.sql_empty"))

        raw_no_semi = raw_clean.rstrip("; \t\r\n").strip()
        masked_no_semi = masked_clean.rstrip("; \t\r\n").strip()
        if not raw_no_semi or not masked_no_semi:
            raise ValueError(t("mssql_mcp.sql_empty"))

        if ";" in masked_no_semi:
            raise ValueError(t("mssql_mcp.sql_multi_statement_blocked"))

        if not _ALLOWED_START_RE.match(masked_no_semi):
            raise ValueError(t("mssql_mcp.sql_only_readonly_allowed"))

        if _FORBIDDEN_MSSQL_RE.search(masked_no_semi):
            raise ValueError(t("mssql_mcp.sql_forbidden_keyword"))

        return raw_no_semi


MSSQL_SQL_GUARD = MssqlSqlGuard()
