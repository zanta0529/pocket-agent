import logging
from typing import Any, List

from pocket_agent.system.config import config
from pocket_agent.system.constants import PROVIDER_GEMINI_OAUTH
from pocket_agent.system.providers.base import OpenAICompatibleProvider

log = logging.getLogger("pocket-agent")

GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


class _GeminiBaseProvider(OpenAICompatibleProvider):
    base_url = GEMINI_OPENAI_BASE_URL
    fallback_models = ("gemini-1.5-pro", "gemini-1.5-flash", "gemini-2.0-flash")
    fail_on_auth_error = True

    def _build_extra_headers(self) -> dict[str, str] | None:
        project_id = config.get("providers.gemini.project_id")
        return {"x-goog-user-project": str(project_id)} if project_id else None

    def _parse_models_payload(self, data: dict[str, Any]) -> List[str]:
        items = data.get("models", data.get("data", []))
        return [m["id"] for m in items if "id" in m]


class GeminiProvider(_GeminiBaseProvider):
    display_name = "Google Gemini API"
    config_key = "providers.gemini.api_key"
    api_key_label = "Gemini"
    missing_key_message = "未設定 Gemini API Key。請設定 `providers.gemini.api_key` 或切換至 `gemini-oauth` 供應商。"


class GeminiOAuthProvider(_GeminiBaseProvider):
    display_name = "Google Gemini OAuth (no API key needed)"
    missing_key_message = "無法取得 Gemini OAuth Token。"

    @classmethod
    def setup_wizard(cls, config) -> None:
        from rich.console import Console
        from rich.prompt import Confirm, Prompt

        from pocket_agent.system.auth import _is_expired, _load_token, get_access_token, logout
        from pocket_agent.system.i18n import t

        console = Console()
        current_client_id = config.get("providers.gemini.client_id", "")
        unset_label = t("provider_setup.unset")
        console.print(f"[dim]{t('provider_setup.gemini_client_id', val=current_client_id or unset_label)}[/dim]")
        new_client_id = Prompt.ask(t("provider_setup.enter_client_id"), default="")
        if new_client_id.strip():
            config.set("providers.gemini.client_id", new_client_id.strip())

        current_client_secret = config.get("providers.gemini.client_secret", "")
        secret_display = "***" if current_client_secret else unset_label
        console.print(f"[dim]{t('provider_setup.gemini_client_secret', val=secret_display)}[/dim]")
        new_client_secret = Prompt.ask(t("provider_setup.enter_client_secret"), default="")
        if new_client_secret.strip():
            config.set("providers.gemini.client_secret", new_client_secret.strip())

        token = _load_token(PROVIDER_GEMINI_OAUTH)
        if token and not _is_expired(token):
            console.print(f"[green]{t('provider_setup.oauth_logged_in', provider='GEMINI-OAUTH')}[/green]")
            if not Confirm.ask(t("provider_setup.oauth_relogin"), default=False):
                console.print()
                return
            logout(PROVIDER_GEMINI_OAUTH)

        client_id_check = config.get("providers.gemini.client_id")
        if not client_id_check:
            console.print(f"[red]{t('provider_setup.gemini_missing_client_id')}[/red]")
            return

        console.print(f"{t('provider_setup.oauth_opening_browser', account='Google')}\n")
        input(t("provider_setup.press_enter"))
        get_access_token(provider=PROVIDER_GEMINI_OAUTH)
        console.print()

    def _resolve_api_key(self) -> str | None:
        from pocket_agent.system.auth import get_access_token

        return get_access_token(provider=PROVIDER_GEMINI_OAUTH)

    def _resolve_fetch_token(self) -> str | None:
        from pocket_agent.system.auth import _is_expired, _load_token, _try_refresh

        token = _load_token(PROVIDER_GEMINI_OAUTH)
        if token and _is_expired(token):
            client_id = config.get("providers.gemini.client_id")
            client_secret = config.get("providers.gemini.client_secret", "")
            token = _try_refresh(PROVIDER_GEMINI_OAUTH, token, client_id, client_secret)
        if not token:
            return None
        return token.get("access_token")
