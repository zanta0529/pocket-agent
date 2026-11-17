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
    from pocket_agent.system import config as config_mod

    # 隔離真實使用者的 ~/.pocket-agent/config.json
    monkeypatch.setattr(config_mod.config, "get", lambda key, default=None: None)

    # 1. 顯式設定優先
    set_locale("en_US")
    assert get_locale() == "en_US"

    # 2. 清除顯式設定後，環境變數優先
    i18n._current_locale = None
    monkeypatch.setenv("POCKET_AGENT_LANG", "en_US")
    assert get_locale() == "en_US"

    # 3. 清除環境變數後，讀取 config["locale"]，若無則回退至預設 zh_TW
    monkeypatch.delenv("POCKET_AGENT_LANG", raising=False)
    assert get_locale() == "zh_TW"

    monkeypatch.setattr(
        config_mod.config,
        "get",
        lambda key, default=None: "en_US" if key == "locale" else default,
    )
    assert get_locale() == "en_US"


def test_get_available_locales():
    """驗證動態掃描 locales 目錄。"""
    from pocket_agent.system.i18n import get_available_locales

    locales = get_available_locales()
    assert "zh_TW" in locales
    assert "en_US" in locales


def test_wizard_init_setup_locale(tmp_path, monkeypatch):
    """驗證 setup 精靈初始化語系規則：無設定時預設 en_US，有設定時依 config.json。"""
    import json

    from pocket_agent.system import config as config_mod
    from pocket_agent.system.wizard import _init_setup_locale

    fake_cfg = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_cfg)

    # 1. config.json 不存在 -> 預設英文 en_US
    _init_setup_locale()
    assert get_locale() == "en_US"

    # 2. config.json 存在但無 locale 欄位 -> 預設英文 en_US
    fake_cfg.write_text(json.dumps({"active_provider": "openai"}), encoding="utf-8")
    _init_setup_locale()
    assert get_locale() == "en_US"

    # 3. config.json 存在且有 locale="zh_TW" -> 依設定採用 zh_TW
    fake_cfg.write_text(json.dumps({"locale": "zh_TW"}), encoding="utf-8")
    _init_setup_locale()
    assert get_locale() == "zh_TW"


def test_t_accepts_key_in_kwargs():
    """驗證格式化參數中包含 key=... 時不會與第一個位置參數衝突。"""
    set_locale("zh_TW")
    assert t("system_cmd.config_edit_normal", key="locale") == "編輯 locale: "
    set_locale("en_US")
    assert t("system_cmd.config_edit_normal", key="locale") == "Edit locale: "


def _flatten_locale_keys(data: dict, prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    for k, v in data.items():
        full_key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            flat.update(_flatten_locale_keys(v, full_key))
        else:
            flat[full_key] = str(v)
    return flat


def _extract_format_fields(template: str) -> set[str]:
    from string import Formatter

    return {field_name for _, field_name, _, _ in Formatter().parse(template) if field_name is not None}


def test_locale_files_keys_and_placeholders_aligned():
    """驗證所有語系檔與基準語系 zh_TW.json 的鍵值集合及格式化佔位符完全對齊。"""
    import json
    from pathlib import Path

    locales_dir = Path(__file__).resolve().parent.parent / "src" / "pocket_agent" / "locales"
    base_file = locales_dir / "zh_TW.json"
    assert base_file.is_file()

    base_flat = _flatten_locale_keys(json.loads(base_file.read_text(encoding="utf-8")))
    locale_files = sorted(p for p in locales_dir.glob("*.json") if p.name != "zh_TW.json")
    assert locale_files, "Expected at least one secondary locale file"

    for loc_file in locale_files:
        loc_flat = _flatten_locale_keys(json.loads(loc_file.read_text(encoding="utf-8")))
        missing = sorted(set(base_flat) - set(loc_flat))
        extra = sorted(set(loc_flat) - set(base_flat))
        assert not missing, f"{loc_file.name} missing keys: {missing}"
        assert not extra, f"{loc_file.name} has extra keys: {extra}"

        for key, base_val in base_flat.items():
            base_fields = _extract_format_fields(base_val)
            loc_fields = _extract_format_fields(loc_flat[key])
            assert base_fields == loc_fields, (
                f"{loc_file.name} placeholder mismatch at '{key}': "
                f"expected {sorted(base_fields)}, got {sorted(loc_fields)}"
            )
