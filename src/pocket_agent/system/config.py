import json
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("pocket-agent")

CONFIG_PATH = Path.home() / ".pocket-agent" / "config.json"


def _get_nested(d: dict, key: str) -> Any:
    parts = key.split(".")
    for part in parts:
        if isinstance(d, dict) and part in d:
            d = d[part]
        else:
            return None
    return d


def _set_nested(d: dict, key: str, value: Any) -> None:
    parts = key.split(".")
    for part in parts[:-1]:
        if part not in d or not isinstance(d[part], dict):
            d[part] = {}
        d = d[part]
    d[parts[-1]] = value


DEFAULT_CONFIG = {
    "active_provider": "openai",
    "active_model": "gpt-6-astra",
    "system": {
        "max_tool_rounds": 20,
        "log_level": "WARNING",
        "use_sandbox": "none",
        "sandbox_container": "pocket_sandbox",
        "auto_learn": False,
    },
    "providers": {
        "openai": {
            "auth_url": "https://auth.openai.com/oauth/authorize",
            "token_url": "https://auth.openai.com/oauth/token",
            "redirect_uri": "http://localhost:1455/auth/callback",
            "codex_base_url": "https://chatgpt.com/backend-api/codex",
            "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
            "originator": "pocket-agent",
            "add_organizations": True,
            "simplified_flow": True,
            "image_wrapper_model": "gpt-6-astra",
        },
        "openrouter": {
            "api_key": "",
            "base_url": "https://openrouter.ai/api/v1",
            "referer": "https://github.com/pocket-agent",
            "title": "Pocket Agent",
        },
        "ollama": {"base_url": "http://localhost:11434"},
        "ollama_cloud": {"api_key": "", "base_url": "https://ollama.com/v1"},
    },
}


def _deep_merge(base: dict, update: dict) -> dict:
    """遞迴合併字典，保留 update 中已有的值，補齊 base 中的預設值"""
    merged = base.copy()
    for k, v in update.items():
        if isinstance(v, dict) and k in merged and isinstance(merged[k], dict):
            merged[k] = _deep_merge(merged[k], v)
        else:
            merged[k] = v
    return merged


class ConfigProvider(ABC):
    """設定檔供應器介面 (Strategy)"""

    @abstractmethod
    def get(self, key: str) -> Optional[Any]:
        pass


class EnvConfigProvider(ConfigProvider):
    """從環境變數讀取設定的供應器"""

    # 定義內部 key 到環境變數的映射表
    ENV_MAP = {
        "active_provider": "POCKET_AGENT_PROVIDER",
        "active_model": "OPENAI_MODEL",
        "providers.openai.api_key": "OPENAI_API_KEY",
        "providers.openai.base_url": "OPENAI_BASE_URL",
        "providers.ollama.base_url": "OLLAMA_BASE_URL",
        "providers.openrouter.api_key": "OPENROUTER_API_KEY",
        "providers.openrouter.base_url": "OPENROUTER_BASE_URL",
        "system.max_tool_rounds": "MAX_TOOL_ROUNDS",
        "system.log_level": "POCKET_AGENT_LOG_LEVEL",
        "providers.openai.client_id": "OPENAI_CLIENT_ID",
        "providers.openai.originator": "OPENAI_ORIGINATOR",
        "providers.openai.add_organizations": "OPENAI_ADD_ORGANIZATIONS",
        "providers.openai.simplified_flow": "OPENAI_CODEX_SIMPLIFIED_FLOW",
        "providers.openai.image_wrapper_model": "OPENAI_IMAGE_WRAPPER_MODEL",
        "system.use_sandbox": "USE_SANDBOX",
        "system.sandbox_container": "SANDBOX_CONTAINER",
        "system.auto_learn": "POCKET_AGENT_AUTO_LEARN",
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
        file_existed = self.path.exists()
        if file_existed:
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception as e:
                log.error("載入設定檔失敗 %s: %s", self.path, e)

        # 深度合併預設值，補齊缺少的欄位
        self._data = _deep_merge(DEFAULT_CONFIG, self._data)

        # 如果檔案不存在，則寫入完整的預設設定檔作為範本
        if not file_existed:
            self.save()

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            log.error("儲存設定檔失敗 %s: %s", self.path, e)

    def get(self, key: str) -> Optional[Any]:
        return _get_nested(self._data, key)

    def set(self, key: str, value: Any) -> None:
        _set_nested(self._data, key, value)


class AuthException(Exception):
    """授權失敗或缺失金鑰時拋出的例外"""

    pass


class KeyringConfigProvider(ConfigProvider):
    """從作業系統 Keyring 讀取敏感設定的供應器"""

    def __init__(self):

        self.service_name = "pocket-agent"

    def get(self, key: str) -> Optional[Any]:
        if "api_key" in key or "token" in key:
            try:
                import keyring

                val = keyring.get_password(self.service_name, key)
                return val
            except Exception:
                pass
        return None

    def set(self, key: str, value: str) -> None:
        if "api_key" in key or "token" in key:
            try:
                import keyring

                keyring.set_password(self.service_name, key, value)
            except Exception:
                pass


class ConfigManager:
    """設定檔管理員 (Facade & Chain of Responsibility)"""

    def __init__(self):
        # 責任鏈順序：先找 Env，再找 Keyring，最後找 Json
        self.json_provider = JsonConfigProvider(CONFIG_PATH)
        self.keyring_provider = KeyringConfigProvider()
        self.providers: List[ConfigProvider] = [
            EnvConfigProvider(),
            self.keyring_provider,
            self.json_provider,
        ]

    def get(self, key: str, default: Any = None) -> Any:
        """依照責任鏈順序取得設定值"""
        val_found = None
        for provider in self.providers:
            val = provider.get(key)
            if val is not None and val != "":
                val_found = val
                break

        if val_found is None:
            val_found = default

        # 若需要 api_key 卻找不到，則拋出 AuthException (延遲載入)
        if ("api_key" in key or "token" in key) and (
            val_found is None or val_found == "" or val_found == "USE_KEYRING"
        ):
            raise AuthException(f"找不到 {key}，請先執行設定或解鎖。")

        return val_found

    def set(self, key: str, value: Any) -> None:
        """寫入設定。若是敏感資訊，優先寫入 Keyring 並在 JSON 留下佔位符"""
        if "api_key" in key or "token" in key:
            self.keyring_provider.set(key, str(value))
            self.json_provider.set(key, "USE_KEYRING")
        else:
            self.json_provider.set(key, value)

        self.json_provider.save()

    def reload(self) -> None:
        """重新載入 JSON 檔案"""
        self.json_provider.load()


# 全域單例
config = ConfigManager()
