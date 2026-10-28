"""網路研究韌性、步進式歷史保存、串流退避重試與 404 自動退避檢索單元測試（不連網）。"""

import asyncio
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

from rich.console import Console

from pocket_agent.core import sessions
from pocket_agent.core.agent import Agent
from pocket_agent.core.compaction import ensure_paired_tool_calls
from pocket_agent.engines.base import BaseEngine, EventText, EventToolCallDone, EventToolCallStart
from pocket_agent.frontend.cli.commands import CommandResult
from pocket_agent.frontend.cli.main import send_turn
from pocket_agent.frontend.cli.render import CliRenderer
from pocket_agent.frontend.cli.state import CliState
from pocket_agent.system.constants import DEFAULT_STREAM_READ_TIMEOUT_SEC
from pocket_agent.tools.web import _build_404_fallback_query, _read_url, is_soft_web_error


class _SequenceEngine(BaseEngine):
    """依序執行預設回合行為的測試用 Engine。"""

    def __init__(self, steps):
        super().__init__(client=None, model="mock-model")
        self.steps = list(steps)
        self.calls = 0

    async def stream_chat(
        self,
        input_items,
        instructions,
        tools,
        reasoning_param=None,
        cancel_check=None,
        build_tools_callback=None,
    ):
        idx = min(self.calls, len(self.steps) - 1)
        self.calls += 1
        step = self.steps[idx]
        if isinstance(step, Exception):
            raise step
        if callable(step):
            events = step(input_items, instructions, tools)
        else:
            events = step
        for ev in events:
            yield ev


def test_step_level_tool_persistence_and_checkpoint_on_stream_abort(tmp_path, monkeypatch):
    """AC-1.1 & AC-1.2 & AC-2.1：工具完成後即時寫入 history，後續串流斷線耗盡重試時保留成果並寫入檢查點。"""

    async def _test():
        monkeypatch.setattr("pocket_agent.system.paths.scratch_dir", lambda: tmp_path)

        steps = [
            # 第 1 輪：模型呼叫 search_web
            [
                EventToolCallStart("call_w1", "search_web"),
                EventToolCallDone("call_w1", "search_web", '{"query": "CDN benefits"}'),
            ],
            # 第 2 輪起：連續 4 次串流異常（初始 1 次 + 重試 3 次），觸發中斷
            RuntimeError("Connection error."),
            RuntimeError("Connection error."),
            RuntimeError("Connection error."),
            RuntimeError("Connection error."),
        ]
        engine = _SequenceEngine(steps)
        mcp = MagicMock()
        mcp.openai_tools.return_value = []
        agent = Agent(client=MagicMock(), mcp=mcp, model="mock-model")
        agent.stream_retry_base_sec = 0.0

        with (
            patch.object(Agent, "engine", new_callable=PropertyMock(return_value=engine)),
            patch("pocket_agent.core.agent.call_web_tool", return_value="搜尋結果：CDN 可降低延遲與源站負載"),
        ):
            _, reply, _ = await agent.chat("請研究 CDN 的好處")

        # 1. 驗證回覆包含中斷原因與研究進度保護提示
        assert "連續發生太多次執行錯誤" in reply
        assert "研究進度已自動保護" in reply
        assert "research_checkpoint.md" in reply

        # 2. 驗證檢查點檔案已寫入 scratch_dir
        ckpt_file = tmp_path / "research_checkpoint.md"
        assert ckpt_file.exists()
        ckpt_text = ckpt_file.read_text(encoding="utf-8")
        assert "search_web" in ckpt_text
        assert "CDN benefits" in ckpt_text

        # 3. 驗證 agent.history 已完整保留第 1 輪的工具呼叫與輸出，且無 [System Error] 污染
        paired = ensure_paired_tool_calls(agent.history)
        assert len(paired) == len(agent.history)
        fc_items = [m for m in agent.history if m.get("type") == "function_call"]
        out_items = [m for m in agent.history if m.get("type") == "function_call_output"]
        assert len(fc_items) == 1
        assert len(out_items) == 1
        assert "CDN 可降低延遲" in out_items[0]["output"]
        assert not any("[System Error]" in str(m.get("content", "")) for m in agent.history)

    asyncio.run(_test())


