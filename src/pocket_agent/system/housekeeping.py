"""自動管家維護模組 (Housekeeping Manager)。

負責定時或手動清理 `~/.pocket-agent/workspaces/<slug>/logs/` 與 `scratch/` 下的過期日誌、
溢出落盤檔案 (Spillover outputs)、截圖暫存與根目錄舊日誌，防止磁碟空間隨時間耗盡。
具備白名單保護、TTL 過期淘汰、檔案數量上限與容量配額防護。
"""

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from pocket_agent.system.config import config
from pocket_agent.system.constants import (
    DEFAULT_HOUSEKEEPING_ACTIVE_BACKUP_TTL_MULTIPLIER,
    DEFAULT_HOUSEKEEPING_BACKUP_MAX_SIZE_MB,
    DEFAULT_HOUSEKEEPING_COOLDOWN_SECONDS,
    DEFAULT_HOUSEKEEPING_LOG_TTL_MULTIPLIER,
    DEFAULT_HOUSEKEEPING_MAX_FILES,
    DEFAULT_HOUSEKEEPING_MAX_SIZE_MB,
    DEFAULT_HOUSEKEEPING_TTL_DAYS,
)
from pocket_agent.system.paths import log_dir, scratch_dir, user_dir

log = logging.getLogger("pocket-agent")

# 向後相容別名 (統一維護於 constants.py)
DEFAULT_TTL_DAYS = DEFAULT_HOUSEKEEPING_TTL_DAYS
DEFAULT_MAX_FILES = DEFAULT_HOUSEKEEPING_MAX_FILES
DEFAULT_MAX_SIZE_MB = DEFAULT_HOUSEKEEPING_MAX_SIZE_MB
COOLDOWN_SECONDS = DEFAULT_HOUSEKEEPING_COOLDOWN_SECONDS

_PROTECTED_SCRATCH_FILES = {"notes.md", "todo.md"}
_TRUE_STRINGS = {"1", "true", "yes", "on"}
_FALSE_STRINGS = {"0", "false", "no", "off"}


def _parse_bool_config(val: Any, default: bool = True) -> bool:
    """解析設定值為布林值，正確處理環境變數常見的文字型態。"""
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        normalized = val.strip().lower()
        if normalized in _TRUE_STRINGS:
            return True
        if normalized in _FALSE_STRINGS:
            return False
        log.warning("無效的布林設定值 '%s'，退回預設值 %s", val, default)
        return default
    return bool(val)


def _parse_int_config(val: Any, default: int, min_value: int = 1, name: str = "") -> int:
    """解析設定值為整數，無效時退回預設值並套用下限保護。"""
    if val is None:
        return default
    try:
        parsed = int(val)
    except ValueError, TypeError:
        log.warning("無效的整數設定值 %s='%s'，退回預設值 %d", name or "config", val, default)
        return default
    if parsed < min_value:
        log.warning("設定值 %s=%d 低於下限 %d，已自動調整為 %d", name or "config", parsed, min_value, min_value)
        return min_value
    return parsed


