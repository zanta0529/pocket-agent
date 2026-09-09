"""Session state and attachment transaction boundaries for the CLI."""

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CliState:
    provider: str
    model: str
    pending_files: list[str] = field(default_factory=list)
    last_failed_input: str = ""

    def add_files(self, paths: list[str]) -> list[str]:
        # Validate the whole batch before committing, including readability.
        normalized = []
        for value in paths:
            path = Path(value).expanduser().resolve(strict=True)
            if not path.is_file():
                raise ValueError(f"不是一般檔案：{path}")
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
