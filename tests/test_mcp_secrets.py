"""離線單元測試：MCP Keyring／JWE 機密儲存、YAML 原子修補與 MCPManager 整合生命週期。"""

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from pocket_agent.extensions.mcp import MCPManager
from pocket_agent.extensions.mcp_secrets import (
    ConfigManagerSecretStore,
    ConnPromptResult,
    SecretPersistResult,
    build_mcp_secret_key,
    is_secret_placeholder,
    normalize_secret_placeholder,
    update_mcp_config_env_value,
)
from pocket_agent.frontend.cli.main import _prompt_mcp_conn_params
from pocket_agent.frontend.cli.render import CliRenderer
from pocket_agent.system.config import (
    AuthException,
    ConfigManager,
    EncryptedConfigProvider,
    JsonConfigProvider,
    KeyringConfigProvider,
    RequireMasterPasswordException,
)
from pocket_agent.system.constants import (
    SECRET_PLACEHOLDER_ENCRYPTED,
    SECRET_PLACEHOLDER_KEYRING,
)


class _FakeSecretStore:
    """純記憶體 MCPSecretStore 測試替身（完全隔離真實 OS Keyring 與磁碟）。"""

    def __init__(self, *, use_encrypted: bool = False) -> None:
        self.data: dict[str, str] = {}
        self.use_encrypted = use_encrypted
        self.locked = False
        self.vault_exists = False
        self.unlock_calls: list[str] = []
        self.create_calls: list[str] = []
        self.delete_calls: list[tuple[str, str]] = []

    def get(self, server_name: str, env_key: str, placeholder: str) -> str | None:
        if placeholder == SECRET_PLACEHOLDER_ENCRYPTED and self.locked:
            raise AuthException("vault locked")
        return self.data.get(build_mcp_secret_key(server_name, env_key))

    def set(self, server_name: str, env_key: str, value: str) -> str:
        key = build_mcp_secret_key(server_name, env_key)
        if not self.use_encrypted:
            self.data[key] = value
            return SECRET_PLACEHOLDER_KEYRING
        if self.locked:
            raise AuthException("vault locked")
        if not self.vault_exists:
            raise RequireMasterPasswordException("create vault")
        self.data[key] = value
        return SECRET_PLACEHOLDER_ENCRYPTED

    def delete(self, server_name: str, env_key: str) -> bool:
        self.delete_calls.append((server_name, env_key))
        key = build_mcp_secret_key(server_name, env_key)
        return self.data.pop(key, None) is not None

    def unlock(self, password: str) -> bool:
        self.unlock_calls.append(password)
        if password == "master-secret":
            self.locked = False
            return True
        return False

    def create_vault_and_set(self, server_name: str, env_key: str, value: str, password: str) -> str:
        self.create_calls.append(password)
        self.vault_exists = True
        self.locked = False
        self.data[build_mcp_secret_key(server_name, env_key)] = value
        return SECRET_PLACEHOLDER_ENCRYPTED


def _make_github_session(reply_text: str = "PR #1 ok") -> SimpleNamespace:
    fake_tool = SimpleNamespace(
        name="github_list_prs",
        description="list prs",
        inputSchema={
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "github_token": {"type": "string"},
            },
            "required": ["owner", "repo"],
        },
    )
    return SimpleNamespace(
        list_tools=AsyncMock(return_value=SimpleNamespace(tools=[fake_tool])),
        call_tool=AsyncMock(return_value=MagicMock(content=[MagicMock(text=reply_text)])),
    )


def _isolated_config_manager(tmp_path: Path) -> ConfigManager:
    cfg_path = tmp_path / "config.json"
    secrets_path = tmp_path / "secrets.json"
    mgr = ConfigManager.__new__(ConfigManager)
    mgr.json_provider = JsonConfigProvider(cfg_path)
    mgr.keyring_provider = KeyringConfigProvider()
    mgr.encrypted_provider = EncryptedConfigProvider(secrets_path)
    mgr.providers = [mgr.keyring_provider, mgr.encrypted_provider, mgr.json_provider]
    return mgr


