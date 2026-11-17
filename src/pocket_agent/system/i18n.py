"""輕量化多語系（i18n）管理模組。

支援以階層式 JSON 語系檔進行翻譯查表、參數代入（kwargs）與安全後備機制（Fallback）。
"""

import json
import logging
import os
from pathlib import Path
from typing import Any

from pocket_agent.system.paths import bundle_dir

log = logging.getLogger("pocket-agent")

DEFAULT_LOCALE = "zh_TW"
SUPPORTED_LOCALES: tuple[str, ...] = ("zh_TW", "en_US")

_current_locale: str | None = None
_translations_cache: dict[str, dict[str, Any]] = {}


def _get_locales_dir() -> Path:
    """取得語系檔所在目錄。相容開發模式、可攜式與 PyInstaller 打包環境。"""
    # 優先嘗試本模組相鄰之 locales 目錄
    adjacent = Path(__file__).resolve().parent.parent / "locales"
    if adjacent.is_dir():
        return adjacent

    # 打包模式 fallback
    bundled = bundle_dir() / "src" / "pocket_agent" / "locales"
    if bundled.is_dir():
        return bundled

    return adjacent


def get_available_locales() -> list[str]:
    """動態掃描 locales 目錄下所有可用的語系檔（不含 .json 副檔名）。"""
    locales_dir = _get_locales_dir()
    if not locales_dir.is_dir():
        return list(SUPPORTED_LOCALES)

    found: list[str] = []
    for f in sorted(locales_dir.glob("*.json")):
        if f.is_file() and not f.name.startswith("."):
            found.append(f.stem)

    return found if found else list(SUPPORTED_LOCALES)


def load_locale(locale: str) -> dict[str, Any]:
    """載入指定語系之 JSON 翻譯表，具備記憶體快取。"""
    if locale in _translations_cache:
        return _translations_cache[locale]

    locales_dir = _get_locales_dir()
    file_path = locales_dir / f"{locale}.json"

    if not file_path.is_file():
        log.debug("Locale file not found: %s", file_path)
        _translations_cache[locale] = {}
        return {}

    try:
        with file_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                _translations_cache[locale] = data
                return data
            _translations_cache[locale] = {}
            return {}
    except Exception as e:
        log.warning("Failed to load locale %s: %s", locale, e)
        _translations_cache[locale] = {}
        return {}


def clear_cache() -> None:
    """清除翻譯快取（主要供測試使用）。"""
    global _translations_cache
    _translations_cache.clear()


def set_locale(locale: str) -> None:
    """顯式設定目前使用之語系。"""
    global _current_locale
    _current_locale = locale


def get_locale() -> str:
    """取得目前作用中的語系代碼。

    優先序：顯式設定 > 環境變數 POCKET_AGENT_LANG > 設定檔 config["locale"] > 預設 DEFAULT_LOCALE。
    """
    global _current_locale
    if _current_locale:
        return _current_locale

    env_locale = os.environ.get("POCKET_AGENT_LANG")
    if env_locale:
        return env_locale

    try:
        from pocket_agent.system.config import config

        cfg_locale = config.get("locale")
        if isinstance(cfg_locale, str) and cfg_locale:
            return cfg_locale
    except Exception:
        pass

    return DEFAULT_LOCALE


def _resolve_nested_key(data: dict[str, Any], key: str) -> Any:
    """解析以點分隔的語意化鍵值（例如 cli.stopping）。"""
    parts = key.split(".")
    curr: Any = data
    for part in parts:
        if isinstance(curr, dict) and part in curr:
            curr = curr[part]
        else:
            return None
    return curr


def t(key: str, /, default: str | None = None, **kwargs: Any) -> str:
    """多語系字串翻譯查找介面。

    Args:
        key: 語意化鍵值（例如 "cli.stopping"）。
        default: 查無此鍵值時的後備字串；若為 None 則以 key 作為後備。
        **kwargs: 傳入用於字串格式化之變數。

    Returns:
        翻譯並完成變數格式化後的字串。
    """
    locale = get_locale()
    trans = load_locale(locale)
    val = _resolve_nested_key(trans, key)

    # 若指定語系查無內容且非預設語系，嘗試自預設語系（zh_TW）後備
    if val is None and locale != DEFAULT_LOCALE:
        fallback_trans = load_locale(DEFAULT_LOCALE)
        val = _resolve_nested_key(fallback_trans, key)

    if val is None or not isinstance(val, str):
        val = default if default is not None else key

    if kwargs:
        try:
            return val.format(**kwargs)
        except Exception:
            return val

    return val