def test_stream_retry_backoff_recovers_without_polluting_history():
    """AC-2.1：串流瞬斷在重試上限內恢復時，執行指數退避且不污染對話歷史。"""

    async def _test():
        steps = [
            RuntimeError("Connection error."),
            RuntimeError("Connection error."),
            [EventText("這是重試成功後的最終研究報告。")],
        ]
        engine = _SequenceEngine(steps)
        mcp = MagicMock()
        mcp.openai_tools.return_value = []
        agent = Agent(client=MagicMock(), mcp=mcp, model="mock-model")
        agent.stream_retry_base_sec = 0.5

        sleep_calls: list[float] = []

        async def _fake_sleep(sec: float):
            sleep_calls.append(sec)

        with (
            patch.object(Agent, "engine", new_callable=PropertyMock(return_value=engine)),
            patch("pocket_agent.core.agent.asyncio.sleep", side_effect=_fake_sleep),
        ):
            _, reply, _ = await agent.chat("測試瞬斷恢復")

        assert reply == "這是重試成功後的最終研究報告。"
        assert len(sleep_calls) == 2
        assert sleep_calls[1] > sleep_calls[0]
        assert len(agent.history) == 2
        assert agent.history[0]["role"] == "user"
        assert agent.history[1]["role"] == "assistant"

    asyncio.run(_test())


def test_openai_provider_stream_read_timeout():
    """AC-2.2：驗證 OpenAIProvider 與 OpenAIApiProvider 設定明確的串流讀取逾時 (120s)。"""
    from pocket_agent.system.providers.openai_provider import OpenAIApiProvider, OpenAIProvider

    with (
        patch("pocket_agent.system.auth.get_access_token", return_value="fake.jwt.token"),
        patch("pocket_agent.system.auth._extract_account_id", return_value="acc_123"),
        patch("pocket_agent.system.providers.openai_provider.AsyncOpenAI") as mock_cls,
    ):
        OpenAIProvider().get_client()
        timeout_obj = mock_cls.call_args.kwargs["timeout"]
        assert timeout_obj.read == DEFAULT_STREAM_READ_TIMEOUT_SEC
        assert timeout_obj.connect == 15.0

    with (
        patch("pocket_agent.system.config.config.get", return_value="sk-test"),
        patch("pocket_agent.system.providers.openai_provider.AsyncOpenAI") as mock_api_cls,
    ):
        OpenAIApiProvider().get_client()
        timeout_api = mock_api_cls.call_args.kwargs["timeout"]
        assert timeout_api.read == DEFAULT_STREAM_READ_TIMEOUT_SEC
        assert timeout_api.connect == 15.0


def test_web_404_auto_fallback_search():
    """AC-3.1：驗證 _read_url 遭遇 HTTP 404 時自動萃取關鍵字進行退避檢索並附加相近網址。"""
    url = "https://developers.cloudflare.com/cache/cache-security/avoid-cache-poisoning.html"
    q = _build_404_fallback_query(url)
    assert q == "site:developers.cloudflare.com cache security avoid poisoning"

    with (
        patch("pocket_agent.tools.web._resolve_public_ip", return_value="104.16.132.229"),
        patch("pocket_agent.tools.web._fetch_pinned", return_value=(404, {}, b"Not Found", False, False)),
        patch(
            "pocket_agent.tools.web._search_ddg_snippets",
            return_value=(
                ["1. Cache Poisoning Prevention\n   URL: https://developers.cloudflare.com/cache/security/"],
                None,
            ),
        ) as mock_search,
    ):
        res = _read_url(url, headers={})

    mock_search.assert_called_once()
    assert res.startswith("[ERROR] 網路操作失敗: HTTP 404")
    assert "[404 自動退避檢索]" in res
    assert "https://developers.cloudflare.com/cache/security/" in res
    assert is_soft_web_error(res) is True