def test_placeholder_and_secret_key_helpers():
    assert normalize_secret_placeholder(" USE_KEYRING ") == SECRET_PLACEHOLDER_KEYRING
    assert normalize_secret_placeholder("USE_ENCRYPTED") == SECRET_PLACEHOLDER_ENCRYPTED
    assert normalize_secret_placeholder("ghp_plain") is None
    assert is_secret_placeholder("USE_KEYRING") is True
    assert is_secret_placeholder("plain") is False
    assert build_mcp_secret_key("github", "GITHUB_TOKEN") == "mcp.github.github_token"
    assert build_mcp_secret_key("GitHub Work!", "GITHUB_TOKEN") == "mcp.github_work.github_token"


def test_config_manager_secret_apis_isolated_from_config_json(tmp_path):
    mgr = _isolated_config_manager(tmp_path)
    kr_store: dict[str, str] = {}

    with (
        patch.object(mgr.keyring_provider, "get", side_effect=lambda k: kr_store.get(k)),
        patch.object(mgr.keyring_provider, "set", side_effect=lambda k, v: kr_store.__setitem__(k, v) or True),
        patch.object(mgr.keyring_provider, "delete", side_effect=lambda k: kr_store.pop(k, None) is not None),
    ):
        # 大小寫鍵名皆可正常讀寫且不寫入 config.json
        assert KeyringConfigProvider._is_secret_key("GITHUB_TOKEN") is True
        placeholder = mgr.set_secret("mcp.github.github_token", "tok_kr_123")
        assert placeholder == SECRET_PLACEHOLDER_KEYRING
        assert mgr.get_secret("mcp.github.github_token", placeholder=SECRET_PLACEHOLDER_KEYRING) == "tok_kr_123"
        raw_json = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert "mcp" not in raw_json
        assert mgr.delete_secret("mcp.github.github_token") is True
        assert mgr.get_secret("mcp.github.github_token", placeholder=SECRET_PLACEHOLDER_KEYRING) is None

    # 無 Keyring 時降級 JWE：先建庫再鎖定，確認鎖定時不覆寫既有的 LLM API 金鑰
    with (
        patch.object(mgr.keyring_provider, "get", return_value=None),
        patch.object(mgr.keyring_provider, "set", return_value=False),
        patch.object(mgr.keyring_provider, "delete", return_value=False),
    ):
        with pytest.raises(RequireMasterPasswordException):
            mgr.set_secret("mcp.github.github_token", "tok_enc_1")
        mgr.encrypted_provider.set("providers.openrouter.api_key", "llm_key_keep")
        mgr.encrypted_provider.save_vault("pw-1")
        assert mgr.set_secret("mcp.github.github_token", "tok_enc_1") == SECRET_PLACEHOLDER_ENCRYPTED

        # 模擬重啟後加密庫已存在且鎖定：set_secret 應拋 AuthException 而不是 RequireMasterPasswordException
        mgr2 = _isolated_config_manager(tmp_path)
        with (
            patch.object(mgr2.keyring_provider, "set", return_value=False),
            pytest.raises(AuthException),
        ):
            mgr2.set_secret("mcp.github.github_token", "tok_enc_2")

        assert mgr2.unlock("pw-1") is True
        assert mgr2.encrypted_provider.get("providers.openrouter.api_key") == "llm_key_keep"
        assert mgr2.get_secret("mcp.github.github_token", placeholder=SECRET_PLACEHOLDER_ENCRYPTED) == "tok_enc_1"
        assert mgr2.delete_secret("mcp.github.github_token") is True
        assert mgr2.get_secret("mcp.github.github_token", placeholder=SECRET_PLACEHOLDER_ENCRYPTED) is None


