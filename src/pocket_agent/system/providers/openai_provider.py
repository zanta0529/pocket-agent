import logging
from typing import Any, List

import httpx
from openai import AsyncOpenAI

from pocket_agent.system.config import config
from pocket_agent.system.constants import DEFAULT_STREAM_READ_TIMEOUT_SEC, PROVIDER_OPENAI
from pocket_agent.system.providers.base import BaseProvider, OpenAICompatibleProvider

log = logging.getLogger("pocket-agent")

OPENAI_API_BASE_URL = "https://api.openai.com/v1"


class OpenAIProvider(BaseProvider):
    display_name = "OpenAI Codex OAuth (no API key needed)"

    @classmethod
    def setup_wizard(cls, config) -> None:
        from rich.console import Console
        from rich.prompt import Confirm

        from pocket_agent.system.auth import _is_expired, _load_token, get_access_token, logout
        from pocket_agent.system.i18n import t

        console = Console()
        console.print(f"[dim]{t('provider_setup.openai_oauth_hint')}[/dim]\n")

        token = _load_token(PROVIDER_OPENAI)
        if token and not _is_expired(token):
            console.print(f"[green]{t('provider_setup.oauth_logged_in', provider='OPENAI')}[/green]")
            if not Confirm.ask(t("provider_setup.oauth_relogin"), default=False):
                console.print()
                return
            logout(PROVIDER_OPENAI)
        console.print(f"{t('provider_setup.oauth_opening_browser', account='OPENAI')}\n")
        input(t("provider_setup.press_enter"))
        get_access_token(provider=PROVIDER_OPENAI)
        console.print()

    def get_client(self) -> Any:
        # 為了避免循環依賴，以及保留 OAuth 邏輯在 auth.py，這裡直接從 auth.py 引入
        from pocket_agent.system.auth import CODEX_BASE_URL, _extract_account_id, get_access_token

        base_url = CODEX_BASE_URL
        api_key = get_access_token(provider=PROVIDER_OPENAI)
        account_id = _extract_account_id(api_key)

        extra_headers = {
            "originator": "pocket-agent",
            "OpenAI-Beta": "responses=experimental",
        }
        if account_id:
            extra_headers["chatgpt-account-id"] = account_id

        timeout = httpx.Timeout(timeout=1800.0, connect=15.0, read=DEFAULT_STREAM_READ_TIMEOUT_SEC)
        return AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers=extra_headers,
            http_client=self.build_async_http_client(timeout),
            timeout=timeout,
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

        token = _load_token(PROVIDER_OPENAI)
        if token and _is_expired(token):
            client_id = config.get("providers.openai.client_id") or DEFAULT_CLIENT_ID
            token = _try_refresh(PROVIDER_OPENAI, token, client_id)
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


class OpenAIApiProvider(OpenAICompatibleProvider):
    display_name = "OpenAI API"
    config_key = "providers.openai-api.api_key"
    api_key_label = "OpenAI"
    base_url = OPENAI_API_BASE_URL
    missing_key_message = "未設定 OpenAI API Key。請設定 `providers.openai-api.api_key`。"
    timeout_seconds = 180.0
    connect_timeout_seconds = 15.0
    read_timeout_seconds = DEFAULT_STREAM_READ_TIMEOUT_SEC
    use_shared_http_client = True
    fail_on_auth_error = True
    fallback_models = ("gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "o1-preview", "o1-mini")
