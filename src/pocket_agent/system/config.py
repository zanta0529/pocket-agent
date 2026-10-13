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
        "context_window": None,
        "context_reserve_output": 4096,
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
    """加密庫已鎖定，需要主密碼解鎖時拋出的例外"""

    pass


class MissingKeyException(Exception):
    """找不到金鑰 (全新安裝或尚未設定) 時拋出的例外"""

    pass


class RequireMasterPasswordException(Exception):
    """金鑰需寫入加密庫，但尚未建立主密碼時拋出的例外"""

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

    def set(self, key: str, value: str) -> bool:
        if "api_key" in key or "token" in key:
            try:
                import keyring

                # 測試是否為 fail backend (無實際 keyring 功能)
                if isinstance(keyring.get_keyring(), keyring.backends.fail.Keyring):
                    return False
                keyring.set_password(self.service_name, key, value)
                return True
            except Exception:
                return False
        return False


def _base64url_encode(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _base64url_decode(s: str) -> bytes:
    import base64

    padding = "=" * (4 - (len(s) % 4))
    return base64.urlsafe_b64decode(s + padding)


class EncryptedConfigProvider(ConfigProvider):
    """基於 JWE (AES-256-GCM + Argon2id) 的加密設定供應器 (Portable 模式)"""

    def __init__(self, path: Path):
        self.path = path
        self._cache = {}
        self._is_unlocked = False
        self._cek = None

    def _derive_key(self, password: str, salt: bytes, t: int, m: int, p: int) -> bytes:
        from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

        kdf = Argon2id(salt=salt, length=32, iterations=t, lanes=p, memory_cost=m)
        return kdf.derive(password.encode("utf-8"))

    def unlock(self, password: str) -> bool:
        if not self.path.exists():
            return False

        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            jwe_string = self.path.read_text(encoding="utf-8").strip()
            parts = jwe_string.split(".")
            if len(parts) != 5:
                return False

            header_b64, encrypted_key_b64, iv_b64, ciphertext_b64, tag_b64 = parts

            # 解析 Header
            header_json = _base64url_decode(header_b64).decode("utf-8")
            header = json.loads(header_json)

            if header.get("alg") != "dir" or header.get("enc") != "A256GCM":
                log.warning("不支援的 JWE 演算法")
                return False

            salt = _base64url_decode(header["salt"])
            t = int(header["t"])
            m = int(header["m"])
            p = int(header["p"])

            # 衍生 256-bit CEK
            cek = self._derive_key(password, salt, t, m, p)

            # 準備 GCM 參數
            iv = _base64url_decode(iv_b64)
            ciphertext = _base64url_decode(ciphertext_b64)
            tag = _base64url_decode(tag_b64)
            ciphertext_and_tag = ciphertext + tag
            aad = header_b64.encode("ascii")

            # GCM 解密 (含完整性驗證)
            aesgcm = AESGCM(cek)
            plaintext = aesgcm.decrypt(iv, ciphertext_and_tag, aad)

            self._cache = json.loads(plaintext.decode("utf-8"))
            self._cek = cek
            self._is_unlocked = True
            return True
        except Exception as e:
            log.warning("JWE 解鎖失敗或密文損壞: %s", e)
            return False

    def save_vault(self, password: Optional[str] = None) -> None:
        """將記憶體中的設定以 JWE 格式加密存檔 (供設定精靈使用)"""
        import os

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        salt = os.urandom(16)
        iv = os.urandom(12)

        t, m, p = 2, 65536, 4

        if password:
            self._cek = self._derive_key(password, salt, t, m, p)

        if not self._cek:
            raise RequireMasterPasswordException("建立加密庫需要主密碼")

        header = {
            "alg": "dir",
            "enc": "A256GCM",
            "kdf": "Argon2id",
            "salt": _base64url_encode(salt),
            "t": t,
            "m": m,
            "p": p,
        }
        header_json = json.dumps(header, separators=(",", ":"))
        header_b64 = _base64url_encode(header_json.encode("utf-8"))
        aad = header_b64.encode("ascii")

        plaintext = json.dumps(self._cache).encode("utf-8")

        aesgcm = AESGCM(self._cek)
        encrypted_data = aesgcm.encrypt(iv, plaintext, aad)

        ciphertext = encrypted_data[:-16]
        tag = encrypted_data[-16:]

        jwe = f"{header_b64}..{_base64url_encode(iv)}.{_base64url_encode(ciphertext)}.{_base64url_encode(tag)}"

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(jwe, encoding="utf-8")
        self._is_unlocked = True

    def get(self, key: str) -> Optional[Any]:
        if not self._is_unlocked:
            return None
        return self._cache.get(key)

    def set(self, key: str, value: str) -> None:
        """此方法僅在已解鎖狀態下能寫入記憶體，實際存檔需呼叫 save_vault"""
        self._cache[key] = value


class ConfigManager:
    """設定檔管理員 (Facade & Chain of Responsibility)"""

    def __init__(self):
        # 責任鏈順序：先找 Env，再找 Keyring，接著找加密庫，最後找 Json
        self.json_provider = JsonConfigProvider(CONFIG_PATH)
        self.keyring_provider = KeyringConfigProvider()

        SECRETS_PATH = Path.home() / ".pocket-agent" / "secrets.json"
        self.encrypted_provider = EncryptedConfigProvider(SECRETS_PATH)

        self.providers: List[ConfigProvider] = [
            EnvConfigProvider(),
            self.keyring_provider,
            self.encrypted_provider,
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

        # 若需要 api_key 卻找不到，進行精準延遲載入判斷
        if "api_key" in key or "token" in key:
            if val_found == "USE_ENCRYPTED" and not self.encrypted_provider._is_unlocked:
                raise AuthException(f"需要存取 {key}，加密庫已鎖定，請解鎖。")
            if val_found is None or val_found == "" or val_found == "USE_KEYRING" or val_found == "USE_ENCRYPTED":
                raise MissingKeyException(f"找不到 {key}，請先執行設定。")

        return val_found

    def set(self, key: str, value: Any) -> None:
        """寫入設定。若是敏感資訊，優先寫入 Keyring，若失敗則降級至 JWE 加密庫"""
        if "api_key" in key or "token" in key:
            success = self.keyring_provider.set(key, str(value))
            if success:
                self.json_provider.set(key, "USE_KEYRING")
            else:
                self.json_provider.set(key, "USE_ENCRYPTED")
                self.encrypted_provider.set(key, str(value))
                if not self.encrypted_provider._is_unlocked and not self.encrypted_provider._cek:
                    # 尚未建立過密碼，拋出例外請呼叫端提示使用者，然後再由呼叫端自行呼叫 save_vault(password)
                    self.json_provider.save()
                    raise RequireMasterPasswordException("需要設定主密碼以建立加密庫")
                else:
                    self.encrypted_provider.save_vault()
        else:
            self.json_provider.set(key, value)

        self.json_provider.save()

    def reload(self) -> None:
        """重新載入 JSON 檔案"""
        self.json_provider.load()

    def unlock(self, password: str) -> bool:
        """
        驗證並解鎖加密金鑰庫 (Portable 模式)。
        將密碼交由 EncryptedConfigProvider 進行 Argon2id 解密。
        """
        return self.encrypted_provider.unlock(password)


# 全域單例
config = ConfigManager()
