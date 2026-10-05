import logging
from typing import Any, List

import httpx
from openai import AsyncOpenAI

from pocket_agent.system.config import config
from pocket_agent.system.providers.base import BaseProvider

log = logging.getLogger("pocket-agent")

MISTRAL_BASE_URL = "https://api.mistral.ai/v1"


class MistralProvider(BaseProvider):
    display_name = "Mistral API"

    @classmethod
    def setup_wizard(cls, config) -> None:
        cls.prompt_api_key("providers.mistral.api_key", "Mistral", config)

    def get_client(self) -> Any:
        api_key = config.get("providers.mistral.api_key")
        if not api_key:
            raise ValueError(
                "未設定 Mistral API Key。請設定 `providers.mistral.api_key` 或透過 `agent setup` 進行設定。"
            )

        return AsyncOpenAI(
            api_key=api_key,
            base_url=MISTRAL_BASE_URL,
            http_client=None,
            timeout=httpx.Timeout(timeout=180.0, connect=10.0),
        )

    def fetch_models(self) -> List[str]:
        api_key = config.get("providers.mistral.api_key")
        if not api_key:
            return []

        try:
            with httpx.Client() as client:
                resp = client.get(
                    f"{MISTRAL_BASE_URL}/models", headers={"Authorization": f"Bearer {api_key}"}, timeout=10
                )
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("data", [])
                return [m["id"] for m in items if "id" in m]
            return ["mistral-large-latest", "mistral-small-latest", "open-mistral-nemo"]
        except Exception as e:
            log.error(f"Mistral fetch models error: {e}")
            return ["mistral-large-latest", "mistral-small-latest", "open-mistral-nemo"]
