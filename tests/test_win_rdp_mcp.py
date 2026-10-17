import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_servers import win_rdp_mcp
from pocket_agent.extensions.mcp import MCPManager


@pytest.fixture(autouse=True)
def _clean_win_env_and_cache(monkeypatch):
    win_rdp_mcp._RUNTIME_CREDS.clear()
    win_rdp_mcp._ACTIVE_SESSION.update({"process": None, "hwnd": 0, "target": ""})
    for key in (
        "WIN_HOST",
        "WIN_RDP_PORT",
        "WIN_RDP_USER",
        "WIN_RDP_PASSWORD",
        "WIN_RDP_TIMEOUT",
        "WIN_RDP_RESOLUTION",
    ):
        monkeypatch.delenv(key, raising=False)


def _set_full_win_creds(monkeypatch):
    monkeypatch.setenv("WIN_HOST", "10.20.30.40")
    monkeypatch.setenv("WIN_RDP_PORT", "3389")
    monkeypatch.setenv("WIN_RDP_USER", r"CORP\winadmin")
    monkeypatch.setenv("WIN_RDP_PASSWORD", "WinSecret#2026")


def test_missing_password_rejected_and_partial_config_cached(monkeypatch):
    monkeypatch.setenv("WIN_HOST", "10.20.30.40")
    monkeypatch.setenv("WIN_RDP_PORT", "")
    monkeypatch.setenv("WIN_RDP_USER", r"CORP\winadmin")

    with patch("mcp_servers.win_rdp_mcp._tty_prompt", side_effect=["", ""]):
        with pytest.raises(ValueError, match="WIN_RDP_PASSWORD"):
            win_rdp_mcp._resolve_connection_params()

    prompts = iter(["", "MyRdpPass!99"])
    with patch("mcp_servers.win_rdp_mcp._tty_prompt", side_effect=lambda *a, **kw: next(prompts)) as mock_prompt:
        host, port, user, pw = win_rdp_mcp._resolve_connection_params()
        assert host == "10.20.30.40"
        assert port == "3389"
        assert user == r"CORP\winadmin"
        assert pw == "MyRdpPass!99"
        assert mock_prompt.call_count == 2

        # 第二次呼叫應直接命中記憶體快取，不再詢問
        host2, port2, user2, pw2 = win_rdp_mcp._resolve_connection_params()
        assert (host2, port2, user2, pw2) == ("10.20.30.40", "3389", r"CORP\winadmin", "MyRdpPass!99")
        assert mock_prompt.call_count == 2


def test_rdp_file_disables_backdoors_and_enables_clipboard(monkeypatch):
    monkeypatch.setenv("WIN_RDP_RESOLUTION", "1600x900")
    content = win_rdp_mcp._build_rdp_file_content("10.20.30.40", "3390", r"CORP\winadmin")
    assert "full address:s:10.20.30.40:3390" in content
    assert r"username:s:CORP\winadmin" in content
    assert "desktopwidth:i:1600" in content
    assert "desktopheight:i:900" in content
    assert "redirectclipboard:i:1" in content
    assert "redirectdrives:i:0" in content
    assert "redirectsmartcards:i:0" in content
    assert "redirectwebauthn:i:0" in content
    assert "prompt for credentials:i:0" in content


def test_mouse_bounds_and_blocked_hotkeys():
    win_rdp_mcp._validate_mouse_bounds(0, 0, 1280, 800)
    win_rdp_mcp._validate_mouse_bounds(1279, 799, 1280, 800)

    with pytest.raises(ValueError, match="超出遠端桌面視窗客戶區範圍"):
        win_rdp_mcp._validate_mouse_bounds(-1, 50, 1280, 800)
    with pytest.raises(ValueError, match="超出遠端桌面視窗客戶區範圍"):
        win_rdp_mcp._validate_mouse_bounds(1280, 50, 1280, 800)

    for blocked in ("ctrl+alt+del", "Ctrl+Alt+Delete", "win+l", "alt+tab"):
        with pytest.raises(ValueError, match="禁止發送系統保護組合鍵"):
            win_rdp_mcp._send_vk_combo(12345, blocked)


@pytest.mark.parametrize(
    "bad_service",
    [
        "Spooler & calc.exe",
        "wuauserv | clip",
        "-invalid",
        "svc;whoami",
    ],
)
def test_service_status_blocks_injection(monkeypatch, bad_service):
    _set_full_win_creds(monkeypatch)
    with patch("mcp_servers.win_rdp_mcp._run_rdp_clip_command") as mock_clip:
        out = win_rdp_mcp.win_service_status(service_name=bad_service)
        assert out.startswith("[ERROR]")
        mock_clip.assert_not_called()


