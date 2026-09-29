from abc import ABC, abstractmethod
from typing import Any, List


class BaseProvider(ABC):
    """
    Provider 策略基礎類別。
    負責封裝特定模型供應商 (如 OpenAI, Ollama 等) 的 Client 初始化與模型清單獲取邏輯。
    """

    @abstractmethod
    def get_client(self) -> Any:
        """回傳可用於該 Provider 的 Client (例如 AsyncOpenAI 的實例)"""
        pass

    @abstractmethod
    def fetch_models(self) -> List[str]:
        """回傳該 Provider 支援的模型清單"""
        pass
