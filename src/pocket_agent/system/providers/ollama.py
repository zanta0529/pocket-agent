import logging
from typing import Any, List

import httpx
from openai import AsyncOpenAI

from pocket_agent.system.config import config
from pocket_agent.system.providers.base import BaseProvider

log = logging.getLogger("pocket-agent")


class OllamaLocalProvider(BaseProvider):
    def get_client(self) -> Any:
        base_url = config.get("providers.ollama.base_url")
        if base_url:
            base_url = base_url.rstrip("/")
            if not base_url.endswith("/v1"):
                base_url = f"{base_url}/v1"
        else:
            base_url = "http://localhost:11434/v1"

        http_client = httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(timeout=1800.0, connect=60.0))
        return AsyncOpenAI(
            api_key="ollama",
            base_url=base_url,
            default_headers={},
            http_client=http_client,
            timeout=httpx.Timeout(timeout=1800.0, connect=60.0),
        )

    def fetch_models(self) -> List[str]:
        try:
            base_url = config.get("providers.ollama.base_url")
            if base_url:
                base_url = base_url.rstrip("/")
                if base_url.endswith("/v1"):
                    base_url = base_url[:-3]
            else:
                base_url = "http://localhost:11434"

            with httpx.Client(trust_env=False) as client:
                resp = client.get(f"{base_url}/api/tags", timeout=10)
            if resp.status_code == 200:
                return [m["name"] for m in resp.json().get("models", [])]
            return []
        except Exception as e:
            log.error(f"Ollama fetch tags error: {e}")
            return []
