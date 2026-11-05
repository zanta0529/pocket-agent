"""巨量工具輸出落盤虛擬化管理器 (Spill-to-Disk Manager)。

當工具輸出（如 read_file, run_command 等）超過動態預算閾值時，
將完整輸出安全落盤寫入暫存溢出檔，僅向 LLM 上下文回傳「頂部預覽 + 尾部結論 + 落盤路徑」，
避免巨量日誌擠占上下文，同時保留頭部啟動參數與尾部測試/執行關鍵結論。
"""

import logging
import re
import time
from pathlib import Path
from typing import Any

from pocket_agent.system.config import config
from pocket_agent.system.constants import (
    DEFAULT_SPILLOVER_HEAD_CHARS,
    DEFAULT_SPILLOVER_TAIL_CHARS,
    DEFAULT_TOOL_OUTPUT_CHARS,
)

log = logging.getLogger("pocket-agent")

# 向後相容別名 (對齊 constants.py)
DEFAULT_HEAD_CHARS = DEFAULT_SPILLOVER_HEAD_CHARS
DEFAULT_TAIL_CHARS = DEFAULT_SPILLOVER_TAIL_CHARS


class SpilloverManager:
    """負責工具輸出的動態落盤與雙端預覽切片。"""

    def __init__(self, output_dir: Path | None = None):
        if output_dir is not None:
            self.output_dir = output_dir
        else:
            from pocket_agent.system.paths import scratch_dir

            self.output_dir = scratch_dir() / "outputs"

    def _ensure_dir(self) -> Path:
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            from pocket_agent.system.paths import user_dir

            fallback = user_dir() / "scratch" / "outputs"
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback
        return self.output_dir

    def spill_or_pass(
        self,
        text: Any,
        tool_name: str = "",
        call_id: str = "",
        max_chars: int | None = None,
        is_sensitive: bool = False,
    ) -> Any:
        """若文字超過動態閾值則落盤並回傳雙端預覽，否則原樣回傳。

        所有字元參數皆支援動態計算或由設定覆寫。敏感輸出超限時僅在記憶體雙端截斷，嚴禁落盤。
        """
        if not isinstance(text, str):
            return text

        # 動態門檻：優先讀取設定，否則採用傳入的 max_chars，保底 DEFAULT_TOOL_OUTPUT_CHARS
        cfg_threshold = config.get("system.spillover_threshold_chars")
        threshold = int(cfg_threshold) if cfg_threshold is not None else (max_chars or DEFAULT_TOOL_OUTPUT_CHARS)

        total_len = len(text)
        if total_len <= threshold:
            return text

        # 動態雙端預覽長度：預設各 1,500，或由設定覆寫，不超過門檻的 1/3
        cfg_head = config.get("system.spillover_head_chars")
        cfg_tail = config.get("system.spillover_tail_chars")
        max_preview = max(1, threshold // 3)
        raw_head = int(cfg_head) if cfg_head is not None else DEFAULT_HEAD_CHARS
        raw_tail = int(cfg_tail) if cfg_tail is not None else DEFAULT_TAIL_CHARS
        head_limit = min(raw_head, max_preview)
        tail_limit = min(raw_tail, max_preview)

        # 雙端切片組裝
        head_part = text[:head_limit]
        tail_part = text[-tail_limit:] if total_len > tail_limit else ""
        omitted_chars = max(0, total_len - head_limit - tail_limit)

        if is_sensitive:
            log.info(
                "敏感工具輸出超限，依資安政策僅於記憶體中雙端截斷，不予落盤: %s (%d 字元)",
                tool_name,
                total_len,
            )
            return (
                f"{head_part}\n\n"
                f"... [【安全防護】已省略中間 {omitted_chars:,} 字元；"
                "基於資安政策，敏感主機診斷資訊禁止落盤保存] ...\n\n"
                f"{tail_part}"
            )

        # 產生安全檔名並寫入全量檔案
        safe_tool = re.sub(r"[^\w\-]", "_", tool_name) or "tool"
        safe_cid = re.sub(r"[^\w\-]", "_", call_id)[:8] or "output"
        ts = int(time.time())
        fname = f"{safe_tool}_{safe_cid}_{ts}.log"

        target_dir = self._ensure_dir()
        file_path = target_dir / fname

        try:
            file_path.write_text(text, encoding="utf-8")
            log.info("巨量工具輸出已落盤: %s (%d 字元)", file_path, total_len)
        except OSError as e:
            log.warning("工具輸出落盤失敗，將退回純截斷模式: %s", e)
            return text[:threshold] + f"\n\n... (落盤失敗，已截斷至 {threshold} 字元) ...\n"

        total_lines = text.count("\n") + 1
        omitted_chars = max(0, total_len - head_limit - tail_limit)

        try:
            display_path = file_path.relative_to(Path.cwd())
        except ValueError:
            display_path = file_path

        summary_msg = (
            f"[系統日誌虛擬化] 工具執行輸出過長（共 {total_len:,} 字元 / {total_lines:,} 行），"
            f"已自動安全落盤至本機暫存檔。\n"
            f"📁 完整日誌路徑：{display_path}\n\n"
            f"=== 頂部預覽 (前 {head_limit:,} 字元) ===\n"
            f"{head_part}\n\n"
            f"... [中間省略 {omitted_chars:,} 字元，完整內容請參照上述檔案] ...\n\n"
            f"=== 尾部結論 (後 {tail_limit:,} 字元) ===\n"
            f"{tail_part}\n\n"
            f"💡 [檢索提示] 若需精確檢索中途省略資訊，請對上述日誌路徑使用 `search_files`，"
            f"或搭配 `start_line` 與 `end_line` 的 `read_file` 進行分頁局部閱讀。\n"
            f"⚠️ [重要限制] 切勿對此日誌路徑執行全量未分頁的 `read_file`，以免再次觸發落盤。"
        )

        return summary_msg


# 全域單例
default_spillover = SpilloverManager()
