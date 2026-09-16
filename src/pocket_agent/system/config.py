import json
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("pocket-agent")

CONFIG_PATH = Path.home() / ".pocket-agent" / "config.json"

SUPPORTED_PROVIDERS = ["openai", "ollama", "openrouter"]


class ConfigProvider(ABC):
    """設定檔供應器介面 (Strategy)"""

    @abstractmethod
    def get(self, key: str) -> Optional[Any]:
        pass


class EnvConfigProvider(ConfigProvider):
    """從環境變數讀取設定的供應器"""

    # 定義內部 key 到環境變數的映射表
    ENV_MAP = {
        "provider": "POCKET_AGENT_PROVIDER",
        "model": "OPENAI_MODEL",
        "openai_api_key": "OPENAI_API_KEY",
        "openai_base_url": "OPENAI_BASE_URL",
        "ollama_base_url": "OLLAMA_BASE_URL",
        "openrouter_api_key": "OPENROUTER_API_KEY",
        "openrouter_base_url": "OPENROUTER_BASE_URL",
        "max_tool_rounds": "MAX_TOOL_ROUNDS",
        "log_level": "POCKET_AGENT_LOG_LEVEL",
        "openai_client_id": "OPENAI_CLIENT_ID",
        "openai_originator": "OPENAI_ORIGINATOR",
        "openai_add_organizations": "OPENAI_ADD_ORGANIZATIONS",
        "openai_codex_simplified_flow": "OPENAI_CODEX_SIMPLIFIED_FLOW",
        "openai_image_wrapper_model": "OPENAI_IMAGE_WRAPPER_MODEL",
        "use_sandbox": "USE_SANDBOX",
        "sandbox_container": "SANDBOX_CONTAINER",
        "auto_learn": "POCKET_AGENT_AUTO_LEARN",
    }

    def get(self, key: str) -> Optional[Any]:
        env_key = self.ENV_MAP.get(key)
        if env_key:
            return os.getenv(env_key)
        return None


class JsonConfigProvider(ConfigProvider):
    """從 JSON 檔案讀寫設定的供應器"""

    def __init__(self, path: Path):
        self.path = path
        self._data: Dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception as e:
                log.error("載入設定檔失敗 %s: %s", self.path, e)

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            log.error("儲存設定檔失敗 %s: %s", self.path, e)

    def get(self, key: str) -> Optional[Any]:
        return self._data.get(key)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value


class ConfigManager:
    """設定檔管理員 (Facade & Chain of Responsibility)"""

    def __init__(self):
        # 責任鏈順序：先找 Env，再找 Json
        self.json_provider = JsonConfigProvider(CONFIG_PATH)
        self.providers: List[ConfigProvider] = [
            EnvConfigProvider(),
            self.json_provider,
        ]

    def get(self, key: str, default: Any = None) -> Any:
        """依照責任鏈順序取得設定值"""
        for provider in self.providers:
            val = provider.get(key)
            if val is not None and val != "":
                return val
        return default

    def set(self, key: str, value: Any) -> None:
        """寫入設定 (一律寫入 JSON)"""
        self.json_provider.set(key, value)
        self.json_provider.save()

    def reload(self) -> None:
        """重新載入 JSON 檔案"""
        self.json_provider.load()


# 全域單例
config = ConfigManager()
