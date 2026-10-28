import logging
from typing import Any, List

import httpx
from openai import AsyncOpenAI

from pocket_agent.system.config import config
from pocket_agent.system.constants import DEFAULT_STREAM_READ_TIMEOUT_SEC
from pocket_agent.system.providers.base import BaseProvider

log = logging.getLogger("pocket-agent")


class OpenAIProvider(BaseProvider):
    display_name = "OpenAI Codex OAuth (no API key needed)"

    @classmethod
    def setup_wizard(cls, config) -> None:
        from rich.console import Console
        from rich.prompt import Confirm

        from pocket_agent.system.auth import _is_expired, _load_token, get_access_token, logout

        console = Console()
        console.print("[dim]提示：此為官方 ChatGPT/Codex 免費配額，需於瀏覽器完成登入授權。[/dim]\n")

        token = _load_token("openai")
        if token and not _is_expired(token):
            console.print("[green]✓ 已登入 OPENAI（token 有效）[/green]")
            if not Confirm.ask("要重新登入嗎？", default=False):
                console.print()
                return
            logout("openai")
        console.print("即將開啟瀏覽器，請用你的 [bold]OPENAI 帳號[/bold] 登入並授權…\n")
        input("按 Enter 繼續…")
        get_access_token(provider="openai")
        console.print()

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
            timeout=httpx.Timeout(timeout=1800.0, connect=15.0, read=DEFAULT_STREAM_READ_TIMEOUT_SEC),
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


class OpenAIApiProvider(BaseProvider):
    display_name = "OpenAI API"

    @classmethod
    def setup_wizard(cls, config) -> None:
        cls.prompt_api_key("providers.openai-api.api_key", "OpenAI", config)

    def get_client(self) -> Any:
        api_key = config.get("providers.openai-api.api_key")
        if not api_key:
            raise ValueError("未設定 OpenAI API Key。請設定 `providers.openai-api.api_key`。")

        return AsyncOpenAI(
            api_key=api_key,
            http_client=None,
            timeout=httpx.Timeout(timeout=180.0, connect=15.0, read=DEFAULT_STREAM_READ_TIMEOUT_SEC),
        )

    def fetch_models(self) -> List[str]:
        api_key = config.get("providers.openai-api.api_key")
        if not api_key:
            return []

        try:
            with httpx.Client() as client:
                resp = client.get(
                    "https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {api_key}"}, timeout=10
                )
            if resp.status_code in (401, 403):
                log.error(f"OpenAI API auth error ({resp.status_code}): {resp.text}")
                return []
            resp.raise_for_status()
            data = resp.json()
            items = data.get("models", data.get("data", []))
            return [m["id"] for m in items if "id" in m]
        except Exception as e:
            log.error(f"OpenAI fetch models error: {e}")
            return ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "o1-preview", "o1-mini"]
