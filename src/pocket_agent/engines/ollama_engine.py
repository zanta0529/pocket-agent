import logging

from pocket_agent.engines.chat_engine import StandardChatCompletionsEngine

log = logging.getLogger("pocket-agent")


class OllamaChatEngine(StandardChatCompletionsEngine):
    provider_name = "ollama"

    async def fetch_context_window(self) -> int | None:
        import time

        import httpx

        try:
            base_url = str(getattr(self.client, "base_url", "http://localhost:11434/v1"))
            ollama_host = base_url.rstrip("/").removesuffix("/v1")
            async with httpx.AsyncClient(timeout=5.0) as http_client:
                resp = await http_client.post(f"{ollama_host}/api/show", json={"model": self.model})
                if resp.status_code != 200:
                    return None
                data = resp.json()
                model_info = data.get("model_info", {})
                for k, v in model_info.items():
                    if k.endswith(".context_length") and isinstance(v, int) and v > 0:
                        key = self._cache_key()
                        self._context_window_cache[key] = (v, time.time() + 3600)
                        return v
        except Exception as e:
            log.debug("查詢 Ollama context_length 失敗: %s", e)
        return None
