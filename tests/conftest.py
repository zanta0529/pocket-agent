"""全域 pytest 測試隔離設定：防止測試期間寫入真實使用者家目錄 (~/.pocket-agent)。"""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_pocket_user_dir(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """將測試期間的 user_dir() 導向獨立暫存目錄，不更動 Path.home() 或測試本身的 tmp_path。"""
    fake_user_dir = tmp_path_factory.mktemp("pocket_agent_user") / ".pocket-agent"
    fake_user_dir.mkdir(parents=True, exist_ok=True)

    def _fake_user_dir() -> Path:
        fake_user_dir.mkdir(parents=True, exist_ok=True)
        return fake_user_dir

    from pocket_agent.system import paths

    monkeypatch.setattr("pocket_agent.system.paths.user_dir", _fake_user_dir)
    monkeypatch.setattr("pocket_agent.system.housekeeping.user_dir", _fake_user_dir)
    monkeypatch.setattr("pocket_agent.core.sessions.SESSIONS_DIR", None)
    monkeypatch.setattr("pocket_agent.core.sessions.SESSION_SALT_PATH", None)
    monkeypatch.setattr("pocket_agent.core.sessions._CACHED_HMAC_KEY", None)
    monkeypatch.setattr("pocket_agent.core.sessions._CACHED_LEGACY_HMAC_KEY", None)
    paths.set_active_session_id(None)
    try:
        yield fake_user_dir
    finally:
        paths.set_active_session_id(None)


@pytest.fixture(autouse=True)
def _isolate_i18n_locale(monkeypatch: pytest.MonkeyPatch):
    """確保所有測試預設使用 zh_TW，不受本機真實 ~/.pocket-agent/config.json 影響。"""
    from pocket_agent.system import i18n

    i18n.clear_cache()
    i18n.set_locale(i18n.DEFAULT_LOCALE)
    monkeypatch.delenv("POCKET_AGENT_LANG", raising=False)
    yield
    i18n.clear_cache()
    i18n.set_locale(i18n.DEFAULT_LOCALE)
    monkeypatch.delenv("POCKET_AGENT_LANG", raising=False)


class _InMemoryKeyring:
    """純記憶體 Keyring 測試替身，防止測試讀寫或刪除作業系統真實憑證庫。"""

    priority = 1

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service_name: str, username: str) -> str | None:
        return self._store.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self._store[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        from keyring.errors import PasswordDeleteError

        if (service_name, username) not in self._store:
            raise PasswordDeleteError(service_name)
        self._store.pop((service_name, username), None)


@pytest.fixture(autouse=True)
def _isolate_system_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    """全域隔離系統 Keyring，避免測試覆寫或刪除使用者真實儲存的憑證。"""
    import keyring.backend
    import keyring.core

    class _Backend(_InMemoryKeyring, keyring.backend.KeyringBackend):
        pass

    monkeypatch.setattr(keyring.core, "_keyring_backend", _Backend())
