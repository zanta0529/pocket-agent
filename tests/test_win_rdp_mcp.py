import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_servers import win_rdp_mcp
from pocket_agent.extensions.mcp import MCPManager


@pytest.fixture(autouse=True)
def _clean_win_env_and_cache(monkeypatch):
    win_rdp_mcp._RUNTIME_CREDS.clear()
    win_rdp_mcp._ACTIVE_SESSION.update({"process": None, "pid": 0, "exe_path": "", "hwnd": 0, "target": ""})
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
    # 零落盤安全保證：.rdp 設定檔絕不包含 username 或 password
    content = win_rdp_mcp._build_rdp_file_content("10.20.30.40", "3390")
    assert "full address:s:10.20.30.40:3390" in content
    assert "username:s:" not in content
    assert "password" not in content.lower()
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
    assert schemas["mcp__win_rdp__win_system_overview"].get("_is_safe_read") is True
    assert schemas["mcp__win_rdp__win_system_overview"]["parameters"]["properties"] == {}
    # win_rdp_mouse_action 未列入 safe_read_tools，必須維持預設攔截確認，且隱藏 rdp_* 參數
    assert schemas["mcp__win_rdp__win_rdp_mouse_action"].get("_is_safe_read") is None
    assert set(schemas["mcp__win_rdp__win_rdp_mouse_action"]["parameters"]["properties"].keys()) == {
        "action",
        "x",
        "y",
    }

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


def test_tty_prompt_non_interactive_raises_value_error():
    """驗證無 TTY 終端機時直接拋出 ValueError，絕不降級至 sys.stdin.readline() 污染 MCP 協定。"""
    with patch("builtins.open", side_effect=OSError("No CONIN$ device")):
        with pytest.raises(ValueError, match="沒有可用的互動終端機"):
            win_rdp_mcp._tty_prompt("請輸入主機位址: ")


def test_run_rdp_clip_command_preserves_and_restores_user_clipboard():
    """驗證執行 RDP 剪貼簿巡檢前備份本機剪貼簿，且執行完畢後立即還原使用者原資料。"""
    user_private_data = "MyPersonalPasswordOrPrivateMessage123"
    clipboard_state = [user_private_data]

    def fake_get():
        return clipboard_state[0]

    def fake_set(val):
        clipboard_state[0] = val

    with (
        patch("mcp_servers.win_rdp_mcp._ensure_rdp_session", return_value=(999, "10.20.30.40")),
        patch("mcp_servers.win_rdp_mcp._send_vk_combo"),
        patch("mcp_servers.win_rdp_mcp._send_unicode_text"),
        patch("mcp_servers.win_rdp_mcp._get_clipboard_text", side_effect=fake_get),
        patch("mcp_servers.win_rdp_mcp._set_clipboard_text", side_effect=fake_set),
    ):
        # 模擬遠端執行產出專屬簽章包裹內容
        def remote_exec_simulator(*args, **kwargs):
            # 取出送進去的 wrapped_cmd
            cmd = args[1]
            import re

            m = re.search(r"echo (__PKT_CLIP_START_[a-f0-9]+__) & .* & echo (__PKT_CLIP_END_[a-f0-9]+__)", cmd)
            if m:
                s_tag, e_tag = m.group(1), m.group(2)
                clipboard_state[0] = f"{s_tag}\nRemote Execution OK Result\n{e_tag}"

        with patch("mcp_servers.win_rdp_mcp._send_unicode_text", side_effect=remote_exec_simulator):
            out = win_rdp_mcp._run_rdp_clip_command("hostname", rdp_host="10.20.30.40", rdp_password="pw")
            assert "Remote Execution OK Result" in out
            # 驗證本機原剪貼簿完好無損地被還原
            assert clipboard_state[0] == user_private_data


def test_focus_loss_aborts_key_sending():
    """驗證當前景視窗焦點轉移時，立即中止按鍵發送以杜絕注入本機視窗，且確保已按下的按鍵在 finally 中釋放。"""
    with (
        patch("mcp_servers.win_rdp_mcp._focus_rdp_window", return_value=True),
        patch("ctypes.windll.user32.keybd_event") as mock_keybd,
        patch("ctypes.windll.user32.GetForegroundWindow", side_effect=[999, 999, 888]),
    ):
        with pytest.raises(RuntimeError, match="焦點轉移"):
            win_rdp_mcp._send_vk_combo(999, "win+r")

        # 驗證即使中途焦點轉移拋出例外，已按下的按鍵（Win 鍵 0x5B）依然有在 finally 中被釋放 (KEYEVENTF_KEYUP)
        assert mock_keybd.call_count == 2
        # 第一次為 KeyDown (0x5B, 0, 0, 0)
        assert mock_keybd.call_args_list[0].args == (0x5B, 0, 0, 0)
        # 第二次為 KeyUp (0x5B, 0, 2, 0)
        assert mock_keybd.call_args_list[1].args == (0x5B, 0, 0x0002, 0)


