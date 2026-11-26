"""門檻自動啟用之工作區 SQLite FTS5 Trigram 增量全文索引（Stage 1 候選預篩）。"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pocket_agent.core.cancellation import check_cancelled
from pocket_agent.system.constants import (
    DEFAULT_HOUSEKEEPING_TTL_DAYS,
    SEARCH_FTS_MAX_DB_SIZE_MB,
    SEARCH_FTS_MAX_FILE_BYTES,
    SEARCH_FTS_MAX_INDEXED_FILES,
    SEARCH_FTS_SYNC_BUDGET_SEC,
)
from pocket_agent.system.paths import user_dir

log = logging.getLogger("pocket-agent")

_META_UPSERT_SQL = (
    "INSERT OR REPLACE INTO file_meta "
    "(path, root, rel_path, name, mtime, size, is_text, encoding, "
    "indexed_complete, last_seen) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


@dataclass(frozen=True)
class ScannedFile:
    """工作區掃描結果的單一檔案中繼資料。"""

    path: Path
    root: Path
    display_path: str
    mtime: float
    size: int


class SearchCandidateIndex(Protocol):
    """全文檢索候選預篩抽象介面（Adapter），隔離儲存實作細節。"""

    def sync_and_query_candidates(
        self,
        scanned_files: list[ScannedFile],
        roots: list[Path],
        query: str,
        read_text_fn: Callable[[Path], tuple[str | None, str | None]],
    ) -> list[ScannedFile] | None: ...


def _unlink_db_with_sidecars(db_path: Path) -> int:
    """刪除 SQLite 主檔與 -wal/-shm 側車檔，回傳釋放位元組數。"""
    freed = 0
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{db_path}{suffix}") if suffix else db_path
        if not candidate.is_file():
            continue
        try:
            freed += candidate.stat().st_size
            candidate.unlink()
        except OSError:
            continue
    return freed


class WorkspaceSearchIndex:
    """SQLite FTS5 Trigram 增量索引管理器，支援多工作區與超時／配額降級。"""

    def __init__(self, db_path: Path | None = None) -> None:
        self._explicit_db_path: Path | None = db_path
        self._trigram_unavailable: bool = False
        self._lock = threading.RLock()

    @property
    def db_path(self) -> Path:
        if self._explicit_db_path is not None:
            return self._explicit_db_path
        return user_dir() / "db" / "workspace_search_index.db"

    @db_path.setter
    def db_path(self, value: Path | None) -> None:
        self._explicit_db_path = value
        self._trigram_unavailable = False

    def _connect(self, target_db: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(target_db)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS file_meta (
                path TEXT PRIMARY KEY,
                root TEXT NOT NULL,
                rel_path TEXT NOT NULL,
                name TEXT NOT NULL,
                mtime REAL NOT NULL,
                size INTEGER NOT NULL,
                is_text INTEGER NOT NULL,
                encoding TEXT NOT NULL,
                indexed_complete INTEGER NOT NULL,
                last_seen REAL NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_file_meta_root ON file_meta(root)")
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS file_content_fts "
            "USING fts5(path UNINDEXED, content, tokenize='trigram')"
        )

    def _db_total_bytes(self, target_db: Path) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{target_db}{suffix}") if suffix else target_db
            if candidate.is_file():
                try:
                    total += candidate.stat().st_size
                except OSError:
                    continue
        return total

    @staticmethod
    def _delete_paths(conn: sqlite3.Connection, paths_to_del: list[tuple[str]]) -> None:
        if not paths_to_del:
            return
        conn.executemany("DELETE FROM file_content_fts WHERE path = ?", paths_to_del)
        conn.executemany("DELETE FROM file_meta WHERE path = ?", paths_to_del)

    def sync_and_query_candidates(
        self,
        scanned_files: list[ScannedFile],
        roots: list[Path],
        query: str,
        read_text_fn: Callable[[Path], tuple[str | None, str | None]],
        *,
        max_file_bytes: int = SEARCH_FTS_MAX_FILE_BYTES,
        max_indexed_files: int = SEARCH_FTS_MAX_INDEXED_FILES,
        max_db_size_mb: int = SEARCH_FTS_MAX_DB_SIZE_MB,
        sync_budget_sec: float = SEARCH_FTS_SYNC_BUDGET_SEC,
    ) -> list[ScannedFile] | None:
        """增量同步並以 Trigram 預篩候選檔案；不適用或異常時回傳 None 降級 Stage 2。"""
        stripped = query.strip()
        if len(stripped) < 3 or self._trigram_unavailable or not roots:
            return None

        root_strs = [str(r) for r in roots]
        max_db_bytes = max(1, int(max_db_size_mb)) * 1024 * 1024
        with self._lock:
            target_db = self.db_path
            if target_db.is_file() and self._db_total_bytes(target_db) > max_db_bytes:
                self.prune_stale_entries(max_size_mb=max_db_size_mb, vacuum=True)
                if target_db.is_file() and self._db_total_bytes(target_db) > max_db_bytes:
                    return None

            try:
                target_db.parent.mkdir(parents=True, exist_ok=True)
                with self._connect(target_db) as conn:
                    self._ensure_schema(conn)
                    placeholders = ",".join("?" for _ in root_strs)
                    existing_rows = conn.execute(
                        "SELECT path, mtime, size, is_text, indexed_complete "
                        f"FROM file_meta WHERE root IN ({placeholders})",
                        root_strs,
                    ).fetchall()
                    existing_map = {r[0]: (float(r[1]), int(r[2]), int(r[3]), int(r[4])) for r in existing_rows}

                    scanned_map: dict[str, ScannedFile] = {str(sf.path): sf for sf in scanned_files}
                    stale_paths = [(p,) for p in existing_map if p not in scanned_map]
                    self._delete_paths(conn, stale_paths)

                    total_indexed = conn.execute(
                        "SELECT COUNT(*) FROM file_meta WHERE is_text = 1 AND indexed_complete = 1"
                    ).fetchone()[0]
                    now_ts = time.time()
                    deadline = time.monotonic() + max(0.1, float(sync_budget_sec))
                    seen_to_touch: list[tuple[float, str]] = []

                    for str_path, sf in scanned_map.items():
                        check_cancelled()
                        prev = existing_map.get(str_path)
                        if (
                            prev is not None
                            and abs(prev[0] - sf.mtime) < 0.001
                            and prev[1] == sf.size
                            and (prev[3] == 1 or sf.size > max_file_bytes)
                        ):
                            seen_to_touch.append((now_ts, str_path))
                            continue

                        was_counted = prev is not None and prev[2] == 1 and prev[3] == 1
                        over_limit = (
                            sf.size > max_file_bytes
                            or time.monotonic() > deadline
                            or (total_indexed >= max_indexed_files and not was_counted)
                        )
                        conn.execute("DELETE FROM file_content_fts WHERE path = ?", (str_path,))
                        if over_limit:
                            conn.execute(
                                _META_UPSERT_SQL,
                                (
                                    str_path,
                                    str(sf.root),
                                    sf.display_path,
                                    sf.path.name,
                                    sf.mtime,
                                    sf.size,
                                    1,
                                    "",
                                    0,
                                    now_ts,
                                ),
                            )
                            if was_counted:
                                total_indexed = max(0, total_indexed - 1)
                            continue

                        text, enc = read_text_fn(sf.path)
                        if text is None:
                            conn.execute(
                                _META_UPSERT_SQL,
                                (
                                    str_path,
                                    str(sf.root),
                                    sf.display_path,
                                    sf.path.name,
                                    sf.mtime,
                                    sf.size,
                                    0,
                                    "",
                                    1,
                                    now_ts,
                                ),
                            )
                            if was_counted:
                                total_indexed = max(0, total_indexed - 1)
                            continue

                        conn.execute("INSERT INTO file_content_fts (path, content) VALUES (?, ?)", (str_path, text))
                        conn.execute(
                            _META_UPSERT_SQL,
                            (
                                str_path,
                                str(sf.root),
                                sf.display_path,
                                sf.path.name,
                                sf.mtime,
                                sf.size,
                                1,
                                enc or "utf-8",
                                1,
                                now_ts,
                            ),
                        )
                        if not was_counted:
                            total_indexed += 1

                    if seen_to_touch:
                        conn.executemany("UPDATE file_meta SET last_seen = ? WHERE path = ?", seen_to_touch)

                    fts_phrase = '"' + stripped.replace('"', '""') + '"'
                    fts_rows = conn.execute(
                        "SELECT path FROM file_content_fts WHERE file_content_fts MATCH ?",
                        (fts_phrase,),
                    ).fetchall()
                    incomplete_rows = conn.execute(
                        f"SELECT path FROM file_meta "
                        f"WHERE root IN ({placeholders}) AND is_text = 1 AND indexed_complete = 0",
                        root_strs,
                    ).fetchall()
                    conn.commit()

                candidate_set = {r[0] for r in fts_rows} | {r[0] for r in incomplete_rows}
                return [sf for sf in scanned_files if str(sf.path) in candidate_set]
            except sqlite3.OperationalError as exc:
                msg = str(exc).lower()
                if "trigram" in msg or "no such module" in msg:
                    self._trigram_unavailable = True
                    log.warning("SQLite 環境不支援 FTS5 trigram，降級為直接掃描: %s", exc)
                else:
                    log.warning("工作區 FTS5 索引查詢失敗，降級為直接掃描: %s", exc)
                return None
            except sqlite3.Error as exc:
                log.warning("工作區 FTS5 索引異常，降級為直接掃描: %s", exc)
                return None

    def prune_stale_rows(self) -> int:
        """清除磁碟已不存在之孤兒路徑列，回傳刪除列數。"""
        with self._lock:
            target_db = self.db_path
            if not target_db.is_file():
                return 0
            try:
                with self._connect(target_db) as conn:
                    self._ensure_schema(conn)
                    rows = conn.execute("SELECT path FROM file_meta").fetchall()
                    stale = [(r[0],) for r in rows if not Path(r[0]).exists()]
                    self._delete_paths(conn, stale)
                    if stale:
                        conn.commit()
                    return len(stale)
            except sqlite3.Error as exc:
                log.warning("清理搜尋索引孤兒紀錄失敗: %s", exc)
                return 0

    def prune_stale_entries(
        self,
        *,
        ttl_days: int | None = None,
        max_size_mb: int | None = None,
        vacuum: bool = False,
    ) -> tuple[int, int]:
        """依孤兒路徑、TTL 與容量配額清理索引 DB，回傳 (清理檔案數, 釋放位元組數)。"""
        with self._lock:
            target_db = self.db_path
            if not target_db.is_file():
                return (0, 0)

            before_bytes = self._db_total_bytes(target_db)
            eff_ttl = max(1, int(ttl_days)) if ttl_days is not None else DEFAULT_HOUSEKEEPING_TTL_DAYS
            ttl_cutoff = time.time() - (eff_ttl * 86400)
            eff_max_mb = max(1, int(max_size_mb)) if max_size_mb is not None else SEARCH_FTS_MAX_DB_SIZE_MB
            eff_max_bytes = eff_max_mb * 1024 * 1024

            removed_rows = 0
            try:
                with self._connect(target_db) as conn:
                    self._ensure_schema(conn)
                    rows = conn.execute("SELECT path, last_seen FROM file_meta").fetchall()
                    to_delete = [(r[0],) for r in rows if float(r[1]) < ttl_cutoff or not Path(r[0]).exists()]
                    self._delete_paths(conn, to_delete)
                    removed_rows = len(to_delete)
                    if removed_rows:
                        conn.commit()
                    if vacuum or removed_rows >= 100:
                        conn.execute("VACUUM")
                        conn.commit()
            except sqlite3.Error as exc:
                log.warning("清理搜尋索引失敗，將重建索引庫: %s", exc)
                freed = _unlink_db_with_sidecars(target_db)
                return (1 if freed > 0 else 0, freed)

            if self._db_total_bytes(target_db) > eff_max_bytes:
                freed = _unlink_db_with_sidecars(target_db)
                return (1 if freed > 0 else 0, freed)

            freed_bytes = max(0, before_bytes - self._db_total_bytes(target_db))
            return (1 if (removed_rows > 0 and freed_bytes > 0) else 0, freed_bytes)


default_search_index = WorkspaceSearchIndex()
