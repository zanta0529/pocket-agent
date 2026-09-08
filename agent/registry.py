import logging
import re
from abc import ABC, abstractmethod
from pathlib import Path

log = logging.getLogger("pocket-agent")

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 Markdown 開頭的 YAML frontmatter，回傳 (meta_dict, body)。"""
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, text

    meta: dict = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta, text[match.end() :]


class MarkdownRegistry(ABC):
    """Markdown 資源管理的樣板抽象基礎類別 (Template Method Pattern)"""

    def __init__(self, repo_dir: Path, user_dir: Path):
        self.repo_dir = repo_dir
        self.user_dir = user_dir

    @abstractmethod
    def get_glob_pattern(self) -> str:
        """指定要掃描的檔案規則，例如 '*.md' 或 '*/SKILL.md'"""
        pass

    @abstractmethod
    def parse_item(self, path: Path, meta: dict, body: str) -> dict:
        """將解析出的資料轉換為標準字典，必須包含 'name' 鍵值"""
        pass

    def list_all(self) -> list[dict]:
        """掃描所有層級的目錄，回傳已註冊的資源清單（使用者目錄優先覆蓋）"""
        found: dict[str, dict] = {}
        for base in (self.repo_dir, self.user_dir):
            if not base.is_dir():
                continue
            for md in sorted(base.glob(self.get_glob_pattern())):
                try:
                    text = md.read_text(encoding="utf-8")
                    meta, body = parse_frontmatter(text)
                except Exception as e:
                    log.warning("資源解析失敗 %s: %s", md, e)
                    continue

                item = self.parse_item(md, meta, body)
                if item and "name" in item:
                    found[item["name"]] = item
        return list(found.values())

    def get_item(self, name: str) -> dict | None:
        """依照名稱取得特定資源資訊"""
        for item in self.list_all():
            if item["name"] == name:
                return item
        return None

    def read_body(self, name: str) -> str | None:
        """讀取並回傳資源的本文內容（去除 frontmatter）"""
        item = self.get_item(name)
        if item and "path" in item:
            try:
                path: Path = item["path"]
                _, body = parse_frontmatter(path.read_text(encoding="utf-8"))
                return body.strip()
            except Exception as e:
                log.error("資源讀取失敗 %s: %s", name, e)
        return None
