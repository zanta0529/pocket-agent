"""會話活動追蹤器：記錄單次會話中新增、修改、刪除之檔案，以及上網查詢之完整網址與狀態。"""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

from pocket_agent.frontend.cli.render import format_display_path

_SNAPSHOT_EXCLUDES = {
    ".git",
    "node_modules",
    "venv",
    "__pycache__",
    ".pocket-agent",
    ".pytest_cache",
    ".ruff_cache",
}
_MAX_SNAPSHOT_FILES = 5000

_FAILURE_PREFIXES = (
    "[ERROR]",
    "Error:",
    "[調研上限攔截]",
    "[使用者拒絕",
    "[操作已由使用者取消",
)


def is_tool_output_success(result: object) -> bool:
    """判定工具回傳字串是否代表執行成功。"""
    if not isinstance(result, str) or not result.strip():
        return False
    text = result.strip()
    return not text.startswith(_FAILURE_PREFIXES)


@dataclass
class UrlActivityItem:
    """單筆上網查詢網址紀錄（依首次查詢順序保留，狀態更新為最新結果並累計次數）。"""

    url: str
    status: str  # "success" | "failed"
    count: int = 1


class SessionActivityTracker:
    """追蹤本次會話中的檔案異動（新增／修改／刪除）與網路查詢網址清單。"""

    def __init__(self) -> None:
        self._display_paths: dict[str, Path] = {}
        self._initial_existed: dict[str, bool] = {}
        self._file_status: dict[str, str] = {}  # key -> "added" | "modified" | "deleted"
        self._confirmed_on_disk: dict[str, bool] = {}
        self._urls: dict[str, UrlActivityItem] = {}

    def clear(self) -> None:
        """清空所有會話活動紀錄（供 /clear 與 /new 使用）。"""
        self._display_paths.clear()
        self._initial_existed.clear()
        self._file_status.clear()
        self._confirmed_on_disk.clear()
        self._urls.clear()

    @staticmethod
    def _normalize_path(path: str | Path) -> tuple[str, Path]:
        resolved = Path(path).expanduser().resolve(strict=False)
        return os.path.normcase(str(resolved)), resolved

    def inspect_file_before_tool(self, path: str | Path) -> bool:
        """於檔案寫入工具執行前檢查檔案是否已存在，但不預先寫入變更紀錄（防止拒絕時誤列）。"""
        if not path:
            return False
        try:
            _, resolved = self._normalize_path(path)
            return resolved.exists()
        except Exception:
            return False

    def record_file_change(
        self,
        path: str | Path,
        action: str,
        *,
        existed_before: bool | None = None,
    ) -> None:
        """記錄檔案異動狀態（action: 'added' | 'modified' | 'deleted'）。"""
        if not path or action not in ("added", "modified", "deleted"):
            return
        try:
            key, resolved = self._normalize_path(path)
        except Exception:
            return

        self._display_paths[key] = resolved
        if key not in self._initial_existed:
            if existed_before is not None:
                self._initial_existed[key] = existed_before
            else:
                self._initial_existed[key] = action != "added"

        if action == "deleted":
            self._file_status[key] = "deleted"
            self._confirmed_on_disk[key] = False
            return

        # 若該檔案在本次會話首次觸碰前不存在，後續繼續追加或修改仍維持「新增 (added)」
        if not self._initial_existed[key]:
            self._file_status[key] = "added"
        else:
            self._file_status[key] = "modified" if action == "modified" else action

        self._confirmed_on_disk[key] = resolved.exists()

    def record_url(self, url: str, *, success: bool) -> None:
        """記錄上網查詢之完整網址與狀態（方案 A：以完整網址去重，更新最新狀態並累計次數）。"""
        clean_url = (url or "").strip()
        if not clean_url:
            return
        status = "success" if success else "failed"
        if clean_url in self._urls:
            item = self._urls[clean_url]
            item.status = status
            item.count += 1
        else:
            self._urls[clean_url] = UrlActivityItem(url=clean_url, status=status, count=1)

    def record_tool_result(
        self,
        tool_name: str,
        args: dict,
        result: object,
        *,
        existed_before: bool | None = None,
        generated_path: Path | None = None,
    ) -> None:
        """根據工具名稱、參數與執行結果自動記錄檔案異動或網址查詢。"""
        real_name = tool_name.split("::")[-1] if "::" in tool_name else tool_name

        # 1. 網路查詢工具（無論成功或失敗皆需記錄完整網址與狀態）
        if real_name == "read_url_content":
            url = str(args.get("url") or "").strip()
            if url:
                self.record_url(url, success=is_tool_output_success(result))
            return

        if real_name == "search_web":
            query = str(args.get("query") or "").strip()
            if query:
                search_url = f"https://duckduckgo.com/?q={quote_plus(query)}"
                ok = is_tool_output_success(result) and not str(result).strip().startswith(
                    "搜尋無結果，或遭到搜尋引擎阻擋"
                )
                self.record_url(search_url, success=ok)
            return

        if real_name == "browser_navigate":
            raw_url = str(args.get("url") or "").strip()
            if raw_url:
                if not raw_url.startswith(("http://", "https://")):
                    raw_url = f"https://{raw_url}"
                self.record_url(raw_url, success=is_tool_output_success(result))
            return

        # 2. 檔案寫入與產出工具（僅在成功執行時記錄，拒絕或報錯不列入）
        if not is_tool_output_success(result):
            return

        if real_name == "write_to_file" and isinstance(result, str) and result.startswith("[SUCCESS]"):
            path_str = args.get("path") or args.get("target_file") or args.get("file") or ""
            if path_str:
                was_existing = bool(existed_before) if existed_before is not None else False
                self.record_file_change(
                    path_str,
                    "modified" if was_existing else "added",
                    existed_before=was_existing,
                )
        elif real_name in ("append_to_file", "replace_file_content") and (
            isinstance(result, str) and result.startswith("[SUCCESS]")
        ):
            path_str = args.get("path") or args.get("target_file") or args.get("file") or ""
            if path_str:
                was_existing = True if existed_before is None else bool(existed_before)
                self.record_file_change(path_str, "modified", existed_before=was_existing)
        elif real_name in ("create_docx", "create_pdf", "create_pptx"):
            if generated_path is not None:
                self.record_file_change(generated_path, "added", existed_before=False)
            elif isinstance(result, str) and "檔案已產生：" in result:
                extracted = result.split("檔案已產生：", 1)[1].strip()
                if extracted:
                    self.record_file_change(extracted, "added", existed_before=False)

    def take_workspace_snapshot(self, workspace_dirs: list[str] | None = None) -> dict[str, tuple[Path, int, int]]:
        """對目前工作區建立輕量級檔案快照 (key -> (path, mtime_ns, size))，供 run_command 前後比對。"""
        roots: list[Path] = []
        try:
            roots.append(Path.cwd().resolve())
        except Exception:
            pass
        for wd in workspace_dirs or []:
            try:
                p = Path(wd).expanduser().resolve()
                if p not in roots and p.is_dir():
                    roots.append(p)
            except Exception:
                continue

        snapshot: dict[str, tuple[Path, int, int]] = {}
        count = 0
        for root in roots:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in _SNAPSHOT_EXCLUDES and not d.startswith(".env")]
                for fname in filenames:
                    if fname.startswith(".env"):
                        continue
                    fpath = Path(dirpath) / fname
                    try:
                        if fpath.is_symlink():
                            continue
                        st = fpath.stat()
                        resolved = fpath.resolve(strict=False)
                        key = os.path.normcase(str(resolved))
                        snapshot[key] = (resolved, st.st_mtime_ns, st.st_size)
                        count += 1
                        if count >= _MAX_SNAPSHOT_FILES:
                            return snapshot
                    except OSError:
                        continue
        return snapshot

    def apply_workspace_diff(
        self,
        before: dict[str, tuple[Path, int, int]],
        workspace_dirs: list[str] | None = None,
    ) -> None:
        """比對工具執行前後的工作區快照，自動捕捉新增、修改與刪除的檔案。"""
        after = self.take_workspace_snapshot(workspace_dirs)
        for key, (after_path, after_mtime, after_size) in after.items():
            if key not in before:
                self.record_file_change(after_path, "added", existed_before=False)
            else:
                _, before_mtime, before_size = before[key]
                if after_mtime != before_mtime or after_size != before_size:
                    self.record_file_change(after_path, "modified", existed_before=True)

        for key, (before_path, _, _) in before.items():
            if key not in after:
                self.record_file_change(before_path, "deleted", existed_before=True)

    def merge_from(self, other: "SessionActivityTracker") -> None:
        """將子代理 (Subagent) 的活動紀錄合併回主代理。"""
        for key, status in other._file_status.items():
            path = other._display_paths.get(key)
            existed = other._initial_existed.get(key)
            if path is not None:
                self.record_file_change(path, status, existed_before=existed)

        for item in other._urls.values():
            if item.url in self._urls:
                target = self._urls[item.url]
                target.status = item.status
                target.count += item.count
            else:
                self._urls[item.url] = UrlActivityItem(url=item.url, status=item.status, count=item.count)

    def rebuild_from_history(self, history: list[dict]) -> None:
        """從對話歷史 (history) 重建檔案與網址活動紀錄（供 /session load 使用）。"""
        self.clear()
        outputs_by_id: dict[str, str] = {}
        for item in history:
            if isinstance(item, dict) and item.get("type") == "function_call_output":
                cid = item.get("call_id")
                if cid:
                    outputs_by_id[str(cid)] = str(item.get("output") or "")

        for item in history:
            if not isinstance(item, dict) or item.get("type") != "function_call":
                continue
            name = str(item.get("name") or "")
            cid = str(item.get("call_id") or "")
            raw_args = item.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except Exception:
                continue
            if not isinstance(args, dict):
                continue
            out = outputs_by_id.get(cid, "")
            existed_hint: bool | None = None
            if name.split("::")[-1] == "write_to_file":
                existed_hint = bool(args.get("overwrite", False))
            self.record_tool_result(name, args, out, existed_before=existed_hint)

    def get_file_changes(self, *, sync_disk: bool = True) -> dict[str, list[str]]:
        """回傳分類後的檔案清單 {'added': [...], 'modified': [...], 'deleted': [...]}。"""
        grouped: dict[str, list[str]] = {"added": [], "modified": [], "deleted": []}
        for key, path in self._display_paths.items():
            status = self._file_status.get(key)
            if not status:
                continue
            if sync_disk and status in ("added", "modified"):
                if self._confirmed_on_disk.get(key, False) and not path.exists():
                    status = "deleted"
                    self._file_status[key] = "deleted"
            display = format_display_path(str(path))
            if status in grouped:
                grouped[status].append(display)
        return grouped

    def get_url_activities(self) -> list[UrlActivityItem]:
        """回傳依首次查詢順序排列之完整網址與狀態清單。"""
        return list(self._urls.values())
