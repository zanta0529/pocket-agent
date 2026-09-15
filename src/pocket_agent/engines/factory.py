import logging
from typing import Any

from pocket_agent.engines.base import BaseEngine
from pocket_agent.engines.ollama_engine import OllamaChatEngine
from pocket_agent.engines.openai_engine import OpenAIResponsesEngine
from pocket_agent.engines.openrouter_engine import OpenRouterEngine

log = logging.getLogger("pocket-agent")


class EngineFactory:
    @staticmethod
    def create(provider: str, client: Any, model: str) -> BaseEngine:
        p = provider.lower()
        if p == "ollama":
            log.debug("Using OllamaChatEngine for %s", model)
            return OllamaChatEngine(client=client, model=model)
        elif p == "openrouter":
            log.debug("Using OpenRouterEngine for %s", model)
            return OpenRouterEngine(client=client, model=model)
        else:
            log.debug("Using OpenAIResponsesEngine for %s", model)
            return OpenAIResponsesEngine(client=client, model=model)
