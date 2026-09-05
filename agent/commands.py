"""Slash commands：使用者端快速指令，展開成提示詞模板。

指令檔（兩處都掃，使用者目錄優先）：
    <repo>/commands/<name>.md
    ~/.pocket-agent/commands/<name>.md

檔案格式：
    ---
    description: 一句話說明
    argument-hint: <選填，提示參數格式，例如 [檔名]>
    ---
    提示詞模板。$ARGUMENTS 代表使用者在 /name 後面打的全部文字，
    $1 $2 … 代表以空白切開的第 n 個參數。
"""

import logging
import re

from .paths import bundle_dir, user_dir

log = logging.getLogger("pocket-agent")

REPO_COMMANDS_DIR = bundle_dir() / "commands"  # 內建，隨程式發佈（唯讀）
USER_COMMANDS_DIR = user_dir() / "commands"  # 使用者自訂／plugin 裝入（可寫）

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def _parse(text: str) -> tuple[dict, str]:
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, text
    meta: dict = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta, text[match.end() :]


def list_commands() -> list[dict]:
    """所有 slash command 的 {name, description, argument_hint, path}。"""
    found: dict[str, dict] = {}
    for base in (REPO_COMMANDS_DIR, USER_COMMANDS_DIR):
        if not base.is_dir():
            continue
        for md in sorted(base.glob("*.md")):
            try:
                meta, _ = _parse(md.read_text(encoding="utf-8"))
            except Exception:
                log.warning("command 解析失敗：%s", md)
                continue
            name = md.stem
            found[name] = {
                "name": name,
                "description": meta.get("description", ""),
                "argument_hint": meta.get("argument-hint", ""),
                "path": md,
            }
    return list(found.values())


def get_command(name: str) -> dict | None:
    for cmd in list_commands():
        if cmd["name"] == name:
            return cmd
    return None


def expand_command(text: str) -> str | None:
    """若 text 是 /name [args] 形式且指令存在，回傳展開後的提示詞；否則 None。"""
    if not text.startswith("/"):
        return None

    head, _, rest = text[1:].partition(" ")
    cmd = get_command(head.strip())
    if cmd is None:
        return None

    try:
        _, template = _parse(cmd["path"].read_text(encoding="utf-8"))
    except Exception as e:
        log.error("command 讀取失敗 %s: %s", head, e)
        return None

    args = rest.strip()
    result = template.replace("$ARGUMENTS", args)

    # $1 $2 … 位置參數
    parts = args.split()
    for i, part in enumerate(parts, start=1):
        result = result.replace(f"${i}", part)
    # 未提供的位置參數清空
    result = re.sub(r"\$\d+", "", result)

    return result.strip()
