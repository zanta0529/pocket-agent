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

    monkeypatch.setattr("pocket_agent.system.paths.user_dir", _fake_user_dir)
    monkeypatch.setattr("pocket_agent.system.housekeeping.user_dir", _fake_user_dir)
    monkeypatch.setattr("pocket_agent.core.sessions.SESSIONS_DIR", None)
    monkeypatch.setattr("pocket_agent.core.sessions.SESSION_SALT_PATH", None)
    monkeypatch.setattr("pocket_agent.core.sessions._CACHED_HMAC_KEY", None)
    monkeypatch.setattr("pocket_agent.core.sessions._CACHED_LEGACY_HMAC_KEY", None)
    return fake_user_dir


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
