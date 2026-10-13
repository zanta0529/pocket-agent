"""自動管家維護模組 (Housekeeping Manager)。

負責定時或手動清理 .agents/logs/ 與 .agents/scratch/ 下的過期日誌、
溢出落盤檔案 (Spillover outputs) 與根目錄舊日誌，防止磁碟空間隨時間耗盡。
具備白名單保護、TTL 過期淘汰、檔案數量上限與容量配額防護。
"""

import logging
import time
from pathlib import Path
from typing import Any

from pocket_agent.system.config import config
from pocket_agent.system.paths import log_dir, scratch_dir, user_dir

log = logging.getLogger("pocket-agent")

DEFAULT_TTL_DAYS = 7
DEFAULT_MAX_FILES = 100
DEFAULT_MAX_SIZE_MB = 50
COOLDOWN_SECONDS = 86400  # 24 小時冷卻期

_PROTECTED_SCRATCH_FILES = {"notes.md", "todo.md"}


class Housekeeper:
    """自動維護管家，負責日誌與暫存檔案的生命週期管理。"""

    def __init__(
        self,
        target_log_dir: Path | None = None,
        target_scratch_dir: Path | None = None,
        cwd: Path | None = None,
        timestamp_file: Path | None = None,
    ):
        self.log_dir = target_log_dir or log_dir()
        self.scratch_dir = target_scratch_dir or scratch_dir()
        self.cwd = cwd or Path.cwd()
        self._custom_timestamp_file = timestamp_file
        self._is_custom_scratch = target_scratch_dir is not None

    @property
    def is_enabled(self) -> bool:
        cfg = config.get("system.housekeeping_enabled")
        return bool(cfg) if cfg is not None else True

    @property
    def ttl_days(self) -> int:
        cfg = config.get("system.housekeeping_ttl_days")
        return int(cfg) if cfg is not None else DEFAULT_TTL_DAYS

    @property
    def max_files(self) -> int:
        cfg = config.get("system.housekeeping_max_files")
        return int(cfg) if cfg is not None else DEFAULT_MAX_FILES

    @property
    def max_size_mb(self) -> int:
        cfg = config.get("system.housekeeping_max_size_mb")
        return int(cfg) if cfg is not None else DEFAULT_MAX_SIZE_MB

    def _get_timestamp_file(self) -> Path:
        if self._custom_timestamp_file is not None:
            return self._custom_timestamp_file
        if self._is_custom_scratch:
            return self.scratch_dir / ".last_housekeeping.timestamp"
        cache_dir = user_dir() / "cache"
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            return cache_dir / "last_housekeeping.timestamp"
        except OSError:
            return self.scratch_dir / ".last_housekeeping.timestamp"

    def is_in_cooldown(self) -> bool:
        """檢查是否仍在 24 小時冷卻期內。"""
        ts_file = self._get_timestamp_file()
        if not ts_file.exists():
            return False
        try:
            last_ts = float(ts_file.read_text(encoding="utf-8").strip())
            return (time.time() - last_ts) < COOLDOWN_SECONDS
        except ValueError, OSError:
            return False

    def mark_executed(self) -> None:
        """更新最後執行時間戳記。"""
        ts_file = self._get_timestamp_file()
        try:
            ts_file.parent.mkdir(parents=True, exist_ok=True)
            ts_file.write_text(str(time.time()), encoding="utf-8")
        except OSError as e:
            log.debug("無法寫入 housekeeping 時間戳記: %s", e)

    def clean_legacy_root_logs(self) -> int:
        """清理專案根目錄下遺留的舊 agent.log。"""
        cleaned = 0
        root_log = self.cwd / "agent.log"
        # 僅當 root_log 不是目前的 log_dir/agent.log 時才清理
        if root_log.exists() and root_log.resolve() != (self.log_dir / "agent.log").resolve():
            try:
                root_log.unlink()
                cleaned += 1
                log.info("已清理根目錄遺留之舊日誌: %s", root_log)
            except OSError as e:
                log.warning("清理根目錄舊日誌失敗: %s", e)
        return cleaned

    def clean_scratch_outputs(
        self,
        ttl_days: int | None = None,
        max_files: int | None = None,
        max_size_mb: int | None = None,
    ) -> tuple[int, int]:
        """清理 .agents/scratch/outputs/ 下的工具溢出落盤日誌。

        回傳 (已刪除檔案數, 釋放位元組數)。
        """
        outputs_dir = self.scratch_dir / "outputs"
        if not outputs_dir.exists():
            return (0, 0)

        effective_ttl = ttl_days if ttl_days is not None else self.ttl_days
        effective_max_files = max_files if max_files is not None else self.max_files
        effective_max_bytes = (max_size_mb if max_size_mb is not None else self.max_size_mb) * 1024 * 1024

        now = time.time()
        ttl_threshold = now - (effective_ttl * 86400)

        deleted_count = 0
        freed_bytes = 0

        # 收集所有非保護檔案資訊: [(path, mtime, size)]
        files_info: list[tuple[Path, float, int]] = []
        for p in outputs_dir.glob("*.log"):
            if not p.is_file():
                continue
            name_lower = p.name.lower()
            if name_lower in _PROTECTED_SCRATCH_FILES or p.suffix == ".keep":
                continue
            try:
                st = p.stat()
                files_info.append((p, st.st_mtime, st.st_size))
            except OSError:
                continue

        # 1. 第一階段：刪除超過 TTL 的過期檔案
        surviving_files: list[tuple[Path, float, int]] = []
        for p, mtime, size in files_info:
            if mtime < ttl_threshold:
                try:
                    p.unlink()
                    deleted_count += 1
                    freed_bytes += size
                except OSError as e:
                    log.warning("刪除過期暫存失敗: %s: %s", p, e)
            else:
                surviving_files.append((p, mtime, size))

        # 依修改時間由舊到新排序 (mtime ascending)
        surviving_files.sort(key=lambda x: x[1])

        # 2. 第二階段：檔案數量上限防護
        while len(surviving_files) > effective_max_files:
            oldest, _, size = surviving_files.pop(0)
            try:
                oldest.unlink()
                deleted_count += 1
                freed_bytes += size
            except OSError as e:
                log.warning("超出檔案數上限刪除失敗: %s: %s", oldest, e)

        # 3. 第三階段：容量配額上限防護 (超出則清理至 80% 安全水位)
        total_size = sum(sz for _, _, sz in surviving_files)
        if total_size > effective_max_bytes:
            target_size = int(effective_max_bytes * 0.8)
            while total_size > target_size and surviving_files:
                oldest, _, size = surviving_files.pop(0)
                try:
                    oldest.unlink()
                    deleted_count += 1
                    freed_bytes += size
                    total_size -= size
                except OSError as e:
                    log.warning("超出容量配額刪除失敗: %s: %s", oldest, e)

        return (deleted_count, freed_bytes)

    def clean_logs(self, ttl_days: int | None = None) -> tuple[int, int]:
        """清理 .agents/logs/ 下的過期備份日誌與舊日誌（保護當前 agent.log）。

        回傳 (已刪除檔案數, 釋放位元組數)。
        """
        if not self.log_dir.exists():
            return (0, 0)

        effective_ttl = ttl_days if ttl_days is not None else (self.ttl_days * 2)  # 日誌允許保留較長
        now = time.time()
        ttl_threshold = now - (effective_ttl * 86400)

        deleted_count = 0
        freed_bytes = 0

        for p in self.log_dir.iterdir():
            if not p.is_file():
                continue
            # 絕對白名單保護：當前作用中的主日誌檔不可刪除
            if p.name == "agent.log":
                continue
            try:
                st = p.stat()
                if st.st_mtime < ttl_threshold:
                    p.unlink()
                    deleted_count += 1
                    freed_bytes += st.st_size
            except OSError as e:
                log.warning("刪除過期日誌失敗: %s: %s", p, e)

        return (deleted_count, freed_bytes)

    def run_cleanup(self, force: bool = False) -> dict[str, Any]:
        """執行完整 Housekeeping 維護。

        若 force 為 False 且在冷卻期內，則略過。
        """
        if not force and not self.is_enabled:
            return {"status": "disabled", "cleaned_files": 0, "freed_bytes": 0}

        if not force and self.is_in_cooldown():
            return {"status": "cooldown", "cleaned_files": 0, "freed_bytes": 0}

        cleaned_root = self.clean_legacy_root_logs()
        scratch_count, scratch_bytes = self.clean_scratch_outputs()
        log_count, log_bytes = self.clean_logs()

        total_files = cleaned_root + scratch_count + log_count
        total_bytes = scratch_bytes + log_bytes

        self.mark_executed()

        res = {
            "status": "success",
            "cleaned_files": total_files,
            "freed_bytes": total_bytes,
            "freed_mb": round(total_bytes / (1024 * 1024), 2),
        }
        if total_files > 0:
            log.info("Housekeeping 完成：清理 %d 個檔案，釋放 %.2f MB", total_files, res["freed_mb"])
        return res


# 全域單例
default_housekeeper = Housekeeper()
