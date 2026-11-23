"""MySQL / MariaDB 唯讀 SQL 語法驗證策略（Strategy Pattern）。"""

import re

from mcp_servers._shared.db_base import SqlGuardStrategy

_FORBIDDEN_MYSQL_RE = re.compile(
    r"(?i)\b(?:INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|REPLACE|RENAME"
    r"|GRANT|REVOKE|CALL|EXECUTE|PREPARE|DEALLOCATE|HANDLER|LOAD\s+DATA|LOAD\s+XML"
    r"|INTO\s+OUTFILE|INTO\s+DUMPFILE|INTO\s+@"
    r"|FOR\s+UPDATE|LOCK\s+IN\s+SHARE\s+MODE|LOCK\s+TABLES|UNLOCK\s+TABLES"
    r"|SET\s+GLOBAL|SET\s+SESSION|SET\s+PERSIST|SET\s+@@"
    r")\b|\b(?:SLEEP|BENCHMARK|GET_LOCK|RELEASE_LOCK|RELEASE_ALL_LOCKS)\s*\("
)
_ALLOWED_START_RE = re.compile(r"(?i)^(?:EXPLAIN\s+(?:FORMAT\s*=\s*\w+\s+)?)?(?:SELECT|WITH)\b")


class MysqlSqlGuard(SqlGuardStrategy):
    """具引號狀態機之 MySQL/MariaDB 單一唯讀查詢驗證策略。"""

    def normalize_comments(self, sql: str) -> str:
        """具引號狀態之 SQL 註解掃描與正規化（拒絕可執行註解與引號內反斜線跳脫）。"""
        out: list[str] = []
        i = 0
        n = len(sql)

        while i < n:
            ch = sql[i]

            if ch in ("'", '"', "`"):
                quote = ch
                out.append(quote)
                i += 1
                closed = False
                while i < n:
                    c = sql[i]
                    if c == "\\" and quote in ("'", '"'):
                        raise ValueError(
                            "基於安全考量，禁止於引號內使用反斜線跳脫（請改用重複引號），"
                            "以避免不同 SQL_MODE 下引號邊界解析差異。"
                        )
                    if c == quote:
                        if i + 1 < n and sql[i + 1] == quote:
                            out.append(quote + quote)
                            i += 2
                            continue
                        out.append(quote)
                        i += 1
                        closed = True
                        break
                    out.append(c)
                    i += 1
                if not closed:
                    raise ValueError("查詢包含未閉合的引號。")
                continue

            if ch == "/" and i + 1 < n and sql[i + 1] == "*":
                end_idx = sql.find("*/", i + 2)
                if end_idx == -1:
                    raise ValueError("查詢包含未閉合的區塊註解。")
                body = sql[i + 2 : end_idx]
                if body.startswith("!") or re.match(r"^[mM]!", body):
                    raise ValueError("基於安全考量，禁止使用 MySQL/MariaDB 可執行註解 (Executable Comments)。")
                out.append(" ")
                i = end_idx + 2
                continue

            if ch == "#":
                newline_idx = sql.find("\n", i + 1)
                if newline_idx == -1:
                    out.append(" ")
                    break
                out.append(" ")
                i = newline_idx
                continue

            if ch == "-" and i + 1 < n and sql[i + 1] == "-":
                if i + 2 < n and not (sql[i + 2].isspace() or ord(sql[i + 2]) <= 32):
                    raise ValueError("基於安全考量，SQL 單行註解 '--' 後方必須緊接空白或換行字元。")
                newline_idx = sql.find("\n", i + 2)
                if newline_idx == -1:
                    out.append(" ")
                    break
                out.append(" ")
                i = newline_idx
                continue

            out.append(ch)
            i += 1

        return "".join(out)

    def validate_select(self, query: str) -> str:
        """驗證 SQL 查詢是否為純唯讀單一 SELECT / WITH / EXPLAIN 語句。"""
        normalized = self.normalize_comments(query).strip()
        if not normalized:
            raise ValueError("查詢語句不可為空。")

        without_trailing_semi = normalized.rstrip("; \t\r\n").strip()
        if not without_trailing_semi:
            raise ValueError("查詢語句不可為空。")
        if ";" in without_trailing_semi:
            raise ValueError("基於安全考量，禁止執行包含分號之多重 SQL 語句。")

        if not _ALLOWED_START_RE.match(without_trailing_semi):
            raise ValueError("基於安全考量，僅允許執行以 SELECT、WITH 或 EXPLAIN SELECT 開頭之唯讀查詢。")

        if _FORBIDDEN_MYSQL_RE.search(without_trailing_semi):
            raise ValueError("基於安全考量，查詢包含禁止執行的寫入、鎖定或系統變更關鍵字。")

        return without_trailing_semi


MYSQL_SQL_GUARD = MysqlSqlGuard()
