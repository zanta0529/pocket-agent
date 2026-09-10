import logging
from typing import Any

from .base import BaseEngine
from .ollama_engine import OllamaChatEngine
from .openai_engine import OpenAIResponsesEngine

log = logging.getLogger("pocket-agent")


class EngineFactory:
    @staticmethod
    def create(provider: str, client: Any, model: str) -> BaseEngine:
        if provider.lower() == "ollama":
            log.debug("Using OllamaChatEngine for %s", model)
            return OllamaChatEngine(client=client, model=model)
        else:
            log.debug("Using OpenAIResponsesEngine for %s", model)
            return OpenAIResponsesEngine(client=client, model=model)
