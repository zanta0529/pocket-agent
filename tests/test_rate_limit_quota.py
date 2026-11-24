"""測試被動回應標頭限流額度解析（rate_limits.py）與 /usage 整合。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from pocket_agent.engines.azure_engine import AzureEngine
from pocket_agent.engines.base import BaseEngine
from pocket_agent.engines.cerebras_engine import CerebrasEngine
from pocket_agent.engines.chat_engine import StandardChatCompletionsEngine
from pocket_agent.engines.groq_engine import GroqEngine
from pocket_agent.engines.mistral_engine import MistralEngine
from pocket_agent.engines.rate_limits import format_rate_limit_quota, parse_rate_limit_snapshot
from pocket_agent.engines.sambanova_engine import SambaNovaEngine
from pocket_agent.system.i18n import set_locale


class _AsyncChunkStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.closed = False

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for chunk in self._chunks:
            yield chunk

    async def close(self):
        self.closed = True


def test_parse_rate_limit_snapshot_for_cerebras_groq_mistral_and_invalid():
    now = 1_750_000_000.0

    cerebras_headers = {
        "x-ratelimit-limit-requests-day": "14400",
        "x-ratelimit-remaining-requests-day": "14388",
        "x-ratelimit-reset-requests-day": "3660",
        "x-ratelimit-limit-tokens-minute": "60000",
        "x-ratelimit-remaining-tokens-minute": "58200",
        "x-ratelimit-reset-tokens-minute": "45s",
    }
    snap_c = parse_rate_limit_snapshot(cerebras_headers, captured_at=now)
    assert snap_c is not None
    assert len(snap_c.buckets) == 2
    assert snap_c.buckets[0].metric == "requests"
    assert snap_c.buckets[0].window == "day"
    assert snap_c.buckets[0].limit == 14400
    assert snap_c.buckets[0].remaining == 14388
    assert snap_c.buckets[0].resets_at == now + 3660.0
    assert snap_c.buckets[1].metric == "tokens"
    assert snap_c.buckets[1].window == "minute"
    assert snap_c.buckets[1].resets_at == now + 45.0

    groq_headers = {
        "X-RateLimit-Limit-Requests": "14400",
        "X-RateLimit-Remaining-Requests": "14000",
        "X-RateLimit-Reset-Requests": "2m59s",
        "X-RateLimit-Limit-Tokens": "6000",
        "X-RateLimit-Remaining-Tokens": "5120",
        "X-RateLimit-Reset-Tokens": "7.66s",
    }
    snap_g = parse_rate_limit_snapshot(groq_headers, captured_at=now)
    assert snap_g is not None
    assert len(snap_g.buckets) == 2
    assert snap_g.buckets[0].resets_at == now + 179.0
    assert snap_g.buckets[1].resets_at == now + 7.66

    mistral_headers = {
        "x-ratelimit-limit-tokens-minute": "500000",
        "x-ratelimit-remaining-tokens-minute": "490000",
        "x-ratelimit-limit-tokens-5-minute": "1000000",
        "x-ratelimit-remaining-tokens-5-minute": "990000",
        "x-ratelimit-limit-tokens-month": "1000000000",
        "x-ratelimit-remaining-tokens-month": "999000000",
    }
    snap_m = parse_rate_limit_snapshot(mistral_headers, captured_at=now)
    assert snap_m is not None
    assert [b.window for b in snap_m.buckets] == ["month", "minute", "5-minute"]

    ts_headers = {
        "x-ratelimit-limit": "100",
        "x-ratelimit-remaining": "99",
        "x-ratelimit-reset": "1800000000",
    }
    snap_ts = parse_rate_limit_snapshot(ts_headers, captured_at=now)
    assert snap_ts is not None
    assert snap_ts.buckets[0].metric == "general"
    assert snap_ts.buckets[0].resets_at == 1800000000.0

    assert parse_rate_limit_snapshot({"content-type": "application/json"}) is None
    assert parse_rate_limit_snapshot(None) is None


def test_format_rate_limit_quota_i18n_and_expired_reset():
    now = 1_750_000_000.0
    snap = parse_rate_limit_snapshot(
        {
            "x-ratelimit-limit-requests-day": "14400",
            "x-ratelimit-remaining-requests-day": "14388",
            "x-ratelimit-reset-requests-day": "125",
            "x-ratelimit-limit-tokens-minute": "60000",
            "x-ratelimit-remaining-tokens-minute": "58200",
        },
        captured_at=now,
    )
    assert snap is not None

    set_locale("zh_TW")
    q_zh = format_rate_limit_quota(snap, now=now)
    assert q_zh.is_supported is True
    assert q_zh.limit is None
    assert q_zh.remaining is None
    lines_zh = q_zh.get_display_lines()
    assert any("14,388 / 上限 14,400（已用 12" in line for line in lines_zh)
    assert any("2 分 5 秒" in line for line in lines_zh)
    assert any("58,200 / 上限 60,000" in line for line in lines_zh)
    assert any("快照" in line for line in lines_zh)

    set_locale("en_US")
    q_en = format_rate_limit_quota(snap, now=now + 300.0)
    lines_en = q_en.get_display_lines()
    assert any("14,388 / 14,400 remaining (used 12" in line for line in lines_en)
    assert any("should have reset at" in line for line in lines_en)
    assert any("Snapshot from last API response headers" in line for line in lines_en)


def test_engines_pending_and_unsupported_quota_messages():
    async def _run():
        BaseEngine._rate_limit_cache.clear()
        try:
            for cls in (CerebrasEngine, GroqEngine, AzureEngine, MistralEngine, SambaNovaEngine):
                eng = cls(client=MagicMock(base_url="https://example.com/v1"), model="m1")
                assert eng.supports_header_quota is True
                set_locale("zh_TW")
                q_zh = await eng.get_quota()
                assert q_zh.is_supported is False
                assert "送出第一則訊息" in (q_zh.message or "")

                set_locale("en_US")
                q_en = await eng.get_quota()
                assert q_en.is_supported is False
                assert "first message" in (q_en.message or "")

            plain_eng = StandardChatCompletionsEngine(client=MagicMock(base_url="https://example.com"), model="gemini")
            assert plain_eng.supports_header_quota is False
            set_locale("en_US")
            q_plain = await plain_eng.get_quota()
            assert q_plain.is_supported is False
            assert "does not support API quota lookup" in (q_plain.message or "")
        finally:
            BaseEngine._rate_limit_cache.clear()

    asyncio.run(_run())


def test_stream_chat_with_raw_response_records_headers_and_isolates_models():
    async def _run():
        BaseEngine._rate_limit_cache.clear()
        try:
            stream_a = _AsyncChunkStream(
                [
                    SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(tool_calls=None, content="hi"),
                                finish_reason="stop",
                            )
                        ],
                        usage=None,
                    )
                ]
            )
            raw_resp_sync = SimpleNamespace(
                headers={
                    "x-ratelimit-limit-requests-day": "1000",
                    "x-ratelimit-remaining-requests-day": "999",
                },
                parse=lambda: stream_a,
            )
            client = MagicMock()
            client.base_url = "https://api.cerebras.ai/v1"
            client.chat.completions.with_raw_response.create = AsyncMock(return_value=raw_resp_sync)

            eng_a = CerebrasEngine(client=client, model="gpt-oss-120b")
            events = [
                ev
                async for ev in eng_a.stream_chat(
                    input_items=[{"role": "user", "content": "hi"}],
                    instructions="",
                    tools=[],
                    reasoning_param=None,
                )
            ]
            assert events
            client.chat.completions.create.assert_not_called()

            q_a = await eng_a.get_quota()
            assert q_a.is_supported is True
            assert any("999 / 上限 1,000" in line for line in q_a.get_display_lines())

            # 不同模型應維持獨立快取，不受前一個模型污染
            eng_b = CerebrasEngine(client=client, model="qwen-3.8-27b")
            q_b = await eng_b.get_quota()
            assert q_b.is_supported is False

            # 非同步 parse() 亦相容
            stream_b = _AsyncChunkStream(
                [
                    SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(tool_calls=None, content="ok"),
                                finish_reason="stop",
                            )
                        ],
                        usage=None,
                    )
                ]
            )
            raw_resp_async = SimpleNamespace(
                headers={
                    "x-ratelimit-limit-tokens": "6000",
                    "x-ratelimit-remaining-tokens": "5500",
                },
                parse=AsyncMock(return_value=stream_b),
            )
            client.chat.completions.with_raw_response.create = AsyncMock(return_value=raw_resp_async)
            _ = [
                ev
                async for ev in eng_b.stream_chat(
                    input_items=[{"role": "user", "content": "hi"}],
                    instructions="",
                    tools=[],
                    reasoning_param=None,
                )
            ]
            q_b_after = await eng_b.get_quota()
            assert q_b_after.is_supported is True
            assert any("5,500 / 上限 6,000" in line for line in q_b_after.get_display_lines())
        finally:
            BaseEngine._rate_limit_cache.clear()

    asyncio.run(_run())


def test_stream_chat_429_exception_updates_rate_limit_snapshot():
    async def _run():
        BaseEngine._rate_limit_cache.clear()
        try:
            err = RuntimeError("rate limited")
            err.status_code = 429
            err.response = SimpleNamespace(
                headers={
                    "x-ratelimit-limit-requests": "100",
                    "x-ratelimit-remaining-requests": "0",
                    "x-ratelimit-reset-requests": "30s",
                }
            )
            client = MagicMock()
            client.base_url = "https://api.groq.com/openai/v1"
            client.chat.completions.with_raw_response.create = AsyncMock(side_effect=err)

            eng = GroqEngine(client=client, model="openai/gpt-oss-120b")
            try:
                async for _ in eng.stream_chat(
                    input_items=[{"role": "user", "content": "hi"}],
                    instructions="",
                    tools=[],
                    reasoning_param=None,
                ):
                    pass
                assert False, "Expected RuntimeError"
            except RuntimeError:
                pass

            q = await eng.get_quota()
            assert q.is_supported is True
            assert any("0 / 上限 100" in line for line in q.get_display_lines())
        finally:
            BaseEngine._rate_limit_cache.clear()

    asyncio.run(_run())
