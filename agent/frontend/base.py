from abc import ABC, abstractmethod


class FrontendStrategy(ABC):
    """前端介面執行策略的抽象基礎類別"""

    @abstractmethod
    def run(self, system_prompt: str = "You are a helpful assistant."):
        """執行前端介面主迴圈"""
        pass
