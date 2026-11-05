"""Hooks：生命週期自動化，在特定事件自動執行 CLI 指令。

設定存 ~/.pocket-agent/hooks.json：
    [
      {"name": "自動commit", "event": "after_response", "command": "git add -A && git commit -m auto"}
    ]

支援的事件：
    before_send    送出使用者訊息前     env: POCKETAGENT_USER_INPUT
    after_response 收到完整回覆後       env: POCKETAGENT_USER_INPUT, POCKETAGENT_REPLY
    before_tool    每次工具呼叫前       env: POCKETAGENT_TOOL_NAME, POCKETAGENT_TOOL_ARGS
    after_tool     每次工具呼叫後       env: POCKETAGENT_TOOL_NAME, POCKETAGENT_TOOL_RESULT

Hook 為輔助自動化，採 fire-and-forget（有逾時），錯誤只記 log 不打斷對話。
"""

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("pocket-agent")

HOOKS_PATH = Path.home() / ".pocket-agent" / "hooks.json"

EVENTS = ("before_send", "after_response", "before_tool", "after_tool")

_HOOK_TIMEOUT = 30


def load_hooks() -> list[dict]:
    if HOOKS_PATH.exists():
        try:
            data = json.loads(HOOKS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception:
            log.exception("hooks.json 解析失敗")
    return []


def save_hooks(hooks: list[dict]) -> None:
    HOOKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    HOOKS_PATH.write_text(
        json.dumps(hooks, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def run_hooks(event: str, context: dict | None = None) -> None:
    """執行某事件的所有 hook。阻塞呼叫，core 端請用 to_thread 包起來。"""
    hooks = [h for h in load_hooks() if h.get("event") == event and h.get("command")]
    if not hooks:
        return

    env = dict(os.environ)
    for key, value in (context or {}).items():
        env[f"POCKETAGENT_{key.upper()}"] = str(value)[:4000]

    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

    for hook in hooks:
        try:
            subprocess.run(
                hook["command"],
                shell=True,
                env=env,
                timeout=_HOOK_TIMEOUT,
                capture_output=True,
                text=True,
                errors="replace",
                creationflags=creationflags,
            )
            log.info("hook 執行：%s [%s]", hook.get("name", "?"), event)
        except Exception as e:
            log.error("hook 失敗 %s [%s]: %s", hook.get("name", "?"), event, e)
