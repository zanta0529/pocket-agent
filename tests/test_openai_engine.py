import asyncio
import base64
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from pocket_agent.engines.base import BaseEngine
from pocket_agent.engines.factory import EngineFactory
from pocket_agent.engines.openai_engine import (
    CodexOAuthQuotaStrategy,
    OpenAIApiQuotaStrategy,
    OpenAIResponsesEngine,
)
from pocket_agent.system.constants import PROVIDER_OPENAI, PROVIDER_OPENAI_API


def _make_fake_jwt(plan_type: str = "business", account_id: str = "acc_123") -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload_dict = {
        "https://api.openai.com/auth": {
            "chatgpt_plan_type": plan_type,
            "chatgpt_account_id": account_id,
        }
    }
    payload = base64.urlsafe_b64encode(json.dumps(payload_dict).encode()).decode().rstrip("=")
    return f"{header}.{payload}.sig"


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


def test_openai_engine_parse_unwrapped_sdk_v3_body_business_plan():
    """測試 AC-1：OpenAI SDK v3 已預先解包 exc.body 且 str(exc) 不含 JSON 時，仍能提取 Business 方案與重置時間。"""
    engine = OpenAIResponsesEngine(client=Mock(), model="gpt-5-codex")
    future_ts = time.time() + 3665

    fake_exc = Exception("Error code: 429 - The usage limit has been reached")
    fake_exc.status_code = 429
    # OpenAI SDK v3 _make_status_error 會將 body.get("error", body) 直接放入 exc.body
    fake_exc.body = {
        "type": "usage_limit_reached",
        "message": "The usage limit has been reached",
        "plan_type": "business",
        "resets_at": future_ts,
        "resets_in_seconds": 3665,
    }

    formatted = engine.format_api_error(fake_exc)
    assert "伺服器回傳：The usage limit has been reached" in formatted
    assert "💳 **API 用量已達上限** (方案：Business)" in formatted
    assert "⏳ **重置時間**：" in formatted
    assert "1 小時 1 分" in formatted


def test_openai_engine_codex_sliding_window_headers_and_jwt_plan_fallback():
    """測試 AC-2：從 OAuth JWT 提取方案類型，並解析 x-codex-primary/secondary 視窗標頭與複合時間字串。"""
    jwt_token = _make_fake_jwt(plan_type="business")
    fake_client = SimpleNamespace(
        api_key=jwt_token,
        base_url="https://chatgpt.com/backend-api/codex",
    )
    engine = OpenAIResponsesEngine(client=fake_client, model="gpt-5-codex", provider="openai")

    future_ts = time.time() + 7200
    fake_response = SimpleNamespace(
        headers={
            "x-codex-primary-used-percent": "45",
            "x-codex-primary-reset-at": str(future_ts - 3600),
            "x-codex-secondary-used-percent": "100",
            "x-codex-secondary-reset-at": str(future_ts),
        }
    )
    fake_exc = Exception("Error code: 429 - The usage limit has been reached")
    fake_exc.status_code = 429
    fake_exc.response = fake_response

    formatted = engine.format_api_error(fake_exc)
    assert "💳 **API 用量已達上限** (方案：Business)" in formatted
    assert "1 小時 59 分" in formatted or "2 小時 0 分" in formatted

    # 測試標準 OpenAI API 複合時間字串標頭 (例如 "1h30m0s")
    assert BaseEngine.parse_duration_seconds("1h30m0s") == 5400.0
    assert BaseEngine.parse_duration_seconds("6m0s") == 360.0
    assert BaseEngine.parse_duration_seconds("500ms") == 0.5


def test_codex_oauth_wham_usage_async_enrichment_and_get_quota():
    """測試 AC-3：當 429 例外本身缺少重置時間時，非同步透過 /wham/usage 補齊，並支援 get_quota() 額度查詢。"""
    CodexOAuthQuotaStrategy._usage_cache = None
    jwt_token = _make_fake_jwt(plan_type="business")
    fake_client = SimpleNamespace(
        api_key=jwt_token,
        base_url="https://chatgpt.com/backend-api/codex",
    )
    engine = OpenAIResponsesEngine(client=fake_client, model="gpt-5-codex", provider="openai")

    future_ts = time.time() + 1800
    wham_payload = {
        "plan_type": "business",
        "rate_limit": {
            "allowed": False,
            "limit_reached": True,
            "primary_window": {
                "used_percent": 100,
                "limit_window_seconds": 18000,
                "reset_after_seconds": 1800,
                "reset_at": future_ts,
            },
            "secondary_window": {
                "used_percent": 32,
                "limit_window_seconds": 604800,
                "reset_after_seconds": 300000,
                "reset_at": future_ts + 300000,
            },
        },
    }

    fake_resp = Mock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = wham_payload

    mock_http_instance = AsyncMock()
    mock_http_instance.get.return_value = fake_resp
    mock_http_ctx = AsyncMock()
    mock_http_ctx.__aenter__.return_value = mock_http_instance

    fake_exc = Exception("Error code: 429 - The usage limit has been reached")
    fake_exc.status_code = 429

    with patch("httpx.AsyncClient", return_value=mock_http_ctx):
        formatted = asyncio.run(engine.format_api_error_async(fake_exc))
        assert "💳 **API 用量已達上限** (方案：Business)" in formatted
        assert "⏳ **重置時間**：" in formatted
        assert "30 分" in formatted

        quota = asyncio.run(engine.get_quota())
        assert quota.is_supported is True
        assert quota.plan_type == "Business"
        lines = "\n".join(quota.get_display_lines())
        assert "目前訂閱方案: Business" in lines
        assert "Codex 5 小時視窗: 已用 100%" in lines
        assert "Codex 7 天週額度視窗: 已用 32%" in lines


def test_openai_strategy_selection_and_factory_mapping():
    """測試 AC-4：Strategy 依 provider/client 正確分派，且 EngineFactory 支援 openai 與 openai-api。"""
    codex_client = SimpleNamespace(
        api_key=_make_fake_jwt("pro"),
        base_url="https://chatgpt.com/backend-api/codex",
    )
    api_client = SimpleNamespace(
        api_key="sk-proj-123456",
        base_url="https://api.openai.com/v1",
    )

    eng_codex = EngineFactory.create(PROVIDER_OPENAI, codex_client, "gpt-5-codex")
    eng_api = EngineFactory.create(PROVIDER_OPENAI_API, api_client, "gpt-4o")

    assert isinstance(eng_codex, OpenAIResponsesEngine)
    assert isinstance(eng_codex.quota_strategy, CodexOAuthQuotaStrategy)
    assert isinstance(eng_api, OpenAIResponsesEngine)
    assert isinstance(eng_api.quota_strategy, OpenAIApiQuotaStrategy)
