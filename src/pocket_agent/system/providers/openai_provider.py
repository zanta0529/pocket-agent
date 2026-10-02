import logging
from typing import Any, List

import httpx
from openai import AsyncOpenAI

from pocket_agent.system.config import config
from pocket_agent.system.providers.base import BaseProvider

log = logging.getLogger("pocket-agent")


class OpenAIProvider(BaseProvider):
    def get_client(self) -> Any:
        # 為了避免循環依賴，以及保留 OAuth 邏輯在 auth.py，這裡直接從 auth.py 引入
        from pocket_agent.system.auth import CODEX_BASE_URL, _extract_account_id, get_access_token

        base_url = CODEX_BASE_URL
        api_key = get_access_token(provider="openai")
        account_id = _extract_account_id(api_key)

        extra_headers = {
            "originator": "pocket-agent",
            "OpenAI-Beta": "responses=experimental",
        }
        if account_id:
            extra_headers["chatgpt-account-id"] = account_id

        return AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers=extra_headers,
            http_client=None,
            timeout=httpx.Timeout(timeout=1800.0, connect=10.0),
        )

    def fetch_models(self) -> List[str]:
        from pocket_agent.system.auth import (
            _SSL_VERIFY,
            CODEX_BASE_URL,
            DEFAULT_CLIENT_ID,
            _extract_account_id,
            _is_expired,
            _load_token,
            _try_refresh,
        )

        token = _load_token("openai")
        if token and _is_expired(token):
            client_id = config.get("providers.openai.client_id") or DEFAULT_CLIENT_ID
            token = _try_refresh("openai", token, client_id)
        if not token:
            return []

        access_token = token["access_token"]
        headers = {
            "Authorization": f"Bearer {access_token}",
            "originator": "pocket-agent",
        }
        account_id = _extract_account_id(access_token)
        if account_id:
            headers["chatgpt-account-id"] = account_id

        try:
            resp = httpx.get(
                f"{CODEX_BASE_URL}/models",
                params={"client_version": "99.0.0"},
                headers=headers,
                timeout=10,
                verify=_SSL_VERIFY,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return []

        items = data.get("models", data.get("data", [])) if isinstance(data, dict) else data
        slugs = []
        for item in items or []:
            if isinstance(item, str):
                slug = item
            elif isinstance(item, dict):
                slug = item.get("slug") or item.get("id") or item.get("model") or ""
            else:
                continue
            if slug and "review" not in slug and slug not in slugs:
                slugs.append(slug)
        return slugs
