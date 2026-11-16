"""Auto-Learn 知識萃取與技能自動儲存服務。"""

import logging
import re
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pocket_agent.core.agent import Agent

log = logging.getLogger("pocket-agent")

_TRIGGER_KEYWORDS = ("成功", "修復", "完成")


def strip_markdown_fences(text: str) -> str:
    """移除 LLM 回覆外層多餘的 Markdown 程式碼圍欄。"""
    cleaned = text.strip()
    if cleaned.startswith("```markdown"):
        cleaned = cleaned[11:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    return cleaned.strip()


class AutoLearnService:
    """封裝對話後知識萃取與 Skill 自動持久化流程。"""

    @staticmethod
    def should_trigger(reply: str) -> bool:
        return any(kw in reply for kw in _TRIGGER_KEYWORDS)

    @staticmethod
    async def extract_skill(agent: "Agent") -> str:
        """非同步呼叫臨時子代理將最近對話總結為 Skill Markdown。"""
        if len(agent.history) < 2:
            return "對話歷史不足，無法萃取 Skill。"

        history_text = "\n".join(f"[{m['role']}] {str(m.get('content', ''))[:500]}" for m in agent.history[-10:])
        summary_prompt = (
            "請將以下最近的對話內容，萃取出可重用的行為或知識，撰寫成符合 Skill 格式的 Markdown 內容。\n"
            "不用加上額外的開場白，直接輸出內容。不要包含 ```markdown 等標籤。\n\n"
            f"{history_text}"
        )

        extractor = agent.create_ephemeral_subagent(
            system_prompt=("你是一個擅長知識萃取與歸納的軟體架構師，專門撰寫精準、具體的 Markdown 技能教學指南。"),
            reasoning_effort="none",
        )
        _, summary, _ = await extractor.chat(summary_prompt)
        return strip_markdown_fences(summary)

    @classmethod
    async def learn_from_history(cls, agent: "Agent") -> None:
        """從目前對話歷史萃取 Skill 並寫入全域技能庫，必要時同步索引。"""
        skill_md = await cls.extract_skill(agent)
        if not skill_md or "對話歷史不足" in skill_md:
            return

        from pocket_agent.extensions.skills import save_skill, sync_index
        from pocket_agent.system.registry import parse_frontmatter

        meta, body = parse_frontmatter(skill_md)
        raw_name = str(meta.get("name") or f"auto-skill-{int(time.time())}")
        safe_name = re.sub(r"[^a-zA-Z0-9_-]", "-", raw_name).strip("-")
        if not safe_name:
            safe_name = f"auto-skill-{int(time.time())}"
        desc = str(meta.get("description") or "自動從對話歷史萃取之經驗技能")
        skill_path = save_skill(safe_name, desc, body.strip() or skill_md, scope="global")
        log.info("Auto-Learn 完成：已寫入 %s", skill_path)
        if agent.workspace_dirs:
            sync_index(agent.workspace_dirs)
