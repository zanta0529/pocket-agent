"""通用 OpenAI-Compatible 端點供應商（支援 LM Studio、vLLM、llama.cpp 與企業內網 AI Gateway）。"""

import ipaddress
import logging
import sys
from typing import Any, List
from urllib.parse import urlparse

import httpx
from openai import AsyncOpenAI as _DefaultAsyncOpenAI

from pocket_agent.system.config import MissingKeyException, config
from pocket_agent.system.i18n import t
from pocket_agent.system.providers.base import OpenAICompatibleProvider

log = logging.getLogger("pocket-agent")

_PLACEHOLDER_API_KEY = "not-needed"


def _is_valid_http_url(url: str) -> bool:
    """檢查 URL 是否為合法的 http:// 或 https:// 端點位址。"""
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _is_loopback_url(url: str) -> bool:
    """判斷目標端點是否為本機迴路位址，避免本機推論服務誤經系統 Proxy。"""
    host = (urlparse(url).hostname or "").strip().lower()
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class OpenAICompatibleCustomProvider(OpenAICompatibleProvider):
    """通用 OpenAI-Compatible 端點供應商策略。"""

    display_name = "OpenAI-Compatible (Local / Gateway)"
    config_key = "providers.openai_compatible.api_key"
    base_url_config_key = "providers.openai_compatible.base_url"
    api_key_label = "OpenAI-Compatible"
    timeout_seconds = 600.0
    connect_timeout_seconds = 30.0

    @classmethod
    def setup_wizard(cls, cfg) -> None:
        from rich.console import Console
        from rich.prompt import Prompt

        console = Console()
        current_url = (cfg.get(cls.base_url_config_key) or "").strip()
        display_url = current_url or t("provider_setup.unset")
        console.print(f"[dim]{t('provider_setup.current_base_url', url=display_url)}[/dim]")

        while True:
            raw_url = Prompt.ask(
                t("provider_setup.enter_openai_compatible_url"),
                default=current_url,
            ).strip()
            cleaned_url = raw_url.rstrip("/")
            if _is_valid_http_url(cleaned_url):
                cfg.set(cls.base_url_config_key, cleaned_url)
                break
            console.print(f"[red]{t('provider_setup.invalid_base_url')}[/red]")

        console.print(f"[dim]{t('provider_setup.optional_api_key_hint')}[/dim]")
        cls.prompt_api_key(cls.config_key, cls.api_key_label, cfg)

    def _resolve_base_url(self) -> str:
        raw_url = (config.get(self.base_url_config_key) or "").strip().rstrip("/")
        if not _is_valid_http_url(raw_url):
            raise ValueError(t("provider_setup.openai_compatible_missing_url"))
        return raw_url

    def _resolve_api_key(self) -> str | None:
        try:
            key = config.get(self.config_key)
        except MissingKeyException:
            return None
        if isinstance(key, str) and key.strip():
            return key.strip()
        return None

    def get_client(self) -> Any:
        base_url = self._resolve_base_url()
        api_key = self._resolve_api_key()
        trust_env = not _is_loopback_url(base_url)

        timeout = httpx.Timeout(
            timeout=self.timeout_seconds,
            connect=self.connect_timeout_seconds,
        )
        http_client = httpx.AsyncClient(trust_env=trust_env, timeout=timeout)

        mod = sys.modules.get(self.__class__.__module__)
        client_cls = getattr(mod, "AsyncOpenAI", _DefaultAsyncOpenAI)
        return client_cls(
            api_key=api_key or _PLACEHOLDER_API_KEY,
            base_url=base_url,
            http_client=http_client,
            timeout=timeout,
        )

    def fetch_models(self) -> List[str]:
        try:
            base_url = self._resolve_base_url()
        except ValueError:
            return []

        try:
            token = self._resolve_api_key()
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            trust_env = not _is_loopback_url(base_url)
            with httpx.Client(trust_env=trust_env) as client:
                resp = client.get(f"{base_url}/models", headers=headers, timeout=10)
            if resp.status_code == 200:
                return self._parse_models_payload(resp.json())
            return []
        except Exception as e:
            log.error("OpenAI-Compatible fetch models error: %s", e)
            return []
