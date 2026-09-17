"""Subagents：帶獨立人設的子代理，主 agent 透過 run_subagent 工具委派。

定義檔（兩處都掃，使用者目錄優先）：
    <repo>/subagents/<name>.md
    ~/.pocket-agent/subagents/<name>.md

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
from pathlib import Path

from pocket_agent.system.paths import bundle_dir, user_dir
from pocket_agent.system.registry import MarkdownRegistry

log = logging.getLogger("pocket-agent")

REPO_SUBAGENTS_DIR = bundle_dir() / "subagents"  # 內建，隨程式發佈（唯讀）
USER_SUBAGENTS_DIR = user_dir() / "subagents"  # 使用者自訂／plugin 裝入（可寫）


class SubagentRegistry(MarkdownRegistry):
    def get_glob_pattern(self) -> str:
        return "*.md"

    def parse_item(self, path: Path, meta: dict, body: str) -> dict:
        return {
            "name": meta.get("name") or path.stem,
            "description": meta.get("description", ""),
            "model": meta.get("model", "") or None,
            "reasoning_effort": meta.get("reasoning_effort", "") or None,
            "allowed_tools": meta.get("allowed_tools"),  # list[str] | None
            "max_tool_rounds": meta.get("max_tool_rounds"),  # int | None
            "prompt": body.strip(),
            "path": path,
        }


# 全域實例
_registry = SubagentRegistry(REPO_SUBAGENTS_DIR, USER_SUBAGENTS_DIR)


def list_subagents() -> list[dict]:
    """所有 subagent 的 {name, description, model, reasoning_effort, prompt, path}。"""
    return _registry.list_all()


def get_subagent(name: str) -> dict | None:
    return _registry.get_item(name)


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
