"""執行期記憶體憑證快取（TTL 過期）與多來源解析策略（Strategy Pattern）。"""

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from mcp_servers._shared.bootstrap import t
from mcp_servers._shared.tty import DEFAULT_TTY_PROMPTER, TtyPrompter
from mcp_servers._shared.validation import clean_param, is_empty_or_null


class CredentialCache:
    """執行緒安全之執行期記憶體憑證快取（具閒置 TTL 自動清除，絕不落盤）。"""

    def __init__(
        self,
        ttl_seconds: float = 600.0,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = threading.RLock()
        self._store: dict[str, str] = {}
        self._secret_store: dict[str, str] = {}
        self._target_store: dict[tuple[str, ...], tuple[str, float]] = {}
        self._updated_at: float = 0.0

    @property
    def lock(self) -> threading.RLock:
        """暴露內部可重入鎖，供複合解析流程原子化存取。"""
        return self._lock

    def _evict_if_expired_locked(self) -> None:
        now = self._clock()
        if (self._store or self._secret_store) and self._updated_at > 0 and (now - self._updated_at > self.ttl_seconds):
            self._store.clear()
            self._secret_store.clear()
            self._updated_at = 0.0
        expired = [key for key, (_, ts) in self._target_store.items() if now - ts > self.ttl_seconds]
        for key in expired:
            self._target_store.pop(key, None)

    def get(self, key: str, default: str = "") -> str:
        with self._lock:
            self._evict_if_expired_locked()
            return self._store.get(key, default)

    def set(self, key: str, value: str) -> None:
        with self._lock:
            self._evict_if_expired_locked()
            self._store[key] = value
            self._updated_at = self._clock()

    def update(self, values: dict[str, str]) -> None:
        with self._lock:
            self._evict_if_expired_locked()
            self._store.update(values)
            self._updated_at = self._clock()

    def get_secret(self, key: str, default: str = "") -> str:
        """讀取敏感憑證（與一般連線中繼資料隔離存放）。"""
        with self._lock:
            self._evict_if_expired_locked()
            return self._secret_store.get(key, default)

    def set_secret(self, key: str, value: str) -> None:
        """寫入敏感憑證（僅存於獨立記憶體雜湊表，與一般連線中繼資料隔離）。"""
        with self._lock:
            self._evict_if_expired_locked()
            self._secret_store[key] = value
            self._updated_at = self._clock()

    def pop(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            self._evict_if_expired_locked()
            self._secret_store.pop(key, None)
            return self._store.pop(key, default)

    def get_target_secret(self, target_key: tuple[str, ...]) -> str:
        with self._lock:
            self._evict_if_expired_locked()
            entry = self._target_store.get(target_key)
            return entry[0] if entry is not None else ""

    def set_target_secret(self, target_key: tuple[str, ...], secret: str) -> None:
        with self._lock:
            self._evict_if_expired_locked()
            self._target_store[target_key] = (secret, self._clock())

    def clear(self, *, target_host: str = "", host_env_key: str = "") -> None:
        """清除全部快取；若指定 target_host 則僅清除對應主機之快取項目。"""
        with self._lock:
            clean_host = target_host.strip()
            if not clean_host:
                self._store.clear()
                self._secret_store.clear()
                self._target_store.clear()
                self._updated_at = 0.0
                return
            for key in [k for k in self._target_store if k and k[0] == clean_host]:
                self._target_store.pop(key, None)
            if host_env_key and self._store.get(host_env_key) == clean_host:
                self._store.clear()
                self._secret_store.clear()
                self._updated_at = 0.0


@dataclass(frozen=True)
class CredentialFieldSpec:
    """單一連線參數欄位之解析規格。"""

    env_key: str
    prompt_text: str = ""
    default: str = ""
    is_secret: bool = False
    required: bool = True
    validator: Callable[[str], str] | None = None


class CredentialResolver:
    """依「注入參數 → 記憶體快取 → 環境變數 → TTY 互動詢問」順序解析連線參數。"""

    def __init__(
        self,
        cache: CredentialCache,
        *,
        prompter: TtyPrompter = DEFAULT_TTY_PROMPTER,
        hint: str = "",
    ) -> None:
        self.cache = cache
        self.prompter = prompter
        self.hint = hint

    def resolve_field(
        self,
        spec: CredentialFieldSpec,
        injected: str = "",
        *,
        allow_cache: bool = True,
        allow_env: bool = True,
    ) -> str:
        """解析單一欄位值，並視規格執行驗證。"""
        if spec.is_secret:
            raw = injected if not is_empty_or_null(injected) else ""
            if not raw and allow_cache:
                raw = self.cache.get_secret(spec.env_key, "")
            if not raw and allow_env:
                env_val = os.getenv(spec.env_key)
                if not is_empty_or_null(env_val):
                    raw = str(env_val)
        else:
            raw = clean_param(injected)
            if not raw and allow_cache:
                raw = clean_param(self.cache.get(spec.env_key, ""))
            if not raw and allow_env:
                raw = clean_param(os.getenv(spec.env_key))

        if not raw and spec.prompt_text and spec.required:
            prompted = self.prompter.prompt(
                spec.prompt_text,
                is_password=spec.is_secret,
                hint=self.hint or spec.env_key,
            )
            raw = prompted if spec.is_secret else clean_param(prompted)

        if not raw and spec.default:
            raw = spec.default

        if not raw:
            if spec.required:
                raise ValueError(t("mcp_shared.missing_required_param", env_key=spec.env_key))
            return ""

        if spec.validator is not None:
            return spec.validator(raw)
        return raw