def test_query_eventlog_validation(monkeypatch):
    _set_full_win_creds(monkeypatch)
    with patch("mcp_servers.win_rdp_mcp._run_rdp_clip_command", return_value="Event 1") as mock_clip:
        bad_log = win_rdp_mcp.win_query_eventlog(log_name="EvilLog")
        assert bad_log.startswith("[ERROR]")
        bad_lvl = win_rdp_mcp.win_query_eventlog(log_name="System", level="bogus")
        assert bad_lvl.startswith("[ERROR]")
        mock_clip.assert_not_called()

        ok = win_rdp_mcp.win_query_eventlog(log_name="System", level="error", lines=500)
        assert ok == "Event 1"
        cmd_sent = mock_clip.call_args.args[0]
        assert 'wevtutil.exe qe "System"' in cmd_sent
        assert "Level=2" in cmd_sent
        assert "/c:100" in cmd_sent


@pytest.mark.parametrize(
    "blocked_path",
    [
        "/etc/passwd",
        r"C:\Windows\..\Users\Administrator\Desktop\secret.txt",
        r"C:\Windows\System32\config\SAM",
        r"C:\Windows\NTDS\ntds.dit",
        r"C:\inetpub\wwwroot\web.config",
        r"C:\ProgramData\app\.env",
        r"C:\ProgramData\certs\server.pfx",
        r"C:\Users\Administrator\ntuser.dat",
    ],
)
def test_win_read_config_blocks_sensitive_paths(monkeypatch, blocked_path):
    _set_full_win_creds(monkeypatch)
    with patch("mcp_servers.win_rdp_mcp._run_rdp_clip_command") as mock_clip:
        out = win_rdp_mcp.win_read_config(file_path=blocked_path)
        assert out.startswith("[ERROR]")
        mock_clip.assert_not_called()


def test_win_read_config_allows_hosts_and_redacts():
    raw = '127.0.0.1 localhost\npassword = MySuperSecretPassword\nConnectionString="Server=db;Password=hidden;"\n'
    redacted = win_rdp_mcp._redact_and_truncate(raw)
    assert "127.0.0.1 localhost" in redacted
    assert "MySuperSecretPassword" not in redacted
    assert "Server=db;Password=hidden;" not in redacted
    assert "[REDACTED]" in redacted


def test_mcp_manager_strips_rdp_params_and_prompts_only_win_keys():
    manager = MCPManager()
    manager._server_envs["win_rdp"] = {
        "WIN_HOST": "10.0.0.55",
        "WIN_RDP_PORT": "3389",
    }
    fake_tools = [
        SimpleNamespace(
            name="win_system_overview",
            description="win overview",
            inputSchema={
                "type": "object",
                "properties": {
                    "rdp_host": {"type": "string"},
                    "rdp_port": {"type": "string"},
                    "rdp_user": {"type": "string"},
                    "rdp_password": {"type": "string"},
                },
            },
        ),
        SimpleNamespace(
            name="win_rdp_mouse_action",
            description="mouse click",
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "rdp_host": {"type": "string"},
                    "rdp_port": {"type": "string"},
                    "rdp_user": {"type": "string"},
                    "rdp_password": {"type": "string"},
                },
                "required": ["action", "x", "y"],
            },
        ),
    ]
    fake_session = SimpleNamespace(
        list_tools=AsyncMock(return_value=SimpleNamespace(tools=fake_tools)),
        call_tool=AsyncMock(return_value=MagicMock(content=[MagicMock(text="Windows Server 2022")])),
    )
    asyncio.run(
        manager._register_tools(
            "win_rdp",
            fake_session,
            inject={},
            safe_read_tools=["win_system_overview"],
        )
    )
    schemas = {s["name"]: s for s in manager.openai_tools()}
    assert schemas["win_system_overview"].get("_is_safe_read") is True
    assert schemas["win_system_overview"]["parameters"]["properties"] == {}
    # win_rdp_mouse_action 未列入 safe_read_tools，必須維持預設攔截確認，且隱藏 rdp_* 參數
    assert schemas["win_rdp_mouse_action"].get("_is_safe_read") is None
    assert set(schemas["win_rdp_mouse_action"]["parameters"]["properties"].keys()) == {"action", "x", "y"}

    prompt_calls = []

    def fake_prompt(server_name, missing_keys):
        prompt_calls.append((server_name, missing_keys))
        return {
            "WIN_RDP_USER": r"CORP\ops",
            "WIN_RDP_PASSWORD": "RdpSecret!",
        }

    manager.on_prompt_conn = fake_prompt
    asyncio.run(manager.call("win_system_overview", {}))

    assert prompt_calls == [("win_rdp", ["WIN_RDP_USER", "WIN_RDP_PASSWORD"])]
    _, sent_args = fake_session.call_tool.call_args.args
    assert sent_args == {
        "rdp_host": "10.0.0.55",
        "rdp_port": "3389",
        "rdp_user": r"CORP\ops",
        "rdp_password": "RdpSecret!",
    }
