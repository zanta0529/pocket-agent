"""Session state and attachment transaction boundaries for the CLI."""

import os
from dataclasses import dataclass, field
from pathlib import Path


def _new_session_id() -> str:
    from pocket_agent.core.sessions import new_session_id

    return new_session_id()


@dataclass
class CliState:
    provider: str
    model: str
    pending_files: list[str] = field(default_factory=list)
    last_failed_input: str = ""
    session_id: str = field(default_factory=_new_session_id)
    session_tokens: int = 0  # 本 session 累計 token 用量（供 toolbar 顯示）
    session_approved_commands: set = field(default_factory=set)  # 本次對話中允許的指令清單
    accept_edits: bool = False  # Accept-edits mode：當次會話自動核准檔案新增與編輯
    quota_supported: bool = False
    quota_limit: float | None = None
    quota_remaining: float | None = None
    context_window: int = 128000
    staged_update_version: str | None = None
    pending_update_restart: bool = False

    def add_files(self, paths: list[str]) -> list[str]:
        # Validate the whole batch before committing, including readability.
        normalized = []
        for value in paths:
            path = Path(value).expanduser().resolve(strict=True)
            if not path.is_file() and not path.is_dir():
                raise ValueError(f"路徑不存在或無效：{path}")
            if path.is_file():
                with path.open("rb"):
                    pass
            normalized.append(str(path))
        known = {os.path.normcase(p) for p in self.pending_files}
        added = []
        for value in normalized:
            key = os.path.normcase(value)
            if key not in known:
                known.add(key)
                self.pending_files.append(value)
                added.append(value)
        return added

    def snapshot(self) -> list[str]:
        # Revalidate pending files: they may have disappeared since attachment.
        self.add_files(list(self.pending_files))
        return list(self.pending_files)

    def consume(self, snapshot: list[str]) -> None:
        sent = set(snapshot)
        self.pending_files[:] = [p for p in self.pending_files if p not in sent]
        self.last_failed_input = ""

    def remove(self, number: int) -> str:
        if not 1 <= number <= len(self.pending_files):
            raise ValueError("附件編號超出範圍")
        return self.pending_files.pop(number - 1)
