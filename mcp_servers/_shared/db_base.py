"""唯讀資料庫 MCP 伺服器共用抽象層與工具（Adapter + Strategy Pattern）。"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from mcp_servers._shared.bootstrap import CONSTANTS, t
from mcp_servers._shared.redaction import (
    DEFAULT_REDACTION_POLICY,
    SENSITIVE_COL_RE,
    RedactionPolicy,
)


class SqlGuardStrategy(ABC):
    """SQL 方言唯讀語法驗證策略介面（由 SQLite / MySQL / MSSQL 各自封裝方言差異）。"""

    @abstractmethod
    def normalize_comments(self, sql: str) -> str:
        """正規化並移除註解，同時保留字面值與識別字邊界。"""

    @abstractmethod
    def validate_select(self, query: str) -> str:
        """驗證查詢為單一唯讀 SELECT / WITH / EXPLAIN 語句，違規時拋出 ValueError。"""


class ReadOnlyDatabaseAdapter(ABC):
    """唯讀資料庫連線與串流游標轉接器契約（Adapter Pattern）。"""

    @abstractmethod
    def connect(self, **params: Any) -> Any:
        """建立強制唯讀隔離的資料庫連線。"""

    def open_streaming_cursor(self, conn: Any) -> Any:
        """開啟查詢游標；具 unbuffered 串流能力的驅動可覆寫此方法。"""
        return conn.cursor()

    def close_safely(self, conn: Any) -> None:
        """強制 rollback 並安全關閉連線，絕不 commit。"""
        if conn is None:
            return
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

    def abort_connection(self, conn: Any, cursor: Any = None) -> None:
        """提早截斷或讀取異常時中止連線；預設委派 close_safely，unbuffered 驅動可覆寫。"""
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        self.close_safely(conn)


@dataclass(frozen=True)
class QueryStreamResult:
    """分批串流查詢格式化結果。"""

    output: str
    shown_rows: int
    total_rows: int
    truncated: bool
    stream_completed: bool


def format_cell_value(
    val: object,
    *,
    is_sensitive: bool = False,
    redaction_policy: RedactionPolicy = DEFAULT_REDACTION_POLICY,
) -> str:
    """格式化單一欄位值：敏感欄強制遮罩、None 轉為 NULL、多行文字遮罩後單行化。"""
    if is_sensitive:
        return "[REDACTED]"
    if val is None:
        return "NULL"
    redacted = redaction_policy.redact(str(val))
    return redacted.replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n")


def stream_query_results(
    cursor: Any,
    *,
    max_chars: int = CONSTANTS.max_output_chars,
    batch_size: int = 100,
    redaction_policy: RedactionPolicy = DEFAULT_REDACTION_POLICY,
) -> QueryStreamResult:
    """以 fetchmany 分批串流提取查詢結果，防範大表 OOM 並自動遮罩敏感欄位。"""
    if not cursor.description:
        return QueryStreamResult(
            output=t("mcp_shared.db_no_columns"),
            shown_rows=0,
            total_rows=0,
            truncated=False,
            stream_completed=True,
        )

    col_names = [str(desc[0]) for desc in cursor.description]
    sensitive_indices = {idx for idx, name in enumerate(col_names) if SENSITIVE_COL_RE.search(name)}

    lines = [", ".join(col_names)]
    chars_count = len(lines[0]) + 1
    total_rows = 0
    truncated = False
    stream_completed = False

    while True:
        batch = cursor.fetchmany(batch_size)
        if not batch:
            stream_completed = True
            break
        for row in batch:
            total_rows += 1
            formatted_vals = [
                format_cell_value(
                    val,
                    is_sensitive=(idx in sensitive_indices),
                    redaction_policy=redaction_policy,
                )
                for idx, val in enumerate(row)
            ]
            row_str = ", ".join(formatted_vals)
            if chars_count + len(row_str) + 1 > max_chars:
                truncated = True
                break
            lines.append(row_str)
            chars_count += len(row_str) + 1
        if truncated:
            break

    if total_rows == 0:
        return QueryStreamResult(
            output=t("mcp_shared.db_empty_result"),
            shown_rows=0,
            total_rows=0,
            truncated=False,
            stream_completed=True,
        )

    shown_rows = len(lines) - 1
    output = "\n".join(lines)
    if truncated:
        output += t("mcp_shared.db_query_truncated", max_chars=max_chars, shown_rows=shown_rows)
    return QueryStreamResult(
        output=output,
        shown_rows=shown_rows,
        total_rows=total_rows,
        truncated=truncated,
        stream_completed=stream_completed,
    )


def format_bytes(num_bytes: int | float | None) -> str:
    """將位元組數轉換為易讀之容量單位字串（B / KB / MB / GB）。"""
    if num_bytes is None:
        return "0 B"
    try:
        val = max(0.0, float(num_bytes))
    except TypeError, ValueError:
        return "0 B"
    if val < 1024:
        return f"{int(val)} B"
    if val < 1024 * 1024:
        return f"{val / 1024:.2f} KB"
    if val < 1024 * 1024 * 1024:
        return f"{val / (1024 * 1024):.2f} MB"
    return f"{val / (1024 * 1024 * 1024):.2f} GB"


def find_redundant_prefix_indexes(
    index_map: dict[str, tuple[bool, list[str]]],
) -> list[str]:
    """偵測單一資料表內具有相同前導欄位序列的冗餘／重複索引。"""
    items = [(name, is_uniq, cols) for name, (is_uniq, cols) in index_map.items() if cols]
    findings: list[str] = []
    seen_pairs: set[tuple[str, str]] = set()
    for i, (name_a, uniq_a, cols_a) in enumerate(items):
        norm_a = [c.lower() for c in cols_a]
        for j, (name_b, uniq_b, cols_b) in enumerate(items):
            if i == j:
                continue
            norm_b = [c.lower() for c in cols_b]
            if len(norm_a) > len(norm_b) or norm_b[: len(norm_a)] != norm_a:
                continue
            if len(norm_a) == len(norm_b):
                pair_key = (min(name_a, name_b), max(name_a, name_b))
                if pair_key in seen_pairs:
                    continue
                if uniq_a and not uniq_b:
                    continue
                seen_pairs.add(pair_key)
                findings.append(
                    t(
                        "mcp_shared.db_redundant_exact",
                        name_a=name_a,
                        cols_a=", ".join(cols_a),
                        name_b=name_b,
                        cols_b=", ".join(cols_b),
                    )
                )
            elif not uniq_a:
                findings.append(
                    t(
                        "mcp_shared.db_redundant_prefix",
                        name_a=name_a,
                        cols_a=", ".join(cols_a),
                        name_b=name_b,
                        cols_b=", ".join(cols_b),
                    )
                )
    return findings


def truncate_health_report(
    lines: list[str],
    *,
    max_chars: int = CONSTANTS.max_output_chars,
    redaction_policy: RedactionPolicy = DEFAULT_REDACTION_POLICY,
) -> str:
    """將健康檢查報告合併並套用機密遮罩與字元上限截斷保護。"""
    text = redaction_policy.redact("\n".join(lines))
    if len(text) <= max_chars:
        return text
    cut = redaction_policy.safe_cut_index(text, max_chars)
    clipped = text[:cut].rsplit("\n", 1)[0]
    return clipped + t("mcp_shared.db_health_truncated", max_chars=max_chars)
