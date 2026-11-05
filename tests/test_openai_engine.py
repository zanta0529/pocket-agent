import time
from types import SimpleNamespace
from unittest.mock import Mock

from pocket_agent.engines.base import BaseEngine
from pocket_agent.engines.openai_engine import OpenAIResponsesEngine


def test_base_engine_format_reset_time_hint():
    # 1. 傳入未來 120 秒
    hint_sec = BaseEngine.format_reset_time_hint(resets_in_seconds=120)
    assert hint_sec is not None
    assert "重置時間" in hint_sec
    assert "2 分" in hint_sec

    # 2. 傳入未來 epoch 時間戳記
    future_ts = time.time() + 3700
    hint_ts = BaseEngine.format_reset_time_hint(resets_at=future_ts)
    assert hint_ts is not None
    assert "1 小時" in hint_ts

    # 3. 傳入無效值返回 None
    assert BaseEngine.format_reset_time_hint() is None


def test_openai_engine_parse_api_error_codex_headers():
    engine = OpenAIResponsesEngine(client=Mock(), model="gpt-4o")

    # 模擬附圖的情境：純文字錯誤訊息，但 HTTP Response Header 帶有 Retry-After
    fake_response = SimpleNamespace(headers={"retry-after": "90"})
    fake_exc = Exception("Error code: 429 - The usage limit has been reached")
    fake_exc.status_code = 429
    fake_exc.response = fake_response

    res = engine.parse_api_error(fake_exc)
    assert res is not None
    assert "API 用量已達上限" in res
    assert "重置時間" in res
    assert "1 分 30 秒" in res

    # 驗證 format_api_error 整體輸出
    formatted = engine.format_api_error(fake_exc)
    assert "HTTP 429 Rate Limit Exceeded" in formatted
    assert "伺服器回傳：Error code: 429 - The usage limit has been reached" in formatted
    assert "API 用量已達上限" in formatted
    assert "重置時間" in formatted


def test_openai_engine_parse_api_error_codex_timestamp_header():
    engine = OpenAIResponsesEngine(client=Mock(), model="gpt-4o")

    # 測試 x-codex-resets-at 標頭
    future_ts = time.time() + 600
    fake_response = SimpleNamespace(headers={"x-codex-resets-at": str(future_ts)})
    fake_exc = Exception("The usage limit has been reached")
    fake_exc.status_code = 429
    fake_exc.response = fake_response

    res = engine.parse_api_error(fake_exc)
    assert res is not None
    assert "API 用量已達上限" in res
    assert "重置時間" in res
    assert "9 分" in res or "10 分" in res


def test_openai_engine_parse_api_error_json_body():
    engine = OpenAIResponsesEngine(client=Mock(), model="gpt-4o")

    # 測試標準 OpenAI JSON body
    fake_exc = Exception("Error code: 429 - Rate limit reached")
    fake_exc.status_code = 429
    fake_exc.body = {
        "error": {
            "type": "usage_limit_reached",
            "plan_type": "Plus",
            "resets_in_seconds": 300,
        }
    }

    res = engine.parse_api_error(fake_exc)
    assert res is not None
    assert "方案：Plus" in res
    assert "5 分" in res