def test_send_vk_combo_normal_flow_presses_and_releases():
    """驗證正常發送組合鍵時依序按下並反向釋放所有虛擬按鍵。"""
    with (
        patch("mcp_servers.win_rdp_mcp._focus_rdp_window", return_value=True),
        patch("ctypes.windll.user32.keybd_event") as mock_keybd,
        patch("ctypes.windll.user32.GetForegroundWindow", return_value=999),
    ):
        win_rdp_mcp._send_vk_combo(999, "ctrl+shift+esc")
        # 依序按下 ctrl(0x11)、shift(0x10)、esc(0x1B)，再反向依序釋放
        assert mock_keybd.call_count == 6
        assert [call.args[0] for call in mock_keybd.call_args_list] == [0x11, 0x10, 0x1B, 0x1B, 0x10, 0x11]
        assert [call.args[2] for call in mock_keybd.call_args_list] == [0, 0, 0, 2, 2, 2]


def test_rdp_session_identity_rejects_spoofed_title_wrong_pid_exe_class_and_target():
    """測試視窗綁定：拒絕偽造標題、錯誤 PID、非系統 mstsc、近似類別與不同 target。"""
    trusted_exe = r"C:\Windows\System32\mstsc.exe"
    alive_proc = SimpleNamespace(pid=4321, poll=lambda: None)
    exited_proc = SimpleNamespace(pid=4321, poll=lambda: 0)

    with (
        patch("mcp_servers.win_rdp_mcp.os.name", "nt"),
        patch("ctypes.windll.user32.IsWindow", return_value=True),
        patch("ctypes.windll.user32.IsWindowVisible", return_value=True),
        patch("mcp_servers.win_rdp_mcp._get_window_class_name", return_value="TscShellContainerClass"),
        patch("mcp_servers.win_rdp_mcp._get_window_pid", return_value=4321),
        patch("mcp_servers.win_rdp_mcp._get_process_image_path", return_value=trusted_exe),
    ):
        # 1. 合法綁定通過
        assert win_rdp_mcp._verify_rdp_hwnd_identity(777, 4321, trusted_exe, alive_proc) is True

        # 2. 行程已退出 -> 拒絕
        assert win_rdp_mcp._verify_rdp_hwnd_identity(777, 4321, trusted_exe, exited_proc) is False

        # 3. PID 不符（例如本機其他程式或 mstsc 轉交給未知 PID） -> 拒絕
        assert win_rdp_mcp._verify_rdp_hwnd_identity(777, 9999, trusted_exe, alive_proc) is False

        # 4. 執行檔路徑不符（例如偽造之 C:\Temp\mstsc.exe） -> 拒絕
        assert win_rdp_mcp._verify_rdp_hwnd_identity(777, 4321, r"C:\Temp\mstsc.exe", alive_proc) is False

    # 5. 近似但非精確的視窗類別（如 FakeTscShellContainerClass） -> 拒絕
    with (
        patch("mcp_servers.win_rdp_mcp.os.name", "nt"),
        patch("ctypes.windll.user32.IsWindow", return_value=True),
        patch("ctypes.windll.user32.IsWindowVisible", return_value=True),
        patch("mcp_servers.win_rdp_mcp._get_window_class_name", return_value="FakeTscShellContainerClass"),
        patch("mcp_servers.win_rdp_mcp._get_window_pid", return_value=4321),
        patch("mcp_servers.win_rdp_mcp._get_process_image_path", return_value=trusted_exe),
    ):
        assert win_rdp_mcp._verify_rdp_hwnd_identity(777, 4321, trusted_exe, alive_proc) is False

    # 6. _verify_active_session 檢查不同 port / user target key -> 拒絕
    win_rdp_mcp._ACTIVE_SESSION.update(
        {
            "process": alive_proc,
            "pid": 4321,
            "exe_path": trusted_exe,
            "hwnd": 777,
            "target": r"CORP\winadmin@10.20.30.40:3389",
        }
    )
    with patch("mcp_servers.win_rdp_mcp._verify_rdp_hwnd_identity", return_value=True):
        assert win_rdp_mcp._verify_active_session(777, r"CORP\winadmin@10.20.30.40:3389") is True
        assert win_rdp_mcp._verify_active_session(777, r"CORP\winadmin@10.20.30.40:3390") is False
        assert win_rdp_mcp._verify_active_session(777, r"CORP\other@10.20.30.40:3389") is False
        assert win_rdp_mcp._verify_active_session(888, r"CORP\winadmin@10.20.30.40:3389") is False


