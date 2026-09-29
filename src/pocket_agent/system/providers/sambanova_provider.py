import logging
from typing import Any, List

import httpx
from openai import AsyncOpenAI

from pocket_agent.system.config import config
from pocket_agent.system.providers.base import BaseProvider

log = logging.getLogger("pocket-agent")

SAMBANOVA_BASE_URL = "https://api.sambanova.ai/v1"


class SambaNovaProvider(BaseProvider):
    def get_client(self) -> Any:
        api_key = config.get("providers.sambanova.api_key")
        if not api_key:
            raise ValueError(
                "未設定 SambaNova API Key。請設定 `providers.sambanova.api_key` 或透過 `agent setup` 進行設定。"
            )

        return AsyncOpenAI(
            api_key=api_key,
            base_url=SAMBANOVA_BASE_URL,
            http_client=None,
            timeout=httpx.Timeout(timeout=180.0, connect=60.0),
        )

    def fetch_models(self) -> List[str]:
        api_key = config.get("providers.sambanova.api_key")
        if not api_key:
            return []

        try:
            with httpx.Client() as client:
                resp = client.get(
                    f"{SAMBANOVA_BASE_URL}/models", headers={"Authorization": f"Bearer {api_key}"}, timeout=10
                )
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("data", [])
                return [m["id"] for m in items if "id" in m]
            return ["Meta-Llama-3.1-405B-Instruct", "Meta-Llama-3.1-70B-Instruct", "Meta-Llama-3.1-8B-Instruct"]
        except Exception as e:
            log.error(f"SambaNova fetch models error: {e}")
            return ["Meta-Llama-3.1-405B-Instruct", "Meta-Llama-3.1-70B-Instruct", "Meta-Llama-3.1-8B-Instruct"]