def test_update_mcp_config_env_value_preserves_comments_and_eol(tmp_path):
    cfg = tmp_path / "mcp_config.yaml"
    original = (
        "# 頂部註解\r\n"
        "servers:\r\n"
        "  # 停用範本\r\n"
        "  # memory:\r\n"
        "  #   command: python\r\n"
        "  github:\r\n"
        "    transport: stdio\r\n"
        "    env:\r\n"
        "      GITHUB_TOKEN: '' # 留空動態輸入\r\n"
        "      GITHUB_ALLOWED_REPOS: 'org/repo'\r\n"
    )
    cfg.write_bytes(original.encode("utf-8"))

    ok = update_mcp_config_env_value(cfg, "github", "GITHUB_TOKEN", SECRET_PLACEHOLDER_KEYRING)
    assert ok is True

    updated_bytes = cfg.read_bytes()
    assert b"\r\n" in updated_bytes
    updated_text = updated_bytes.decode("utf-8")
    assert "# 頂部註解" in updated_text
    assert "# 停用範本" in updated_text
    assert "GITHUB_TOKEN: 'USE_KEYRING' # 留空動態輸入" in updated_text
    parsed = yaml.safe_load(updated_text)
    assert parsed["servers"]["github"]["env"]["GITHUB_TOKEN"] == "USE_KEYRING"
    assert parsed["servers"]["github"]["env"]["GITHUB_ALLOWED_REPOS"] == "org/repo"


def test_update_mcp_config_env_value_inserts_missing_key_and_rejects_flow_or_example(tmp_path):
    cfg = tmp_path / "mcp_config.yaml"
    cfg.write_text("servers:\n  github:\n    transport: stdio\n", encoding="utf-8")
    assert update_mcp_config_env_value(cfg, "github", "GITHUB_TOKEN", SECRET_PLACEHOLDER_KEYRING) is True
    assert yaml.safe_load(cfg.read_text(encoding="utf-8"))["servers"]["github"]["env"]["GITHUB_TOKEN"] == "USE_KEYRING"

    flow_cfg = tmp_path / "flow.yaml"
    flow_text = "servers:\n  github:\n    env: {GITHUB_TOKEN: ''}\n"
    flow_cfg.write_text(flow_text, encoding="utf-8")
    assert update_mcp_config_env_value(flow_cfg, "github", "GITHUB_TOKEN", SECRET_PLACEHOLDER_KEYRING) is False
    assert flow_cfg.read_text(encoding="utf-8") == flow_text

    from pocket_agent.system.paths import mcp_example_path

    assert (
        update_mcp_config_env_value(mcp_example_path(), "github", "GITHUB_TOKEN", SECRET_PLACEHOLDER_KEYRING) is False
    )