def test_ensure_rdp_session_does_not_adopt_unverified_window_and_binds_spawned_pid(monkeypatch):
    """驗證 _ensure_rdp_session 不依標題接管既有視窗，僅綁定新啟動之可信 PID。"""
    _set_full_win_creds(monkeypatch)
    trusted_exe = r"C:\Windows\System32\mstsc.exe"
    fake_proc = SimpleNamespace(pid=5566, poll=lambda: None, terminate=MagicMock())

    with (
        patch("mcp_servers.win_rdp_mcp.os.name", "nt"),
        patch("mcp_servers.win_rdp_mcp._get_trusted_mstsc_path", return_value=trusted_exe),
        patch("mcp_servers.win_rdp_mcp._set_ephemeral_rdp_credential"),
        patch("mcp_servers.win_rdp_mcp._delete_ephemeral_rdp_credential"),
        patch("subprocess.Popen", return_value=fake_proc) as mock_popen,
        patch("mcp_servers.win_rdp_mcp._find_rdp_hwnd_for_process", return_value=31337) as mock_find,
        patch("mcp_servers.win_rdp_mcp._verify_rdp_hwnd_identity", return_value=True),
        patch("time.sleep"),
    ):
        hwnd, host = win_rdp_mcp._ensure_rdp_session()
        assert hwnd == 31337
        assert host == "10.20.30.40"
        mock_popen.assert_called_once()
        assert mock_popen.call_args.args[0][0] == trusted_exe
        mock_find.assert_called_once_with(5566, trusted_exe, fake_proc)
        assert win_rdp_mcp._ACTIVE_SESSION["pid"] == 5566
        assert win_rdp_mcp._ACTIVE_SESSION["hwnd"] == 31337


def test_win_rdp_screenshot_aborts_on_focus_failure_and_uses_window_capture(monkeypatch, tmp_path):
    """驗證聚焦失敗時立即中止擷取；成功時使用 ImageGrab.grab(window=hwnd)。"""
    _set_full_win_creds(monkeypatch)
    fake_img = MagicMock()

    # 1. 聚焦失敗時不得呼叫 ImageGrab.grab 或儲存圖檔
    with (
        patch("mcp_servers.win_rdp_mcp._ensure_rdp_session", return_value=(777, "10.20.30.40")),
        patch("mcp_servers.win_rdp_mcp._verify_active_session", return_value=True),
        patch("mcp_servers.win_rdp_mcp._focus_rdp_window", return_value=False),
        patch("PIL.ImageGrab.grab") as mock_grab,
    ):
        out_fail = win_rdp_mcp.win_rdp_screenshot(monitor_wait_sec=0)
        assert out_fail.startswith("[ERROR]")
        assert "無法將遠端桌面視窗置於前景" in out_fail
        mock_grab.assert_not_called()

    # 2. 聚焦與身分驗證通過時，必須使用 window=hwnd 擷取，不得傳入桌面 bbox
    with (
        patch("mcp_servers.win_rdp_mcp._ensure_rdp_session", return_value=(777, "10.20.30.40")),
        patch("mcp_servers.win_rdp_mcp._verify_active_session", return_value=True),
        patch("mcp_servers.win_rdp_mcp._focus_rdp_window", return_value=True),
        patch("mcp_servers.win_rdp_mcp._get_client_rect_screen", return_value=(100, 100, 1280, 800)),
        patch("PIL.ImageGrab.grab", return_value=fake_img) as mock_grab_ok,
        patch("tempfile.gettempdir", return_value=str(tmp_path)),
    ):
        out_ok = win_rdp_mcp.win_rdp_screenshot(monitor_wait_sec=0)
        assert out_ok.startswith("[OK]")
        mock_grab_ok.assert_called_once_with(window=777)
        fake_img.save.assert_called_once()
