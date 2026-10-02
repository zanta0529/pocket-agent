import logging
from typing import Any, List

import httpx
from openai import AsyncOpenAI

from pocket_agent.system.config import config
from pocket_agent.system.providers.base import BaseProvider

log = logging.getLogger("pocket-agent")

GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


class GeminiProvider(BaseProvider):
    def get_client(self) -> Any:
        api_key = config.get("providers.gemini.api_key")
        if not api_key:
            raise ValueError(
                "未設定 Gemini API Key。請設定 `providers.gemini.api_key` 或切換至 `gemini-oauth` 供應商。"
            )

        # Google AI Studio API is fully compatible with OpenAI SDK using this endpoint
        return AsyncOpenAI(
            api_key=api_key,
            base_url=GEMINI_OPENAI_BASE_URL,
            http_client=None,
            timeout=httpx.Timeout(timeout=180.0, connect=10.0),
        )

    def fetch_models(self) -> List[str]:
        # Currently, Google AI Studio OpenAI-compat endpoint might not perfectly implement /v1/models.
        # But we can try to fetch, or return a static list of known models.
        api_key = config.get("providers.gemini.api_key")
        if not api_key:
            return []

        try:
            with httpx.Client() as client:
                resp = client.get(
                    f"{GEMINI_OPENAI_BASE_URL}models", headers={"Authorization": f"Bearer {api_key}"}, timeout=10
                )
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("models", data.get("data", []))
                return [m["id"] for m in items if "id" in m]
            return ["gemini-1.5-pro", "gemini-1.5-flash", "gemini-2.0-flash"]  # Fallback
        except Exception as e:
            log.error(f"Gemini fetch models error: {e}")
            return ["gemini-1.5-pro", "gemini-1.5-flash", "gemini-2.0-flash"]


class GeminiOAuthProvider(BaseProvider):
    def get_client(self) -> Any:
        from pocket_agent.system.auth import get_access_token

        api_key = get_access_token(provider="gemini-oauth")
        if not api_key:
            raise ValueError("無法取得 Gemini OAuth Token。")

        return AsyncOpenAI(
            api_key=api_key,
            base_url=GEMINI_OPENAI_BASE_URL,
            http_client=None,
            timeout=httpx.Timeout(timeout=180.0, connect=10.0),
        )

    def fetch_models(self) -> List[str]:
        from pocket_agent.system.auth import _is_expired, _load_token, _try_refresh

        token = _load_token("gemini-oauth")
        if token and _is_expired(token):
            client_id = config.get("providers.gemini.client_id")
            token = _try_refresh("gemini-oauth", token, client_id)

        if not token:
            return []

        access_token = token.get("access_token")
        if not access_token:
            return []

        try:
            with httpx.Client() as client:
                resp = client.get(
                    f"{GEMINI_OPENAI_BASE_URL}models", headers={"Authorization": f"Bearer {access_token}"}, timeout=10
                )
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("models", data.get("data", []))
                return [m["id"] for m in items if "id" in m]
            return ["gemini-1.5-pro", "gemini-1.5-flash", "gemini-2.0-flash"]  # Fallback
        except Exception as e:
            log.error(f"Gemini OAuth fetch models error: {e}")
            return ["gemini-1.5-pro", "gemini-1.5-flash", "gemini-2.0-flash"]
