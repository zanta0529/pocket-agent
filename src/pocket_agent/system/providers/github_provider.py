import logging
from typing import Any, List

import httpx
from openai import AsyncOpenAI

from pocket_agent.system.config import config
from pocket_agent.system.providers.base import BaseProvider

log = logging.getLogger("pocket-agent")

GITHUB_BASE_URL = "https://models.inference.ai.azure.com"


class GitHubProvider(BaseProvider):
    def get_client(self) -> Any:
        api_key = config.get("providers.github.api_key")
        if not api_key:
            raise ValueError(
                "未設定 GitHub Models API Key。請設定 `providers.github.api_key` 或透過 `agent setup` 進行設定。"
            )

        return AsyncOpenAI(
            api_key=api_key,
            base_url=GITHUB_BASE_URL,
            http_client=None,
            timeout=httpx.Timeout(timeout=180.0, connect=60.0),
        )

    def fetch_models(self) -> List[str]:
        api_key = config.get("providers.github.api_key")
        if not api_key:
            return []

        try:
            with httpx.Client() as client:
                resp = client.get(
                    f"{GITHUB_BASE_URL}/models", headers={"Authorization": f"Bearer {api_key}"}, timeout=10
                )
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("data", [])
                return [m["id"] for m in items if "id" in m]
            return [
                "gpt-4o",
                "gpt-4o-mini",
                "meta-llama-3.1-405b-instruct",
                "meta-llama-3.1-70b-instruct",
                "cohere-command-r-plus",
            ]
        except Exception as e:
            log.error(f"GitHub fetch models error: {e}")
            return [
                "gpt-4o",
                "gpt-4o-mini",
                "meta-llama-3.1-405b-instruct",
                "meta-llama-3.1-70b-instruct",
                "cohere-command-r-plus",
            ]