def test_soft_web_404_does_not_trigger_consecutive_all_errors_abort():
    """AC-3.2：連續兩輪遭遇網頁 HTTP 404 軟錯誤時，不應觸發 tool_consecutive_all_errors 拔除工具。"""

    async def _test():
        steps = [
            [
                EventToolCallStart("c1", "read_url_content"),
                EventToolCallDone("c1", "read_url_content", '{"url": "https://example.com/old-1"}'),
            ],
            [
                EventToolCallStart("c2", "read_url_content"),
                EventToolCallDone("c2", "read_url_content", '{"url": "https://example.com/old-2"}'),
            ],
            lambda _items, _inst, tools: [
                EventText(f"第 3 輪仍保有 {len(tools)} 個可用工具並完成回答。"),
            ],
        ]
        engine = _SequenceEngine(steps)
        mcp = MagicMock()
        mcp.openai_tools.return_value = []
        agent = Agent(client=MagicMock(), mcp=mcp, model="mock-model")

        with (
            patch.object(Agent, "engine", new_callable=PropertyMock(return_value=engine)),
            patch(
                "pocket_agent.core.agent.call_web_tool",
                return_value="[ERROR] 網路操作失敗: HTTP 404\n\n[404 自動退避檢索] ...",
            ),
        ):
            _, reply, _ = await agent.chat("測試連續 404 不拔除工具")

        assert "仍保有" in reply
        assert "0 個可用工具" not in reply

    asyncio.run(_test())


def test_incremental_scratchpad_reminder_and_instructions(tmp_path, monkeypatch):
    """AC-4.1：驗證系統提示詞防盲猜規範，以及第 3 次網路檢索時自動附加階段性筆記提醒。"""

    async def _test():
        monkeypatch.setattr("pocket_agent.system.paths.scratch_dir", lambda: tmp_path)

        steps = [
            [
                EventToolCallStart("w1", "search_web"),
                EventToolCallDone("w1", "search_web", '{"query": "q1"}'),
                EventToolCallStart("w2", "search_web"),
                EventToolCallDone("w2", "search_web", '{"query": "q2"}'),
                EventToolCallStart("w3", "search_web"),
                EventToolCallDone("w3", "search_web", '{"query": "q3"}'),
            ],
            [EventText("研究完成")],
        ]
        engine = _SequenceEngine(steps)
        mcp = MagicMock()
        mcp.openai_tools.return_value = []
        agent = Agent(client=MagicMock(), mcp=mcp, model="mock-model")

        inst = agent._instructions("研究 CDN")
        assert "切勿憑記憶猜測深層文件 URL" in inst

        with (
            patch.object(Agent, "engine", new_callable=PropertyMock(return_value=engine)),
            patch("pocket_agent.core.agent.call_web_tool", return_value="搜尋結果片段"),
        ):
            await agent.chat("研究 CDN")

        outputs = [m["output"] for m in agent.history if m.get("type") == "function_call_output"]
        assert len(outputs) == 3
        assert "[系統提醒：階段性筆記]" in outputs[2]
        assert "notes.md" in outputs[2]

    asyncio.run(_test())


def test_send_turn_autosaves_session_each_turn(tmp_path, monkeypatch):
    """AC-1.1：驗證 CLI send_turn 在每輪結束時立即自動儲存 session。"""
    monkeypatch.setattr(sessions, "SESSIONS_DIR", tmp_path)
    state = CliState("openai", "model")
    renderer = CliRenderer(Console(file=StringIO(), force_terminal=False))

    async def _fake_chat(prompt, attachments=None):
        agent.history.append({"role": "user", "content": prompt})
        agent.history.append({"role": "assistant", "content": "回覆內容"})
        return "", "回覆內容", ""

    agent = SimpleNamespace(
        history=[],
        is_sensitive_session=False,
        total_tokens=100,
        chat=_fake_chat,
        request_stop=MagicMock(),
    )

    asyncio.run(send_turn(agent, state, renderer, "測試問題", CommandResult("chat", "測試問題")))

    saved = sessions.load_session(state.session_id)
    assert saved is not None
    assert len(saved["history"]) == 2
    assert saved["history"][0]["content"] == "測試問題"