class Housekeeper:
    """自動維護管家，負責日誌與暫存檔案的生命週期管理。"""

    def __init__(
        self,
        target_log_dir: Path | None = None,
        target_scratch_dir: Path | None = None,
        cwd: Path | None = None,
        timestamp_file: Path | None = None,
    ):
        self._custom_log_dir = target_log_dir
        self._custom_scratch_dir = target_scratch_dir
        self._custom_cwd = cwd
        self._custom_timestamp_file = timestamp_file
        self._is_custom_scratch = target_scratch_dir is not None
        self._lock = threading.Lock()

    @property
    def log_dir(self) -> Path:
        return self._custom_log_dir if self._custom_log_dir is not None else log_dir()

    @property
    def scratch_dir(self) -> Path:
        return self._custom_scratch_dir if self._custom_scratch_dir is not None else scratch_dir()

    @property
    def cwd(self) -> Path:
        return self._custom_cwd if self._custom_cwd is not None else Path.cwd()

    @property
    def is_enabled(self) -> bool:
        return _parse_bool_config(config.get("system.housekeeping_enabled"), True)

    @property
    def ttl_days(self) -> int:
        return _parse_int_config(
            config.get("system.housekeeping_ttl_days"),
            DEFAULT_TTL_DAYS,
            min_value=1,
            name="housekeeping_ttl_days",
        )

    @property
    def max_files(self) -> int:
        return _parse_int_config(
            config.get("system.housekeeping_max_files"),
            DEFAULT_MAX_FILES,
            min_value=1,
            name="housekeeping_max_files",
        )

    @property
    def max_size_mb(self) -> int:
        return _parse_int_config(
            config.get("system.housekeeping_max_size_mb"),
            DEFAULT_MAX_SIZE_MB,
            min_value=1,
            name="housekeeping_max_size_mb",
        )

    @property
    def backup_max_size_mb(self) -> int:
        return _parse_int_config(
            config.get("system.housekeeping_backup_max_size_mb"),
            DEFAULT_HOUSEKEEPING_BACKUP_MAX_SIZE_MB,
            min_value=1,
            name="housekeeping_backup_max_size_mb",
        )

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

    def _clean_legacy_root_logs_with_bytes(self) -> tuple[int, int]:
        """清理專案根目錄下遺留的舊 agent.log，回傳 (刪除數, 釋放位元組數)。"""
        cleaned = 0
        freed_bytes = 0
        root_log = self.cwd / "agent.log"
        # 僅當 root_log 不是目前的 log_dir/agent.log 時才清理
        if root_log.exists() and root_log.resolve() != (self.log_dir / "agent.log").resolve():
            try:
                size = root_log.stat().st_size
                root_log.unlink()
                cleaned += 1
                freed_bytes += size
                log.info("已清理根目錄遺留之舊日誌: %s", root_log)
            except OSError as e:
                log.warning("清理根目錄舊日誌失敗: %s", e)
        return cleaned, freed_bytes

    def clean_legacy_root_logs(self) -> int:
        """清理專案根目錄下遺留的舊 agent.log。"""
        cleaned, _ = self._clean_legacy_root_logs_with_bytes()
        return cleaned

    def _clean_dir_with_quota(
        self,
        target_dir: Path,
        pattern: str,
        *,
        protected_names: set[str] | None = None,
        protect_keep_suffix: bool = False,
        ttl_days: int | None = None,
        max_files: int | None = None,
        max_size_mb: int | None = None,
        label: str = "暫存",
    ) -> tuple[int, int]:
        """依 TTL、檔案數上限與 80% 容量配額清理指定目錄，回傳 (刪除數, 釋放位元組數)。"""
        if not target_dir.exists():
            return (0, 0)

        effective_ttl = ttl_days if ttl_days is not None else self.ttl_days
        effective_max_files = max_files if max_files is not None else self.max_files
        effective_max_bytes = (max_size_mb if max_size_mb is not None else self.max_size_mb) * 1024 * 1024

        now = time.time()
        ttl_threshold = now - (effective_ttl * 86400)

        deleted_count = 0
        freed_bytes = 0

        files_info: list[tuple[Path, float, int]] = []
        for p in target_dir.glob(pattern):
            if not p.is_file():
                continue
            if protected_names and p.name.lower() in protected_names:
                continue
            if protect_keep_suffix and p.suffix == ".keep":
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
                    log.warning("刪除過期%s失敗: %s: %s", label, p, e)
            else:
                surviving_files.append((p, mtime, size))

        surviving_files.sort(key=lambda x: x[1])

        # 2. 第二階段：檔案數量上限防護
        while len(surviving_files) > effective_max_files:
            oldest, _, size = surviving_files.pop(0)
            try:
                oldest.unlink()
                deleted_count += 1
                freed_bytes += size
            except OSError as e:
                log.warning("超出%s數量上限刪除失敗: %s: %s", label, oldest, e)

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
                    log.warning("超出%s容量配額刪除失敗: %s: %s", label, oldest, e)

        return (deleted_count, freed_bytes)

    def clean_scratch_outputs(
        self,
        ttl_days: int | None = None,
        max_files: int | None = None,
        max_size_mb: int | None = None,
    ) -> tuple[int, int]:
        """清理 `scratch/outputs/` 下的工具溢出落盤日誌。

        回傳 (已刪除檔案數, 釋放位元組數)。
        """
        return self._clean_dir_with_quota(
            self.scratch_dir / "outputs",
            "*.log",
            protected_names=_PROTECTED_SCRATCH_FILES,
            protect_keep_suffix=True,
            ttl_days=ttl_days,
            max_files=max_files,
            max_size_mb=max_size_mb,
            label="暫存",
        )

    def clean_scratch_clipboard(
        self,
        ttl_days: int | None = None,
        max_files: int | None = None,
        max_size_mb: int | None = None,
    ) -> tuple[int, int]:
        """清理 `scratch/clipboard/` 下的剪貼簿暫存截圖檔案。

        回傳 (已刪除檔案數, 釋放位元組數)。
        """
        return self._clean_dir_with_quota(
            self.scratch_dir / "clipboard",
            "screenshot_*.png",
            ttl_days=ttl_days,
            max_files=max_files,
            max_size_mb=max_size_mb,
            label="截圖暫存",
        )

    def clean_scratch_checkpoints(self, ttl_days: int | None = None) -> tuple[int, int]:
        """清理 `scratch/` 下超過 TTL 的研究檢查點暫存檔（保護 notes.md／todo.md）。"""
        ckpt = self.scratch_dir / "research_checkpoint.md"
        if not ckpt.is_file():
            return (0, 0)
        effective_ttl = ttl_days if ttl_days is not None else self.ttl_days
        ttl_threshold = time.time() - (effective_ttl * 86400)
        try:
            st = ckpt.stat()
            if st.st_mtime < ttl_threshold:
                ckpt.unlink()
                return (1, st.st_size)
        except OSError as e:
            log.warning("刪除過期研究檢查點失敗: %s: %s", ckpt, e)
        return (0, 0)

    def clean_logs(self, ttl_days: int | None = None) -> tuple[int, int]:
        """清理 `logs/` 下的過期舊日誌（保護當日按日日誌與向下相容的 agent.log）。

        回傳 (已刪除檔案數, 釋放位元組數)。
        """
        if not self.log_dir.exists():
            return (0, 0)

        base_ttl = ttl_days if ttl_days is not None else self.ttl_days
        effective_ttl = base_ttl * DEFAULT_HOUSEKEEPING_LOG_TTL_MULTIPLIER
        now = time.time()
        ttl_threshold = now - (effective_ttl * 86400)
        today_log_name = f"agent_{time.strftime('%Y-%m-%d')}.log"

        deleted_count = 0
        freed_bytes = 0

        for p in self.log_dir.iterdir():
            if not p.is_file():
                continue
            # 絕對白名單保護：當日作用中按日日誌（含同日滾動檔）與舊版 agent.log 不可刪除
            if p.name == "agent.log" or p.name == today_log_name or p.name.startswith(f"{today_log_name}."):
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

    def clean_backups(
        self,
        ttl_days: int | None = None,
        active_ttl_days: int | None = None,
        max_size_mb: int | None = None,
        backup_dir: Path | None = None,
    ) -> tuple[int, int]:
        """分級清理 `~/.pocket-agent/backups/` 時光機備份。

        - 孤兒備份（未被 latest_backup_map.json 引用）：依 ttl_days（預設 7 天）清理。
        - 活躍備份（仍在 latest_backup_map.json 中）：依 active_ttl_days（預設 28 天）清理。
        - 目錄總容量超過 max_size_mb（預設 200 MB）時，依 mtime 由舊至新淘汰至 80% 安全水位，
          並優先淘汰孤兒備份；若有活躍備份被刪除，同步更新 latest_backup_map.json。
        """
        target_dir = backup_dir if backup_dir is not None else (user_dir() / "backups")
        if not target_dir.is_dir():
            return (0, 0)

        from pocket_agent.tools.fs import _BACKUP_MAP_LOCK, _write_backup_map_atomic

        base_ttl = ttl_days if ttl_days is not None else self.ttl_days
        eff_orphan_ttl = max(1, base_ttl)
        eff_active_ttl = (
            max(1, active_ttl_days)
            if active_ttl_days is not None
            else (eff_orphan_ttl * DEFAULT_HOUSEKEEPING_ACTIVE_BACKUP_TTL_MULTIPLIER)
        )
        eff_max_bytes = (max_size_mb if max_size_mb is not None else self.backup_max_size_mb) * 1024 * 1024

        now = time.time()
        orphan_threshold = now - (eff_orphan_ttl * 86400)
        active_threshold = now - (eff_active_ttl * 86400)

        mapping_file = target_dir / "latest_backup_map.json"
        deleted_count = 0
        freed_bytes = 0

        with _BACKUP_MAP_LOCK:
            mapping: dict[str, str] = {}
            if mapping_file.is_file():
                try:
                    loaded = json.loads(mapping_file.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        mapping = {str(k): str(v) for k, v in loaded.items()}
                except Exception as e:
                    log.warning("讀取 latest_backup_map.json 失敗，將保守跳過備份清理: %s", e)
                    return (0, 0)

            active_paths: set[Path] = set()
            for v in mapping.values():
                try:
                    active_paths.add(Path(v).resolve())
                except OSError:
                    continue

            surviving_orphans: list[tuple[Path, float, int]] = []
            surviving_actives: list[tuple[Path, float, int]] = []
            removed_resolved: set[Path] = set()

            for p in target_dir.iterdir():
                if not p.is_file():
                    continue
                if p.name == "latest_backup_map.json" or p.name.startswith(".latest_backup_map_"):
                    continue
                try:
                    resolved_p = p.resolve()
                    st = p.stat()
                except OSError:
                    continue

                is_active = resolved_p in active_paths
                threshold = active_threshold if is_active else orphan_threshold
                if st.st_mtime < threshold:
                    try:
                        p.unlink()
                        deleted_count += 1
                        freed_bytes += st.st_size
                        if is_active:
                            removed_resolved.add(resolved_p)
                    except OSError as e:
                        log.warning("刪除過期時光機備份失敗: %s: %s", p, e)
                else:
                    entry = (p, st.st_mtime, st.st_size)
                    if is_active:
                        surviving_actives.append(entry)
                    else:
                        surviving_orphans.append(entry)

            # 容量配額防護：超出上限時清理至 80% 安全水位，優先淘汰孤兒備份
            total_size = sum(sz for _, _, sz in surviving_orphans) + sum(sz for _, _, sz in surviving_actives)
            if total_size > eff_max_bytes:
                target_size = int(eff_max_bytes * 0.8)
                surviving_orphans.sort(key=lambda x: x[1])
                surviving_actives.sort(key=lambda x: x[1])
                evict_queue = [(False, *item) for item in surviving_orphans] + [
                    (True, *item) for item in surviving_actives
                ]
                for is_active, p, _, size in evict_queue:
                    if total_size <= target_size:
                        break
                    try:
                        resolved_p = p.resolve()
                        p.unlink()
                        deleted_count += 1
                        freed_bytes += size
                        total_size -= size
                        if is_active:
                            removed_resolved.add(resolved_p)
                    except OSError as e:
                        log.warning("超出備份容量配額刪除失敗: %s: %s", p, e)

            # 同步清理已不存在或被淘汰的活躍映射
            if mapping:
                updated_mapping: dict[str, str] = {}
                changed = False
                for orig_k, bkup_v in mapping.items():
                    try:
                        resolved_v = Path(bkup_v).resolve()
                    except OSError:
                        changed = True
                        continue
                    if resolved_v in removed_resolved or not Path(bkup_v).exists():
                        changed = True
                        continue
                    updated_mapping[orig_k] = bkup_v
                if changed:
                    try:
                        _write_backup_map_atomic(mapping_file, updated_mapping)
                    except OSError as e:
                        log.warning("更新 latest_backup_map.json 失敗: %s", e)

        return (deleted_count, freed_bytes)

    def clean_all_workspaces(
        self,
        ttl_days: int | None = None,
        max_files: int | None = None,
        max_size_mb: int | None = None,
    ) -> tuple[int, int]:
        """清理全域 ~/.pocket-agent/workspaces/ 底下所有其他工作區的過期暫存與日誌。"""
        workspaces_root = user_dir() / "workspaces"
        if not workspaces_root.exists():
            return (0, 0)

        total_cleaned = 0
        total_freed = 0
        current_ws = None
        try:
            current_ws = self.scratch_dir.parent.resolve()
        except Exception:
            pass

        for ws in workspaces_root.iterdir():
            if not ws.is_dir():
                continue
            if current_ws and ws.resolve() == current_ws:
                continue

            ws_scratch = ws / "scratch"
            ws_log = ws / "logs"

            sub_hk = Housekeeper(
                target_log_dir=ws_log,
                target_scratch_dir=ws_scratch,
                cwd=self.cwd,
            )
            c1, b1 = sub_hk.clean_scratch_outputs(ttl_days, max_files, max_size_mb)
            c2, b2 = sub_hk.clean_scratch_clipboard(ttl_days, max_files, max_size_mb)
            c3, b3 = sub_hk.clean_logs(ttl_days)
            c4, b4 = sub_hk.clean_scratch_checkpoints(ttl_days)
            total_cleaned += c1 + c2 + c3 + c4
            total_freed += b1 + b2 + b3 + b4

        return (total_cleaned, total_freed)

    def _clean_fallback_dirs(
        self,
        ttl_days: int | None = None,
        max_files: int | None = None,
        max_size_mb: int | None = None,
    ) -> tuple[int, int]:
        """清理 paths.py 在 OSError 時的 fallback 全域目錄（~/.pocket-agent/logs 與 scratch）。"""
        fallback_log = user_dir() / "logs"
        fallback_scratch = user_dir() / "scratch"
        if not fallback_log.exists() and not fallback_scratch.exists():
            return (0, 0)

        try:
            cur_log = self.log_dir.resolve()
            cur_scratch = self.scratch_dir.resolve()
            if fallback_log.resolve() == cur_log and fallback_scratch.resolve() == cur_scratch:
                return (0, 0)
        except OSError:
            pass

        sub_hk = Housekeeper(
            target_log_dir=fallback_log,
            target_scratch_dir=fallback_scratch,
            cwd=self.cwd,
        )
        c1, b1 = sub_hk.clean_scratch_outputs(ttl_days, max_files, max_size_mb)
        c2, b2 = sub_hk.clean_scratch_clipboard(ttl_days, max_files, max_size_mb)
        c3, b3 = sub_hk.clean_logs(ttl_days)
        c4, b4 = sub_hk.clean_scratch_checkpoints(ttl_days)
        return (c1 + c2 + c3 + c4, b1 + b2 + b3 + b4)

    def _collect_managed_stats(self) -> tuple[int, int]:
        """統計目前受管目錄（工作區暫存／日誌與全域備份）中現存的檔案數與總位元組數。"""
        file_count = 0
        total_bytes = 0
        targets: list[tuple[Path, str | None]] = [
            (self.scratch_dir / "outputs", "*.log"),
            (self.scratch_dir / "clipboard", "screenshot_*.png"),
            (self.log_dir, None),
        ]
        for directory, pattern in targets:
            if not directory.is_dir():
                continue
            entries = directory.glob(pattern) if pattern else directory.iterdir()
            for p in entries:
                if not p.is_file():
                    continue
                try:
                    total_bytes += p.stat().st_size
                    file_count += 1
                except OSError:
                    continue

        ckpt = self.scratch_dir / "research_checkpoint.md"
        if ckpt.is_file():
            try:
                total_bytes += ckpt.stat().st_size
                file_count += 1
            except OSError:
                pass

        backups_dir = user_dir() / "backups"
        if backups_dir.is_dir():
            for p in backups_dir.iterdir():
                if not p.is_file():
                    continue
                if p.name == "latest_backup_map.json" or p.name.startswith(".latest_backup_map_"):
                    continue
                try:
                    total_bytes += p.stat().st_size
                    file_count += 1
                except OSError:
                    continue

        return (file_count, total_bytes)

    def run_cleanup(self, force: bool = False) -> dict[str, Any]:
        """執行完整 Housekeeping 維護。

        若 force 為 False 且在冷卻期內，則略過。
        """
        if not force and not self.is_enabled:
            return {"status": "disabled", "cleaned_files": 0, "freed_bytes": 0}

        with self._lock:
            if not force and self.is_in_cooldown():
                return {"status": "cooldown", "cleaned_files": 0, "freed_bytes": 0}

            cleaned_root, root_bytes = self._clean_legacy_root_logs_with_bytes()
            scratch_count, scratch_bytes = self.clean_scratch_outputs()
            clipboard_count, clipboard_bytes = self.clean_scratch_clipboard()
            ckpt_count, ckpt_bytes = self.clean_scratch_checkpoints()
            log_count, log_bytes = self.clean_logs()
            backup_count, backup_bytes = self.clean_backups()
            other_ws_count, other_ws_bytes = self.clean_all_workspaces()
            fb_count, fb_bytes = self._clean_fallback_dirs()

            total_files = (
                cleaned_root
                + scratch_count
                + clipboard_count
                + ckpt_count
                + log_count
                + backup_count
                + other_ws_count
                + fb_count
            )
            total_bytes = (
                root_bytes
                + scratch_bytes
                + clipboard_bytes
                + ckpt_bytes
                + log_bytes
                + backup_bytes
                + other_ws_bytes
                + fb_bytes
            )

            self.mark_executed()
            remaining_files, remaining_bytes = self._collect_managed_stats()
            eff_ttl = self.ttl_days

        res = {
            "status": "success",
            "cleaned_files": total_files,
            "freed_bytes": total_bytes,
            "freed_mb": round(total_bytes / (1024 * 1024), 2),
            "remaining_files": remaining_files,
            "remaining_bytes": remaining_bytes,
            "remaining_mb": round(remaining_bytes / (1024 * 1024), 2),
            "ttl_days": eff_ttl,
            "log_ttl_days": eff_ttl * DEFAULT_HOUSEKEEPING_LOG_TTL_MULTIPLIER,
        }
        if total_files > 0:
            log.info("Housekeeping 完成：清理 %d 個檔案，釋放 %.2f MB", total_files, res["freed_mb"])
        return res


# 全域單例
default_housekeeper = Housekeeper()
