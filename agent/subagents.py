"""Subagents：帶獨立人設的子代理，主 agent 透過 run_subagent 工具委派。

定義檔（兩處都掃，使用者目錄優先）：
    <repo>/agents/<name>.md
    ~/.pocket-agent/agents/<name>.md

檔案格式：
    ---
    name: researcher
    description: 需要深入查找、彙整資料時委派
    model: <選填，覆寫模型>
    reasoning_effort: <選填 none/low/medium/high/xhigh>
    ---
    子代理的 system prompt。
"""

import logging
import re

from .paths import bundle_dir, user_dir

log = logging.getLogger("pocket-agent")

REPO_AGENTS_DIR = bundle_dir() / "agents"  # 內建，隨程式發佈（唯讀）
USER_AGENTS_DIR = user_dir() / "agents"  # 使用者自訂／plugin 裝入（可寫）

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


def list_subagents() -> list[dict]:
    """所有 subagent 的 {name, description, model, reasoning_effort, prompt, path}。"""
    found: dict[str, dict] = {}
    for base in (REPO_AGENTS_DIR, USER_AGENTS_DIR):
        if not base.is_dir():
            continue
        for md in sorted(base.glob("*.md")):
            try:
                meta, body = _parse(md.read_text(encoding="utf-8"))
            except Exception:
                log.warning("subagent 解析失敗：%s", md)
                continue
            name = meta.get("name") or md.stem
            found[name] = {
                "name": name,
                "description": meta.get("description", ""),
                "model": meta.get("model", "") or None,
                "reasoning_effort": meta.get("reasoning_effort", "") or None,
                "prompt": body.strip(),
                "path": md,
            }
    return list(found.values())


def get_subagent(name: str) -> dict | None:
    for sa in list_subagents():
        if sa["name"] == name:
            return sa
    return None


# ── run_subagent 工具 ─────────────────────────────


def subagent_tools() -> list[dict]:
    """有定義 subagent 才註冊工具。"""
    agents = list_subagents()
    if not agents:
        return []

    listing = "、".join(f"{a['name']}（{a['description']}）" for a in agents)
    return [
        {
            "type": "function",
            "name": "run_subagent",
            "description": (
                "把一個明確的子任務委派給專門的子代理，回傳它的結果。"
                "子代理有獨立的對話脈絡與人設，適合需要專注處理的工作。"
                f"可用的子代理：{listing}"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "子代理名稱",
                    },
                    "task": {
                        "type": "string",
                        "description": "要交辦的完整任務描述（子代理看不到主對話，請寫清楚）",
                    },
                },
                "required": ["name", "task"],
            },
        }
    ]


def is_subagent_tool(name: str) -> bool:
    return name == "run_subagent"
