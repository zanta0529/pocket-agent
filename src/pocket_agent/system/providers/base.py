import importlib.util
import logging
from abc import ABC, abstractmethod
from typing import Any, List

import httpx

log = logging.getLogger("pocket-agent")


class BaseProvider(ABC):
    """
    Provider 策略基礎類別。
    負責封裝特定模型供應商 (如 OpenAI, Ollama 等) 的 Client 初始化與模型清單獲取邏輯。
    """

    display_name: str = "Unknown Provider"

    @classmethod
    def setup_wizard(cls, config) -> None:
        """
        執行該供應商專屬的互動式 CLI 設定流程。
        各子類別實作自己的 API Key 提示或 OAuth 瀏覽器登入。
        """
        pass

    @staticmethod
    def prompt_api_key(config_key: str, display_name: str, config) -> None:
        """共用輔助函式：提示並更新 API Key"""
        from rich.console import Console
        from rich.prompt import Prompt

        from pocket_agent.system.config import AuthException, MissingKeyException, RequireMasterPasswordException
        from pocket_agent.system.i18n import t

        console = Console()
        current_key = ""
        while True:
            try:
                current_key = config.get(config_key, "")
                break
            except MissingKeyException:
                current_key = ""
                break
            except AuthException:
                console.print(f"\n[yellow]{t('provider_setup.vault_locked')}[/yellow]")
                pwd = Prompt.ask(t("provider_setup.master_password_prompt"), password=True)
                if config.unlock(pwd):
                    console.print(f"[green]{t('provider_setup.unlock_success')}[/green]\n")
                    continue
                else:
                    console.print(f"[red]{t('provider_setup.unlock_failed')}[/red]")

        key_hint = "*" * 10 + current_key[-4:] if current_key and len(current_key) > 4 else t("provider_setup.unset")
        console.print(f"[dim]{t('provider_setup.current_api_key', hint=key_hint)}[/dim]")
        new_key = Prompt.ask(t("provider_setup.enter_api_key", name=display_name), default="")

        if new_key.strip():
            try:
                config.set(config_key, new_key.strip())
            except RequireMasterPasswordException:
                console.print(f"\n[yellow]{t('provider_setup.vault_creating')}[/yellow]")
                while True:
                    pwd1 = Prompt.ask(t("provider_setup.set_master_password"), password=True)
                    pwd2 = Prompt.ask(t("provider_setup.confirm_master_password"), password=True)
                    if pwd1 == pwd2 and pwd1:
                        config.encrypted_provider.save_vault(pwd1)
                        console.print(f"[green]{t('provider_setup.vault_created')}[/green]")
                        break
                    else:
                        console.print(f"[red]{t('provider_setup.password_mismatch')}[/red]")

        console.print(f"[green]{t('provider_setup.api_key_updated', name=display_name)}[/green]\n")

    @staticmethod
    def build_async_http_client(timeout: httpx.Timeout) -> httpx.AsyncClient:
        """建立具備連線池與可選 HTTP/2 支援的共用非同步 HTTP Client。"""
        has_h2 = importlib.util.find_spec("h2") is not None
        limits = httpx.Limits(max_keepalive_connections=20, max_connections=50, keepalive_expiry=60.0)
        return httpx.AsyncClient(timeout=timeout, limits=limits, http2=has_h2)

    @abstractmethod
    def get_client(self) -> Any:
        """回傳可用於該 Provider 的 Client (例如 AsyncOpenAI 的實例)"""
        pass

    @abstractmethod
    def fetch_models(self) -> List[str]:
        """回傳該 Provider 支援的模型清單"""
        pass


class OpenAICompatibleProvider(BaseProvider):
    """OpenAI 相容 API 供應商之共用樣板基底（Template Method）。"""

    config_key: str = ""
    api_key_label: str = ""
    base_url: str = ""
    fallback_models: tuple[str, ...] = ()
    missing_key_message: str = ""
    timeout_seconds: float = 180.0
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float | None = None
    use_shared_http_client: bool = False
    fail_on_auth_error: bool = False

    @classmethod
    def setup_wizard(cls, config) -> None:
        if cls.config_key:
            cls.prompt_api_key(cls.config_key, cls.api_key_label or cls.display_name, config)

    def _resolve_api_key(self) -> str | None:
        from pocket_agent.system.config import config

        return config.get(self.config_key) if self.config_key else None

    def _resolve_fetch_token(self) -> str | None:
        return self._resolve_api_key()

    def _build_extra_headers(self) -> dict[str, str] | None:
        return None

    def _models_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/models"

    def _parse_models_payload(self, data: dict[str, Any]) -> List[str]:
        items = data.get("models", data.get("data", []))
        return [m["id"] for m in items if "id" in m]

    def get_client(self) -> Any:
        import sys

        from openai import AsyncOpenAI as _DefaultAsyncOpenAI

        api_key = self._resolve_api_key()
        if not api_key:
            label = self.api_key_label or self.display_name
            msg = self.missing_key_message or (
                f"未設定 {label} API Key。請設定 `{self.config_key}` 或透過 `agent setup` 進行設定。"
            )
            raise ValueError(msg)

        if self.read_timeout_seconds is not None:
            timeout = httpx.Timeout(
                timeout=self.timeout_seconds,
                connect=self.connect_timeout_seconds,
                read=self.read_timeout_seconds,
            )
        else:
            timeout = httpx.Timeout(timeout=self.timeout_seconds, connect=self.connect_timeout_seconds)

        kwargs: dict[str, Any] = {
            "api_key": api_key,
            "http_client": self.build_async_http_client(timeout) if self.use_shared_http_client else None,
            "timeout": timeout,
        }
        if self.base_url:
            kwargs["base_url"] = self.base_url
        extra_headers = self._build_extra_headers()
        if extra_headers:
            kwargs["default_headers"] = extra_headers
        mod = sys.modules.get(self.__class__.__module__)
        client_cls = getattr(mod, "AsyncOpenAI", _DefaultAsyncOpenAI)
        return client_cls(**kwargs)

    def fetch_models(self) -> List[str]:
        token = self._resolve_fetch_token()
        if not token:
            return []

        headers = {"Authorization": f"Bearer {token}"}
        extra_headers = self._build_extra_headers()
        if extra_headers:
            headers.update(extra_headers)

        try:
            with httpx.Client() as client:
                resp = client.get(self._models_url(), headers=headers, timeout=10)
            if self.fail_on_auth_error and resp.status_code in (401, 403):
                log.error("%s auth error (%s): %s", self.display_name, resp.status_code, resp.text)
                return []
            if resp.status_code == 200:
                return self._parse_models_payload(resp.json())
            if self.fail_on_auth_error:
                resp.raise_for_status()
            return list(self.fallback_models)
        except Exception as e:
            log.error("%s fetch models error: %s", self.display_name, e)
            return list(self.fallback_models)
