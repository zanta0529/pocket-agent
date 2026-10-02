import logging
from typing import Any, List

import httpx
from openai import AsyncOpenAI

from pocket_agent.system.config import config
from pocket_agent.system.providers.base import BaseProvider

log = logging.getLogger("pocket-agent")


class OpenRouterProvider(BaseProvider):
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

        # OpenRouter usually does not need a custom HTTPX client for basic trust_env,
        # but to keep consistency with the original code, we don't pass http_client here.
        # Wait, the original code in auth.py:
        # http_client = None
        # for openrouter, it doesn't set http_client, so it passes None.

        return AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers=extra_headers,
            http_client=None,
            timeout=httpx.Timeout(timeout=1800.0, connect=10.0),
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
