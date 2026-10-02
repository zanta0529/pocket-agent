import logging
from typing import Any, List

import httpx
from openai import AsyncOpenAI

from pocket_agent.system.config import config
from pocket_agent.system.providers.base import BaseProvider

log = logging.getLogger("pocket-agent")

GROQ_BASE_URL = "https://api.groq.com/openai/v1"


class GroqProvider(BaseProvider):
    def get_client(self) -> Any:
        api_key = config.get("providers.groq.api_key")
        if not api_key:
            raise ValueError("未設定 Groq API Key。請設定 `providers.groq.api_key` 或透過 `agent setup` 進行設定。")

        return AsyncOpenAI(
            api_key=api_key,
            base_url=GROQ_BASE_URL,
            http_client=None,
            timeout=httpx.Timeout(timeout=180.0, connect=10.0),
        )

    def fetch_models(self) -> List[str]:
        api_key = config.get("providers.groq.api_key")
        if not api_key:
            return []

        try:
            with httpx.Client() as client:
                resp = client.get(f"{GROQ_BASE_URL}/models", headers={"Authorization": f"Bearer {api_key}"}, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("data", [])
                # Filter for text models only if needed, but Groq mostly exposes LLMs here
                return [m["id"] for m in items if "id" in m]
            return ["llama-3.1-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768", "gemma2-9b-it"]
        except Exception as e:
            log.error(f"Groq fetch models error: {e}")
            return ["llama-3.1-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768", "gemma2-9b-it"]
