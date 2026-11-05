import logging
from typing import Any, List

import httpx
from openai import AsyncOpenAI

from pocket_agent.system.config import config
from pocket_agent.system.providers.base import BaseProvider

log = logging.getLogger("pocket-agent")

GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


class GeminiProvider(BaseProvider):
    display_name = "Google Gemini API"

    @classmethod
    def setup_wizard(cls, config) -> None:
        cls.prompt_api_key("providers.gemini.api_key", "Gemini", config)

    def get_client(self) -> Any:
        api_key = config.get("providers.gemini.api_key")
        if not api_key:
            raise ValueError(
                "未設定 Gemini API Key。請設定 `providers.gemini.api_key` 或切換至 `gemini-oauth` 供應商。"
            )

        project_id = config.get("providers.gemini.project_id")
        extra_headers = {"x-goog-user-project": str(project_id)} if project_id else None

        return AsyncOpenAI(
            api_key=api_key,
            base_url=GEMINI_OPENAI_BASE_URL,
            default_headers=extra_headers,
            http_client=None,
            timeout=httpx.Timeout(timeout=180.0, connect=10.0),
        )

    def fetch_models(self) -> List[str]:
        api_key = config.get("providers.gemini.api_key")
        if not api_key:
            return []

        headers = {"Authorization": f"Bearer {api_key}"}
        project_id = config.get("providers.gemini.project_id")
        if project_id:
            headers["x-goog-user-project"] = str(project_id)

        try:
            with httpx.Client() as client:
                resp = client.get(f"{GEMINI_OPENAI_BASE_URL}models", headers=headers, timeout=10)
            if resp.status_code in (401, 403):
                log.error(f"Gemini API auth error ({resp.status_code}): {resp.text}")
                return []
            resp.raise_for_status()
            data = resp.json()
            items = data.get("models", data.get("data", []))
            return [m["id"] for m in items if "id" in m]
        except Exception as e:
            log.error(f"Gemini fetch models error: {e}")
            return ["gemini-1.5-pro", "gemini-1.5-flash", "gemini-2.0-flash"]


class GeminiOAuthProvider(BaseProvider):
    display_name = "Google Gemini OAuth (no API key needed)"

    @classmethod
    def setup_wizard(cls, config) -> None:
        from rich.console import Console
        from rich.prompt import Confirm, Prompt

        from pocket_agent.system.auth import _is_expired, _load_token, get_access_token, logout

        console = Console()
        current_client_id = config.get("providers.gemini.client_id", "")
        console.print(f"[dim]目前 Google OAuth Client ID: {current_client_id or '未設定'}[/dim]")
        new_client_id = Prompt.ask("請輸入 Client ID (直接 Enter 保持原狀)", default="")
        if new_client_id.strip():
            config.set("providers.gemini.client_id", new_client_id.strip())

        current_client_secret = config.get("providers.gemini.client_secret", "")
        console.print(
            f"[dim]目前 Google OAuth Client Secret (選填): {'***' if current_client_secret else '未設定'}[/dim]"
        )
        new_client_secret = Prompt.ask("請輸入 Client Secret (直接 Enter 保持原狀)", default="")
        if new_client_secret.strip():
            config.set("providers.gemini.client_secret", new_client_secret.strip())

        token = _load_token("gemini-oauth")
        if token and not _is_expired(token):
            console.print("[green]✓ 已登入 GEMINI-OAUTH（token 有效）[/green]")
            if not Confirm.ask("要重新登入嗎？", default=False):
                console.print()
                return
            logout("gemini-oauth")

        client_id_check = config.get("providers.gemini.client_id")
        if not client_id_check:
            console.print("[red]✗ 必須提供 Client ID 才能進行 OAuth 授權，請重試。[/red]")
            return

        console.print("即將開啟瀏覽器，請用你的 [bold]Google 帳號[/bold] 登入並授權…\n")
        input("按 Enter 繼續…")
        get_access_token(provider="gemini-oauth")
        console.print()

    def get_client(self) -> Any:
        from pocket_agent.system.auth import get_access_token

        api_key = get_access_token(provider="gemini-oauth")
        if not api_key:
            raise ValueError("無法取得 Gemini OAuth Token。")

        project_id = config.get("providers.gemini.project_id")
        extra_headers = {"x-goog-user-project": str(project_id)} if project_id else None

        return AsyncOpenAI(
            api_key=api_key,
            base_url=GEMINI_OPENAI_BASE_URL,
            default_headers=extra_headers,
            http_client=None,
            timeout=httpx.Timeout(timeout=180.0, connect=10.0),
        )

    def fetch_models(self) -> List[str]:
        from pocket_agent.system.auth import _is_expired, _load_token, _try_refresh

        token = _load_token("gemini-oauth")
        if token and _is_expired(token):
            client_id = config.get("providers.gemini.client_id")
            client_secret = config.get("providers.gemini.client_secret", "")
            token = _try_refresh("gemini-oauth", token, client_id, client_secret)

        if not token:
            return []

        access_token = token.get("access_token")
        if not access_token:
            return []

        headers = {"Authorization": f"Bearer {access_token}"}
        project_id = config.get("providers.gemini.project_id")
        if project_id:
            headers["x-goog-user-project"] = str(project_id)

        try:
            with httpx.Client() as client:
                resp = client.get(f"{GEMINI_OPENAI_BASE_URL}models", headers=headers, timeout=10)
            if resp.status_code in (401, 403):
                log.error(f"Gemini OAuth auth error ({resp.status_code}): {resp.text}")
                return []
            resp.raise_for_status()
            data = resp.json()
            items = data.get("models", data.get("data", []))
            return [m["id"] for m in items if "id" in m]
        except Exception as e:
            log.error(f"Gemini OAuth fetch models error: {e}")
            return ["gemini-1.5-pro", "gemini-1.5-flash", "gemini-2.0-flash"]
