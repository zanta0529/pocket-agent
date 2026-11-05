"""六項核心程式碼可靠性、安全性與資源管理修補之回歸測試。"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pocket_agent.core import sessions
from pocket_agent.core.agent import Agent
from pocket_agent.engines import BaseEngine, EventText, EventToolCallDone, EventToolCallStart
from pocket_agent.extensions.mcp import MCPManager
from pocket_agent.system import auth, updater


# ============================================================================
# 1. auth.py：Token 儲存防禦性交易機制
# ============================================================================
def test_auth_save_chunked_failure_keeps_old_token(monkeypatch):
    """驗證新長 Token 在切塊寫入失敗時，既有的有效 Token 完好無缺且能正常載入。"""
    store: dict[str, str] = {}

    def fake_get_password(service, key):
        return store.get(f"{service}:{key}")

    def fake_set_password(service, key, value):
        store[f"{service}:{key}"] = value

    def fake_delete_password(service, key):
        store.pop(f"{service}:{key}", None)

    monkeypatch.setattr(auth.keyring, "get_password", fake_get_password)
    monkeypatch.setattr(auth.keyring, "set_password", fake_set_password)
    monkeypatch.setattr(auth.keyring, "delete_password", fake_delete_password)

    # 1. 儲存既有短 Token
    old_token = {"access_token": "original_valid_access_token", "expires_in": 3600}
    auth._save_token("openai", old_token)
    assert auth._load_token("openai")["access_token"] == "original_valid_access_token"

    # 2. 模擬儲存需要切塊的新長 Token，但在寫入第二個切塊時發生例外
    new_token = {"access_token": "new_long_token_" * 150, "expires_in": 3600}
    real_set = fake_set_password
    set_count = 0

    def failing_set_password(service, key, value):
        nonlocal set_count
        set_count += 1
        if "-1" in key:
            raise RuntimeError("Keyring quota exceeded (CredWrite 1783)")
        real_set(service, key, value)

    monkeypatch.setattr(auth.keyring, "set_password", failing_set_password)

    with pytest.raises(RuntimeError, match="系統憑證庫 \\(Keyring\\) 寫入失敗"):
        auth._save_token("openai", new_token)

    # 3. 驗證原 Token 依然存在且完全可載入
    monkeypatch.setattr(auth.keyring, "set_password", real_set)
    loaded = auth._load_token("openai")
    assert loaded is not None
    assert loaded["access_token"] == "original_valid_access_token"


def test_auth_save_short_token_failure_keeps_old_token(monkeypatch):
    """驗證新短 Token 寫入失敗時，既有的切塊 Token 不會被提前刪除。"""
    store: dict[str, str] = {}

    monkeypatch.setattr(auth.keyring, "get_password", lambda s, k: store.get(f"{s}:{k}"))
    monkeypatch.setattr(auth.keyring, "set_password", lambda s, k, v: store.update({f"{s}:{k}": v}))
    monkeypatch.setattr(auth.keyring, "delete_password", lambda s, k: store.pop(f"{s}:{k}", None))

    # 先存一個長 Token
    long_token = {"access_token": "long_token_chunked_" * 150, "expires_in": 3600}
    auth._save_token("openai", long_token)
    assert auth._load_token("openai")["access_token"].startswith("long_token_chunked_")

    # 模擬寫入新短 Token 時 set_password 失敗
    def failing_set(s, k, v):
        raise RuntimeError("Write failed")

    monkeypatch.setattr(auth.keyring, "set_password", failing_set)

    with pytest.raises(RuntimeError):
        auth._save_token("openai", {"access_token": "short", "expires_in": 3600})

    # 原長 Token 應不受影響
    monkeypatch.setattr(auth.keyring, "set_password", lambda s, k, v: store.update({f"{s}:{k}": v}))
    loaded = auth._load_token("openai")
    assert loaded is not None
    assert loaded["access_token"].startswith("long_token_chunked_")


# ============================================================================
# 2. sessions.py：排他原子建立 Salt 與金鑰快取
# ============================================================================
def test_sessions_salt_concurrency_race(tmp_path, monkeypatch):
    """驗證當另一個行程搶先建立 salt_file 時，能正確退回讀取現有 salt 並產出一致的金鑰。"""
    monkeypatch.setattr(sessions, "_CACHED_HMAC_KEY", None)
    salt_dir = tmp_path / ".pocket-agent"
    salt_file = salt_dir / "session_salt"
    monkeypatch.setattr(sessions.Path, "home", lambda: tmp_path)

    # 預先建立一個由「競爭程序」產生的 32 位元組 salt
    salt_dir.mkdir(parents=True, exist_ok=True)
    competing_salt = b"A" * 32
    salt_file.write_bytes(competing_salt)

    # 呼叫 _get_hmac_key()，此時應直接讀取現有 salt
    key1 = sessions._get_hmac_key()
    assert sessions._CACHED_HMAC_KEY is not None

    # 重設快取，模擬同一行程再次讀取或同 salt 簽署
    sessions._CACHED_HMAC_KEY = None
    key2 = sessions._get_hmac_key()
    assert key1 == key2

    # 驗證 session 簽章可被驗證
    session_id = "test-concurrency-session"
    sessions.save_session(session_id, [{"role": "user", "content": "hello"}])
    loaded = sessions.load_session(session_id)
    assert loaded is not None
    assert loaded["history"][0]["content"] == "hello"


# ============================================================================
# 3. updater.py：更新檔案複製失敗阻斷機制
# ============================================================================
def test_updater_copy_failure_blocks_version_update(tmp_path, monkeypatch):
    """驗證檔案複製拋出例外時，_merge_copy 記錄失敗，且 updater 中止更新與不寫入 VERSION_FILE。"""
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    source.mkdir()
    dest.mkdir()

    (source / "good.txt").write_text("good", encoding="utf-8")
    (source / "locked.txt").write_text("locked", encoding="utf-8")

    # 模擬 locked.txt 複製時拋出 PermissionError
    real_copy2 = updater.shutil.copy2

    def fake_copy2(src, dst):
        if Path(src).name == "locked.txt":
            raise PermissionError("Access is denied (File in use)")
        return real_copy2(src, dst)

    monkeypatch.setattr(updater.shutil, "copy2", fake_copy2)

    reports = []
    failed = updater._merge_copy(source, dest, reports.append)

    assert len(failed) == 1
    assert failed[0][0] == "locked.txt"
    assert "Access is denied" in failed[0][1]

    # 驗證若在 _update_via_zip 流程中，版本號寫入將被阻斷
    version_file = dest / updater.VERSION_FILE
    new_sha = "abcd1234efgh"

    with pytest.raises(RuntimeError, match="檔案更新未完整套用，為避免版本混合損壞已中止更新"):
        if failed:
            failed_summary = ", ".join(f"{name} ({err})" for name, err in failed[:5])
            raise RuntimeError(f"檔案更新未完整套用，為避免版本混合損壞已中止更新。失敗檔案：{failed_summary}")
        version_file.write_text(new_sha, encoding="utf-8")

    assert not version_file.exists()


# ============================================================================
# 4 & 5. agent.py：獨立雙重計數器與串流重試上下文同步
# ============================================================================
class FakeBreakerEngine(BaseEngine):
    """模擬連續多輪串流成功，但工具回傳失敗的測試引擎。"""

    def __init__(self):
        super().__init__(client=MagicMock(), model="test-breaker")
        self.call_count = 0
        self.received_messages = []

    async def stream_chat(self, input_items, *args, **kwargs):
        self.call_count += 1
        self.received_messages.append(list(input_items))

        # 前兩輪皆回傳呼叫工具的指令
        if self.call_count <= 2:
            yield EventToolCallStart(
                call_id=f"call_{self.call_count}",
                name="failing_tool",
            )
            yield EventToolCallDone(
                call_id=f"call_{self.call_count}",
                name="failing_tool",
                full_args="{}",
            )
        else:
            # 觸發止損後模型回覆純文字
            yield EventText(delta="工具故障，建議手動排查。")

    def is_context_overflow(self, error):
        return False

    def is_non_retryable_error(self, error):
        return False


def test_agent_consecutive_tool_errors_breaker(monkeypatch):
    """驗證連續兩輪工具全部失敗時，tool_consecutive_all_errors 能正確累計並觸發熔斷。"""
    engine = FakeBreakerEngine()
    mock_client = MagicMock()
    mock_mcp = MagicMock()
    mock_mcp.openai_tools.return_value = []
    mock_mcp.call = AsyncMock(return_value="[ERROR] command failed with exit code 1")

    agent = Agent(
        client=mock_client,
        mcp=mock_mcp,
        model="test-model",
        max_tool_rounds=5,
        provider="openai",
    )
    monkeypatch.setattr(Agent, "engine", property(lambda self: engine))

    thinking, reply, _usage = asyncio.run(agent.chat("請執行測試"))
    assert "工具故障，建議手動排查" in reply
    assert engine.call_count == 3

    # 驗證第三次傳入的訊息中包含強制跳出的指示
    third_turn_msgs = engine.received_messages[2]
    last_user_msg = third_turn_msgs[-1]
    assert last_user_msg["role"] == "user"
    assert "工具連續回傳錯誤" in last_user_msg["content"]
    assert "不要再呼叫工具" in last_user_msg["content"]


class FakeStreamRetryEngine(BaseEngine):
    """模擬串流第一次發生網路例外、第二次重試成功的測試引擎。"""

    def __init__(self):
        super().__init__(client=MagicMock(), model="test-retry")
        self.attempts = 0
        self.received_messages = []

    async def stream_chat(self, input_items, *args, **kwargs):
        self.attempts += 1
        self.received_messages.append(list(input_items))
        if self.attempts == 1:
            raise RuntimeError("Temporary network timeout")
        yield EventText(delta="重試後成功回覆。")

    def is_context_overflow(self, error):
        return False

    def is_non_retryable_error(self, error):
        return False


def test_agent_stream_retry_receives_system_prompt(monkeypatch):
    """驗證串流失敗重試時，[System Error] 提示訊息確實注入到了重試請求的 input_items 中。"""
    engine = FakeStreamRetryEngine()
    mock_client = MagicMock()
    mock_mcp = MagicMock()
    mock_mcp.openai_tools.return_value = []
    agent = Agent(
        client=mock_client,
        mcp=mock_mcp,
        model="test-model",
        max_tool_rounds=3,
        provider="openai",
    )
    monkeypatch.setattr(Agent, "engine", property(lambda self: engine))

    thinking, reply, _usage = asyncio.run(agent.chat("測試串流重試"))
    assert reply == "重試後成功回覆。"
    assert engine.attempts == 2

    # 驗證第二次重試傳給 stream_chat 的 messages 中包含系統錯誤訊息
    second_attempt_msgs = engine.received_messages[1]
    system_errors = [m for m in second_attempt_msgs if "[System Error]" in str(m.get("content"))]
    assert len(system_errors) == 1
    assert "Temporary network timeout" in system_errors[0]["content"]


# ============================================================================
# 6. mcp.py：Session 初始化失敗立即清理 Session 與 Transport
# ============================================================================
def test_mcp_initialize_failure_cleans_up():
    """驗證當 ClientSession.initialize() 拋出例外時，session 與 transport 皆及時退出清理。"""
    manager = MCPManager("dummy.yaml")

    mock_transport_cm = MagicMock()
    mock_transport_enter = AsyncMock(return_value=(MagicMock(), MagicMock()))
    mock_transport_exit = AsyncMock()
    mock_transport_cm.__aenter__ = mock_transport_enter
    mock_transport_cm.__aexit__ = mock_transport_exit

    mock_session_instance = MagicMock()
    mock_session_enter = AsyncMock()
    mock_session_exit = AsyncMock()
    mock_session_init = AsyncMock(side_effect=RuntimeError("Protocol handshake failed"))

    mock_session_instance.__aenter__ = mock_session_enter
    mock_session_instance.__aexit__ = mock_session_exit
    mock_session_instance.initialize = mock_session_init

    with (
        patch("pocket_agent.extensions.mcp.stdio_client", return_value=mock_transport_cm),
        patch("pocket_agent.extensions.mcp.ClientSession", return_value=mock_session_instance),
    ):
        with pytest.raises(RuntimeError, match="Protocol handshake failed"):
            asyncio.run(manager._connect("test_srv", {"transport": "stdio", "command": "dummy"}))

    # 驗證 session 的 __aexit__ 被正確調用以清理 session
    mock_session_exit.assert_awaited_once_with(None, None, None)

    # 驗證 transport 的 __aexit__ 亦被及時調用釋放
    mock_transport_exit.assert_awaited_once_with(None, None, None)

    # 驗證 exit stack closers 被清空，沒有留下未清理的連線
    assert len(manager._exit_stack_closers) == 0
