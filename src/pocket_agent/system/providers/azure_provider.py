"""Azure OpenAI / Azure AI Foundry 供應商策略（同時支援新一代 v1 API 與經典 api-version 部署模式）。"""

import logging
import sys
from typing import Any, List
from urllib.parse import urlparse

import httpx
from openai import AsyncAzureOpenAI as _DefaultAsyncAzureOpenAI
from openai import AsyncOpenAI as _DefaultAsyncOpenAI

from pocket_agent.system.config import MissingKeyException, config
from pocket_agent.system.i18n import t
from pocket_agent.system.providers.base import OpenAICompatibleProvider

log = logging.getLogger("pocket-agent")


def _is_valid_http_url(url: str) -> bool:
    """檢查 URL 是否為合法的 http:// 或 https:// 端點位址。"""
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _is_v1_endpoint(url: str) -> bool:
    """判斷端點路徑是否已明確指定新一代 v1 或 models 路徑。"""
    path = urlparse(url).path.rstrip("/").lower()
    return path.endswith("/openai/v1") or path.endswith("/v1") or path.endswith("/models")


def _normalize_v1_base_url(endpoint: str) -> str:
    """將 Azure 資源根網址正規化為新一代 `/openai/v1` 基底網址；已含路徑者保持原樣。"""
    cleaned = endpoint.strip().rstrip("/")
    if _is_v1_endpoint(cleaned):
        return cleaned
    return f"{cleaned}/openai/v1"


class AzureOpenAIProvider(OpenAICompatibleProvider):
    """Azure OpenAI / Azure AI Foundry 雙模式供應商策略。"""

    display_name = "Azure OpenAI / AI Foundry"
    config_key = "providers.azure.api_key"
    endpoint_config_key = "providers.azure.endpoint"
    api_version_config_key = "providers.azure.api_version"
    api_key_label = "Azure OpenAI"
    timeout_seconds = 300.0
    connect_timeout_seconds = 30.0

    @classmethod
    def setup_wizard(cls, cfg) -> None:
        from rich.console import Console
        from rich.prompt import Prompt

        console = Console()
        current_endpoint = (cfg.get(cls.endpoint_config_key) or "").strip()
        display_endpoint = current_endpoint or t("provider_setup.unset")
        console.print(f"[dim]{t('provider_setup.current_base_url', url=display_endpoint)}[/dim]")

        while True:
            raw_url = Prompt.ask(
                t("provider_setup.enter_azure_endpoint"),
                default=current_endpoint,
            ).strip()
            cleaned_url = raw_url.rstrip("/")
            if _is_valid_http_url(cleaned_url):
                cfg.set(cls.endpoint_config_key, cleaned_url)
                break
            console.print(f"[red]{t('provider_setup.invalid_base_url')}[/red]")

        current_version = (cfg.get(cls.api_version_config_key) or "").strip()
        display_version = current_version or t("provider_setup.azure_v1_default")
        console.print(f"[dim]{t('provider_setup.current_azure_api_version', version=display_version)}[/dim]")
        new_version = Prompt.ask(
            t("provider_setup.enter_azure_api_version"),
            default=current_version,
        ).strip()
        cfg.set(cls.api_version_config_key, new_version)

        cls.prompt_api_key(cls.config_key, cls.api_key_label, cfg)

    def _resolve_endpoint(self) -> str:
        raw_url = (config.get(self.endpoint_config_key) or "").strip().rstrip("/")
        if not _is_valid_http_url(raw_url):
            raise ValueError(t("provider_setup.azure_missing_endpoint"))
        return raw_url

    def _resolve_api_version(self) -> str:
        return (config.get(self.api_version_config_key) or "").strip()

    def _resolve_api_key(self) -> str | None:
        return config.get(self.config_key) if self.config_key else None

    def get_client(self) -> Any:
        endpoint = self._resolve_endpoint()
        api_key = self._resolve_api_key()
        if not api_key or not str(api_key).strip():
            raise ValueError(t("provider_setup.azure_missing_key"))

        api_version = self._resolve_api_version()
        timeout = httpx.Timeout(
            timeout=self.timeout_seconds,
            connect=self.connect_timeout_seconds,
        )
        http_client = self.build_async_http_client(timeout)
        mod = sys.modules.get(self.__class__.__module__)

        if api_version and not _is_v1_endpoint(endpoint):
            azure_cls = getattr(mod, "AsyncAzureOpenAI", _DefaultAsyncAzureOpenAI)
            return azure_cls(
                azure_endpoint=endpoint,
                api_key=str(api_key).strip(),
                api_version=api_version,
                http_client=http_client,
                timeout=timeout,
            )

        v1_base_url = _normalize_v1_base_url(endpoint)
        openai_cls = getattr(mod, "AsyncOpenAI", _DefaultAsyncOpenAI)
        return openai_cls(
            api_key=str(api_key).strip(),
            base_url=v1_base_url,
            http_client=http_client,
            timeout=timeout,
        )

    def fetch_models(self) -> List[str]:
        try:
            endpoint = self._resolve_endpoint()
            token = self._resolve_api_key()
        except ValueError, MissingKeyException:
            return []

        if not token or not str(token).strip():
            return []

        clean_token = str(token).strip()
        api_version = self._resolve_api_version()
        headers = {
            "api-key": clean_token,
            "Authorization": f"Bearer {clean_token}",
        }

        if api_version and not _is_v1_endpoint(endpoint):
            models_url = f"{endpoint}/openai/models"
            params: dict[str, str] | None = {"api-version": api_version}
        else:
            models_url = f"{_normalize_v1_base_url(endpoint)}/models"
            params = None

        try:
            with httpx.Client() as client:
                resp = client.get(models_url, headers=headers, params=params, timeout=10)
            if resp.status_code == 200:
                return self._parse_models_payload(resp.json())
            return []
        except Exception as e:
            log.error("Azure OpenAI fetch models error: %s", e)
            return []
