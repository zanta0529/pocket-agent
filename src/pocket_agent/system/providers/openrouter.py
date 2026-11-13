import logging
from typing import Any, List

import httpx
from openai import AsyncOpenAI

from pocket_agent.system.config import config
from pocket_agent.system.providers.base import BaseProvider

log = logging.getLogger("pocket-agent")


class OpenRouterProvider(BaseProvider):
    display_name = "OpenRouter"

    @classmethod
    def setup_wizard(cls, config) -> None:
        from rich.console import Console
        from rich.prompt import Prompt

        console = Console()
        current_url = config.get("providers.openrouter.base_url", "https://openrouter.ai/api/v1")
        console.print(f"[dim]目前 Base URL: {current_url}[/dim]")
        new_url = Prompt.ask("請輸入 Base URL", default=current_url)
        config.set("providers.openrouter.base_url", new_url)
        cls.prompt_api_key("providers.openrouter.api_key", "OpenRouter", config)

    def get_client(self) -> Any:
        base_url = config.get("providers.openrouter.base_url")
        if not base_url:
            raise ValueError("未設定 OpenRouter Base URL。請先透過 `agent setup` 或 GUI 設定。")
        base_url = base_url.rstrip("/")
        api_key = config.get("providers.openrouter.api_key") or ""

        extra_headers = {
            "HTTP-Referer": config.get("providers.openrouter.referer", "https://github.com/pocket-agent"),
            "X-Title": config.get("providers.openrouter.title", "Pocket Agent"),
        }

        timeout = httpx.Timeout(timeout=1800.0, connect=10.0)
        return AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers=extra_headers,
            http_client=self.build_async_http_client(timeout),
            timeout=timeout,
        )

    def fetch_models(self) -> List[str]:
        try:
            base_url = config.get("providers.openrouter.base_url")
            if base_url:
                base_url = base_url.rstrip("/")
            else:
                base_url = "https://openrouter.ai/api/v1"

            with httpx.Client() as client:
                resp = client.get(f"{base_url}/models", timeout=10)
            if resp.status_code == 200:
                return [m["id"] for m in resp.json().get("data", [])]
            return []
        except Exception as e:
            log.error(f"OpenRouter fetch models error: {e}")
            return []
