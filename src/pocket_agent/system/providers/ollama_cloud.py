import logging
from typing import Any, List

import httpx
from openai import AsyncOpenAI

from pocket_agent.system.config import config
from pocket_agent.system.providers.base import BaseProvider

log = logging.getLogger("pocket-agent")


class OllamaCloudProvider(BaseProvider):
    def get_client(self) -> Any:
        base_url = config.get("providers.ollama_cloud.base_url") or "https://ollama.com/v1"
        base_url = base_url.rstrip("/")
        api_key = config.get("providers.ollama_cloud.api_key") or ""
        if not api_key:
            raise ValueError("未設定 Ollama Cloud API Key。請先透過設定檔加入。")

        http_client = httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(timeout=1800.0, connect=60.0))
        return AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers={},
            http_client=http_client,
            timeout=httpx.Timeout(timeout=1800.0, connect=60.0),
        )

    def fetch_models(self) -> List[str]:
        try:
            base_url = config.get("providers.ollama_cloud.base_url") or "https://ollama.com/v1"
            base_url = base_url.rstrip("/")
            api_key = config.get("providers.ollama_cloud.api_key") or ""
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            with httpx.Client(trust_env=False) as client:
                resp = client.get(f"{base_url}/models", headers=headers, timeout=10)
            if resp.status_code == 200:
                return [m["id"] for m in resp.json().get("data", [])]
            return []
        except Exception as e:
            log.error(f"Ollama Cloud fetch models error: {e}")
            return []