def test_mcp_manager_ac1_keyring_resolve_and_stdio_scrub(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    cfg = tmp_path / "mcp_config.yaml"
    cfg.write_text(
        "servers:\n"
        "  github:\n"
        "    transport: stdio\n"
        "    command: python\n"
        "    args: ['mcp_servers/github_mcp.py']\n"
        "    env:\n"
        "      GITHUB_TOKEN: 'USE_KEYRING'\n",
        encoding="utf-8",
    )
    store = _FakeSecretStore()
    store.data["mcp.github.github_token"] = "tok_from_keyring"

    manager = MCPManager(config_path=cfg, secret_store=store)
    stdio_params = manager._resolve_stdio_params(yaml.safe_load(cfg.read_text(encoding="utf-8"))["servers"]["github"])
    assert stdio_params.env is not None
    assert "GITHUB_TOKEN" not in stdio_params.env

    session = _make_github_session()
    manager._server_envs["github"] = {"GITHUB_TOKEN": "USE_KEYRING"}
    manager.on_prompt_conn = MagicMock()
    asyncio.run(manager._register_tools("github", session, inject={}, safe_read_tools="*"))

    out = asyncio.run(manager.call("mcp__github__github_list_prs", {"owner": "octocat", "repo": "hello"}))
    assert "PR #1 ok" in out
    manager.on_prompt_conn.assert_not_called()
    session.call_tool.assert_awaited_once_with(
        "github_list_prs",
        {"owner": "octocat", "repo": "hello", "github_token": "tok_from_keyring"},
    )


def test_mcp_manager_ac2_encrypted_vault_unlock(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    cfg = tmp_path / "mcp_config.yaml"
    cfg.write_text("servers:\n  github:\n    env:\n      GITHUB_TOKEN: 'USE_ENCRYPTED'\n", encoding="utf-8")
    store = _FakeSecretStore(use_encrypted=True)
    store.vault_exists = True
    store.locked = True
    store.data["mcp.github.github_token"] = "tok_from_jwe"

    manager = MCPManager(config_path=cfg, secret_store=store)
    manager._server_envs["github"] = {"GITHUB_TOKEN": "USE_ENCRYPTED"}
    manager.on_vault_unlock = lambda _srv, unlock_fn: unlock_fn("master-secret")

    session = _make_github_session()
    asyncio.run(manager._register_tools("github", session, inject={}, safe_read_tools="*"))

    asyncio.run(manager.call("mcp__github__github_list_prs", {"owner": "octocat", "repo": "hello"}))
    assert store.unlock_calls == ["master-secret"]
    session.call_tool.assert_awaited_once_with(
        "github_list_prs",
        {"owner": "octocat", "repo": "hello", "github_token": "tok_from_jwe"},
    )


def test_mcp_manager_ac3_ac4_clear_placeholder_hot_reload_and_deferred_save(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    cfg = tmp_path / "mcp_config.yaml"
    cfg.write_text(
        "# keep comment\nservers:\n  github:\n    env:\n      GITHUB_TOKEN: 'USE_KEYRING'\n",
        encoding="utf-8",
    )
    store = _FakeSecretStore()
    store.data["mcp.github.github_token"] = "old_tok"

    manager = MCPManager(config_path=cfg, secret_store=store)
    manager._config_file_sig = manager._stat_config_file()
    manager._server_envs["github"] = {"GITHUB_TOKEN": "USE_KEYRING"}
    persisted_events: list[SecretPersistResult] = []
    manager.on_secret_persisted = persisted_events.append

    session = _make_github_session()
    asyncio.run(manager._register_tools("github", session, inject={}, safe_read_tools="*"))

    # 第一次使用舊 Keyring Token
    asyncio.run(manager.call("mcp__github__github_list_prs", {"owner": "octocat", "repo": "hello"}))
    assert session.call_tool.call_args[0][1]["github_token"] == "old_tok"

    # 同會話內使用者清空 mcp_config.yaml 的 USE_KEYRING（確保 mtime/size 變動）
    cfg.write_text(
        "# keep comment\nservers:\n  github:\n    env:\n      GITHUB_TOKEN: ''\n",
        encoding="utf-8",
    )
    st = cfg.stat()
    os.utime(cfg, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    manager.on_prompt_conn = MagicMock(
        return_value=ConnPromptResult(
            values={"GITHUB_TOKEN": "new_valid_tok"},
            save_to_keyring=frozenset({"GITHUB_TOKEN"}),
        )
    )
    asyncio.run(manager.call("mcp__github__github_list_prs", {"owner": "octocat", "repo": "hello"}))

    manager.on_prompt_conn.assert_called_once_with("github", ["GITHUB_TOKEN"])
    assert session.call_tool.call_args[0][1]["github_token"] == "new_valid_tok"
    assert store.data["mcp.github.github_token"] == "new_valid_tok"
    assert "# keep comment" in cfg.read_text(encoding="utf-8")
    assert yaml.safe_load(cfg.read_text(encoding="utf-8"))["servers"]["github"]["env"]["GITHUB_TOKEN"] == "USE_KEYRING"
    assert len(persisted_events) == 1
    assert persisted_events[0].saved is True
    assert persisted_events[0].yaml_updated is True


def test_mcp_manager_ac5_401_skips_persist_and_avoids_loop(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    cfg = tmp_path / "mcp_config.yaml"
    cfg.write_text("servers:\n  github:\n    env:\n      GITHUB_TOKEN: ''\n", encoding="utf-8")
    store = _FakeSecretStore()

    manager = MCPManager(config_path=cfg, secret_store=store)
    manager._config_file_sig = manager._stat_config_file()
    manager._server_envs["github"] = {"GITHUB_TOKEN": ""}
    persisted_events: list[SecretPersistResult] = []
    manager.on_secret_persisted = persisted_events.append

    session = _make_github_session("[ERROR] [CONN_AUTH] GitHub 認證失敗 (HTTP 401)")
    asyncio.run(manager._register_tools("github", session, inject={}, safe_read_tools="*"))

    manager.on_prompt_conn = MagicMock(
        side_effect=[
            ConnPromptResult(values={"GITHUB_TOKEN": "bad_tok"}, save_to_keyring=frozenset({"GITHUB_TOKEN"})),
            ConnPromptResult(values={"GITHUB_TOKEN": "good_tok"}, save_to_keyring=frozenset({"GITHUB_TOKEN"})),
        ]
    )

    # 第一次回傳 401：不得寫入 Keyring 或 YAML
    asyncio.run(manager.call("mcp__github__github_list_prs", {"owner": "octocat", "repo": "hello"}))
    assert "mcp.github.github_token" not in store.data
    assert yaml.safe_load(cfg.read_text(encoding="utf-8"))["servers"]["github"]["env"]["GITHUB_TOKEN"] == ""
    assert persisted_events[-1].saved is False
    assert persisted_events[-1].reason == "auth_error"

    # 第二次恢復 200：應重新詢問並成功落盤
    session.call_tool = AsyncMock(return_value=MagicMock(content=[MagicMock(text="OK")]))
    asyncio.run(manager.call("mcp__github__github_list_prs", {"owner": "octocat", "repo": "hello"}))
    assert manager.on_prompt_conn.call_count == 2
    assert store.data["mcp.github.github_token"] == "good_tok"
    assert yaml.safe_load(cfg.read_text(encoding="utf-8"))["servers"]["github"]["env"]["GITHUB_TOKEN"] == "USE_KEYRING"


def test_mcp_manager_ac5_existing_keyring_401_bypasses_keyring_on_retry(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    cfg = tmp_path / "mcp_config.yaml"
    cfg.write_text("servers:\n  github:\n    env:\n      GITHUB_TOKEN: 'USE_KEYRING'\n", encoding="utf-8")
    store = _FakeSecretStore()
    store.data["mcp.github.github_token"] = "expired_tok"

    manager = MCPManager(config_path=cfg, secret_store=store)
    manager._config_file_sig = manager._stat_config_file()
    manager._server_envs["github"] = {"GITHUB_TOKEN": "USE_KEYRING"}

    session = _make_github_session("[ERROR] [CONN_AUTH] 401")
    asyncio.run(manager._register_tools("github", session, inject={}, safe_read_tools="*"))
    manager.on_prompt_conn = MagicMock(
        return_value=ConnPromptResult(
            values={"GITHUB_TOKEN": "fresh_tok"}, save_to_keyring=frozenset({"GITHUB_TOKEN"})
        ),
    )

    asyncio.run(manager.call("mcp__github__github_list_prs", {"owner": "octocat", "repo": "hello"}))
    session.call_tool = AsyncMock(return_value=MagicMock(content=[MagicMock(text="OK")]))
    asyncio.run(manager.call("mcp__github__github_list_prs", {"owner": "octocat", "repo": "hello"}))

    manager.on_prompt_conn.assert_called_once_with("github", ["GITHUB_TOKEN"])
    assert store.data["mcp.github.github_token"] == "fresh_tok"


def test_mcp_manager_ac6_decline_save_deletes_old_keyring_secret(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    cfg = tmp_path / "mcp_config.yaml"
    cfg.write_text("servers:\n  github:\n    env:\n      GITHUB_TOKEN: ''\n", encoding="utf-8")
    store = _FakeSecretStore()
    store.data["mcp.github.github_token"] = "stale_old_tok"

    manager = MCPManager(config_path=cfg, secret_store=store)
    manager._config_file_sig = manager._stat_config_file()
    manager._server_envs["github"] = {"GITHUB_TOKEN": ""}

    session = _make_github_session()
    asyncio.run(manager._register_tools("github", session, inject={}, safe_read_tools="*"))
    manager.on_prompt_conn = MagicMock(
        return_value=ConnPromptResult(values={"GITHUB_TOKEN": "session_only_tok"}, save_to_keyring=frozenset()),
    )

    asyncio.run(manager.call("mcp__github__github_list_prs", {"owner": "octocat", "repo": "hello"}))
    assert "mcp.github.github_token" not in store.data
    assert store.delete_calls == [("github", "GITHUB_TOKEN")]
    assert yaml.safe_load(cfg.read_text(encoding="utf-8"))["servers"]["github"]["env"]["GITHUB_TOKEN"] == ""


def test_mcp_manager_ac7_env_fallback_when_yaml_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "env_pat_tok")
    cfg = tmp_path / "mcp_config.yaml"
    cfg.write_text("servers:\n  github:\n    env:\n      GITHUB_TOKEN: ''\n", encoding="utf-8")
    store = _FakeSecretStore()
    store.data["mcp.github.github_token"] = "old_kr_tok"

    manager = MCPManager(config_path=cfg, secret_store=store)
    manager._server_envs["github"] = {"GITHUB_TOKEN": ""}
    manager.on_prompt_conn = MagicMock()
    session = _make_github_session()
    asyncio.run(manager._register_tools("github", session, inject={}, safe_read_tools="*"))

    asyncio.run(manager.call("mcp__github__github_list_prs", {"owner": "octocat", "repo": "hello"}))
    manager.on_prompt_conn.assert_not_called()
    session.call_tool.assert_awaited_once_with(
        "github_list_prs",
        {"owner": "octocat", "repo": "hello", "github_token": "env_pat_tok"},
    )


def test_cli_prompt_mcp_conn_params_default_false_and_spinner_lifecycle():
    from rich.console import Console

    renderer = CliRenderer(Console(quiet=True))
    fake_status = MagicMock()
    renderer._active_status = fake_status

    with (
        patch("rich.prompt.Prompt.ask", return_value="pat_cli_val"),
        patch("rich.prompt.Confirm.ask", return_value=False) as mock_confirm,
    ):
        res = _prompt_mcp_conn_params(renderer, "github", ["GITHUB_TOKEN"])

    assert isinstance(res, ConnPromptResult)
    assert res.values == {"GITHUB_TOKEN": "pat_cli_val"}
    assert res.save_to_keyring == frozenset()
    assert mock_confirm.call_args.kwargs["default"] is False
    fake_status.stop.assert_called_once()
    fake_status.start.assert_called_once()


def test_config_manager_secret_store_adapter(tmp_path):
    mgr = _isolated_config_manager(tmp_path)
    adapter = ConfigManagerSecretStore(mgr)
    with patch.object(mgr.keyring_provider, "set", return_value=False):
        assert (
            adapter.create_vault_and_set("github", "GITHUB_TOKEN", "tok_v", "pw-vault") == SECRET_PLACEHOLDER_ENCRYPTED
        )
    assert adapter.get("github", "GITHUB_TOKEN", SECRET_PLACEHOLDER_ENCRYPTED) == "tok_v"
    assert adapter.delete("github", "GITHUB_TOKEN") is True
