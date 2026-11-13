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
                console.print("\n[yellow]加密庫目前處於鎖定狀態，需先解鎖才能讀取或修改金鑰。[/yellow]")
                pwd = Prompt.ask("請輸入主密碼", password=True)
                if config.unlock(pwd):
                    console.print("[green]✓ 解鎖成功！[/green]\n")
                    continue
                else:
                    console.print("[red]密碼錯誤或加密庫損毀，請重試。[/red]")

        key_hint = "*" * 10 + current_key[-4:] if current_key and len(current_key) > 4 else "未設定"
        console.print(f"[dim]目前 API Key: {key_hint}[/dim]")
        new_key = Prompt.ask(f"請輸入 {display_name} API Key (直接 Enter 保持原狀)", default="")

        if new_key.strip():
            try:
                config.set(config_key, new_key.strip())
            except RequireMasterPasswordException:
                console.print("\n[yellow]系統不支援原生憑證，即將為您建立高強度的 JWE 加密庫來保護金鑰。[/yellow]")
                while True:
                    pwd1 = Prompt.ask("請設定您的[bold cyan]主密碼[/bold cyan]", password=True)
                    pwd2 = Prompt.ask("請再次輸入以確認", password=True)
                    if pwd1 == pwd2 and pwd1:
                        config.encrypted_provider.save_vault(pwd1)
                        console.print("[green]✓ 加密庫建立成功！[/green]")
                        break
                    else:
                        console.print("[red]密碼不一致或為空，請重新輸入。[/red]")

        console.print(f"[green]✓ {display_name} API Key 設定已更新[/green]\n")

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
