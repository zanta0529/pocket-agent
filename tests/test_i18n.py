"""Tests for pocket_agent.system.i18n module."""

import pytest

from pocket_agent.system import i18n
from pocket_agent.system.i18n import clear_cache, get_locale, set_locale, t


@pytest.fixture(autouse=True)
def reset_i18n_state(monkeypatch):
    """確保每個測試案例都有乾淨的 i18n 狀態。"""
    clear_cache()
    set_locale("zh_TW")
    monkeypatch.delenv("POCKET_AGENT_LANG", raising=False)
    yield
    clear_cache()
    set_locale("zh_TW")
    monkeypatch.delenv("POCKET_AGENT_LANG", raising=False)


def test_t_default_zh_tw():
    """驗證預設繁體中文查表。"""
    set_locale("zh_TW")
    msg = t("cli.stopping")
    assert "停止要求已送出" in msg


def test_t_en_us():
    """驗證切換為英文查表。"""
    set_locale("en_US")
    msg = t("cli.stopping")
    assert "Stop request sent" in msg


def test_t_fallback_to_zh_tw():
    """驗證當指定語系缺少該 key 時，自動回退到 zh_TW。"""
    set_locale("en_US")
    # 假設故意查一個只有在未翻譯時會 fallback 的情況
    # 先注入快取模擬缺少某 key
    i18n._translations_cache["en_US"] = {"cli": {}}
    i18n._translations_cache["zh_TW"] = {"cli": {"only_in_zh": "僅在中文存在"}}

    res = t("cli.only_in_zh")
    assert res == "僅在中文存在"


def test_t_missing_key_fallback_to_default_or_key():
    """驗證無任何語系包含該 key 時，回退至 default 參數或 key 本身。"""
    res_default = t("non.existent.key", default="後備文字")
    assert res_default == "後備文字"

    res_key = t("non.existent.key")
    assert res_key == "non.existent.key"


def test_t_kwargs_formatting():
    """驗證帶有格式化參數的字串。"""
    set_locale("zh_TW")
    msg = t("wizard.completed", command="python main.py")
    assert "python main.py" in msg

    set_locale("en_US")
    msg_en = t("wizard.completed", command="python main.py")
    assert "Setup complete!" in msg_en
    assert "python main.py" in msg_en


def test_t_kwargs_formatting_error_handled():
    """驗證格式化參數缺失或型別錯誤時優雅降級，不拋出例外。"""
    set_locale("zh_TW")
    # wizard.completed 包含 {command}，但不傳 command 參數
    msg = t("wizard.completed")
    # 應回傳原始包含 {command} 的模板字串而不崩潰
    assert "{command}" in msg


def test_get_locale_precedence(monkeypatch):
    """驗證語系判斷優先權。"""
    # 1. 顯式設定優先
    set_locale("en_US")
    assert get_locale() == "en_US"

    # 2. 清除顯式設定後，環境變數優先
    i18n._current_locale = None
    monkeypatch.setenv("POCKET_AGENT_LANG", "en_US")
    assert get_locale() == "en_US"

    # 3. 清除環境變數後，回退至預設 zh_TW
    monkeypatch.delenv("POCKET_AGENT_LANG", raising=False)
    assert get_locale() == "zh_TW"
