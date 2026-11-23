"""Microsoft SQL Server (T-SQL) 唯讀 SQL 語法驗證策略（Strategy Pattern）。"""

import re

from mcp_servers._shared.db_base import SqlGuardStrategy

_FORBIDDEN_MSSQL_RE = re.compile(
    r"(?i)\b(?:INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|MERGE"
    r"|GRANT|REVOKE|DENY|EXEC|EXECUTE|DECLARE|SET|USE"
    r"|BACKUP|RESTORE|SHUTDOWN|RECONFIGURE|DBCC|KILL"
    r"|BULK|OPENROWSET|OPENDATASOURCE|OPENQUERY|OPENXML"
    r"|INTO|WAITFOR|UPDATETEXT|WRITETEXT|READTEXT"
    r"|NEXT\s+VALUE\s+FOR|SEQUENCE"
    r"|XP_[\w]+|SP_OA[\w]*|SP_CONFIGURE|SP_ADD[\w]*)\b"
)
_ALLOWED_START_RE = re.compile(r"(?i)^(?:SELECT|WITH)\b")


class MssqlSqlGuard(SqlGuardStrategy):
    """支援 T-SQL 巢狀註解、N'...' 字串遮罩與 [...] 識別字之唯讀查詢驗證策略。"""

    def strip_leading_comments(self, sql: str) -> str:
        """移除 SQL 開頭的單行 (--) 與多行 (/* */) 註解。"""
        s = sql.lstrip()
        while s:
            if s.startswith("--"):
                newline_idx = s.find("\n")
                if newline_idx == -1:
                    return ""
                s = s[newline_idx + 1 :].lstrip()
            elif s.startswith("/*"):
                end_idx = s.find("*/")
                if end_idx == -1:
                    return ""
                s = s[end_idx + 2 :].lstrip()
            else:
                break
        return s

    def normalize_comments(self, sql: str) -> str:
        """在進行關鍵字安全檢查前，正規化移除註解並保留字串常值邊界，防範混淆繞過。"""
        chars: list[str] = []
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
                chars.append(" ")
                continue

            if sql[i : i + 2] == "--":
                i += 2
                while i < n and sql[i] not in "\r\n":
                    i += 1
                chars.append(" ")
                continue

            if sql[i] == "'" or (sql[i] in "nN" and i + 1 < n and sql[i + 1] == "'"):
                if sql[i] in "nN":
                    chars.append("N")
                    i += 1
                chars.append("'")
                i += 1
                closed = False
                while i < n:
                    if sql[i] == "'":
                        if i + 1 < n and sql[i + 1] == "'":
                            chars.append("''")
                            i += 2
                        else:
                            chars.append("'")
                            i += 1
                            closed = True
                            break
                    else:
                        chars.append(" ")
                        i += 1
                if not closed:
                    raise ValueError("基於安全考量，查詢包含未閉合的字串常值。")
                continue

            if sql[i] == "[":
                chars.append("[")
                i += 1
                while i < n and sql[i] != "]":
                    chars.append(sql[i])
                    i += 1
                if i < n:
                    chars.append("]")
                    i += 1
                else:
                    raise ValueError("基於安全考量，查詢包含未閉合的括號識別字。")
                continue

            chars.append(sql[i])
            i += 1

        return re.sub(r"\s+", " ", "".join(chars)).strip()

    def validate_select(self, query: str) -> str:
        """驗證 T-SQL 查詢是否為純唯讀單一 SELECT / WITH 語句。"""
        stripped = self.strip_leading_comments(query).strip()
        if not stripped:
            raise ValueError("查詢語句不可為空。")

        without_trailing_semi = stripped.rstrip("; \t\r\n")
        if ";" in without_trailing_semi:
            raise ValueError("基於安全考量，禁止執行包含分號之多重 SQL 語句。")

        normalized = self.normalize_comments(without_trailing_semi)
        if not _ALLOWED_START_RE.match(normalized):
            raise ValueError("基於安全考量，僅允許執行以 SELECT 或 WITH 開頭之唯讀查詢。")

        if _FORBIDDEN_MSSQL_RE.search(normalized):
            raise ValueError("基於安全考量，查詢包含禁止執行的寫入、預存程序或系統變更關鍵字。")

        return without_trailing_semi


MSSQL_SQL_GUARD = MssqlSqlGuard()
