from unittest.mock import MagicMock, patch

import pytest

from pocket_agent.tools.document import call_document_tool
from pocket_agent.tools.fs import _validate_safe_path, call_fs_tool
from pocket_agent.tools.web import call_web_tool


# ==========================================
# 1. SSRF 防護測試 (web.py)
# ==========================================
def test_read_url_content_ssrf_private_ip():
    """測試是否能成功阻擋指向私有 IP (如 127.0.0.1) 的請求。"""
    args = {"url": "http://127.0.0.1/admin/api"}
    result = call_web_tool("read_url_content", args)
    assert "[ERROR]" in result
    assert "安全限制：禁止存取內部或保留網段" in result


def test_read_url_content_ssrf_localhost():
    """測試是否能成功阻擋 localhost 網域的請求。"""
    args = {"url": "http://localhost:8080/api"}
    result = call_web_tool("read_url_content", args)
    assert "[ERROR]" in result
    assert "安全限制：禁止存取內部或保留網段" in result


def test_read_url_content_ssrf_allowlist_domain_with_intranet_ip():
    """測試受信任之白名單網域（如 raw.githubusercontent.com）即使解析為企業內網 IP 也予放行。"""
    fake_dns = [(2, 1, 6, "", ("10.1.5.219", 443))]
    fake_response = (200, {"content-type": "text/plain"}, b"Swagger Spec Content", False, False)

    with (
        patch("socket.getaddrinfo", return_value=fake_dns),
        patch("pocket_agent.tools.web._fetch_pinned", return_value=fake_response),
    ):
        result = call_web_tool(
            "read_url_content",
            {"url": "https://raw.githubusercontent.com/swagger-api/swagger-ui/master/README.md"},
        )
        assert "[ERROR]" not in result
        assert "Swagger Spec Content" in result


def test_read_url_content_ssrf_disallow_loopback_even_for_allowlist_domain():
    """測試即使是白名單網域，若解析為 Loopback (127.0.0.1) 依然嚴格阻斷。"""
    fake_dns = [(2, 1, 6, "", ("127.0.0.1", 443))]

    with patch("socket.getaddrinfo", return_value=fake_dns):
        result = call_web_tool(
            "read_url_content",
            {"url": "https://raw.githubusercontent.com/evil"},
        )
        assert "[ERROR]" in result
        assert "安全限制：禁止存取內部或保留網段" in result


# ==========================================
# 2. 目錄穿越與工作區監禁測試 (document.py)
# ==========================================
def test_call_document_tool_workspace_jail(tmp_path):
    """測試讀取外部 (工作區外) 的文件檔案是否會被拒絕。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    outside_file = tmp_path / "secret.pdf"
    outside_file.write_bytes(b"%PDF-1.4 dummy pdf")

    args = {"path": str(outside_file)}

    # 將工作區限定為 workspace，嘗試讀取 outside_file
    result = call_document_tool("read_document", args, workspace_dirs=[str(workspace)])

    assert "[ERROR]" in result
    assert "安全限制：路徑" in result
    assert "超出允許的工作區範圍" in result


# ==========================================
# 3. 敏感檔案與 DoS 預防測試 (fs.py)
# ==========================================
def test_fs_validate_safe_path_env_variant(tmp_path):
    """測試敏感檔案前綴比對是否生效 (如 .env.local)。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    target = workspace / ".env.local"

    with pytest.raises(ValueError, match="安全限制：禁止存取敏感目錄或檔案"):
        _validate_safe_path(str(target), workspace_dirs=[str(workspace)])


def test_fs_grep_search_dos_protection(tmp_path):
    """測試 grep_search 是否會自動略過大於 5MB 的檔案。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    large_file = workspace / "huge.log"
    # 建立一個小檔案，但透過 mock 假裝它很大
    large_file.write_text("This contains the secret_keyword but is too large.")

    with patch("pathlib.Path.stat") as mock_stat:
        mock_stat_result = MagicMock()
        mock_stat_result.st_size = 6 * 1024 * 1024  # 模擬 6 MB (大於 5MB 上限)
        mock_stat.return_value = mock_stat_result

        args = {"query": "secret_keyword", "path": str(workspace)}
        # 由於檔案過大，搜尋應自動跳過，所以找不到內容
        result = call_fs_tool("search_files", args, on_confirm=None, workspace_dirs=[str(workspace)])

        assert "找不到包含" in result


def test_fs_validate_safe_path_global_user_dir_allowed(tmp_path):
    """測試從專案工作區存取全域目錄 (~/.pocket-agent) 下的沙盒與產物能夠順利通過。"""
    project_dir = tmp_path / "my_project"
    project_dir.mkdir()
    fake_global = tmp_path / "home" / ".pocket-agent"
    scratch_file = fake_global / "workspaces" / "project-slug" / "scratch" / "outputs" / "out.log"
    scratch_file.parent.mkdir(parents=True)
    scratch_file.write_text("dummy output", encoding="utf-8")

    with (
        patch("pathlib.Path.cwd", return_value=project_dir),
        patch("pocket_agent.system.paths.user_dir", return_value=fake_global),
    ):
        resolved = _validate_safe_path(str(scratch_file))
        assert resolved == scratch_file.resolve()


def test_fs_validate_safe_path_global_user_dir_env_forbidden(tmp_path):
    """測試即便全域目錄已放行，存取全域目錄下的 .env 依然會被嚴格阻擋。"""
    project_dir = tmp_path / "my_project"
    project_dir.mkdir()
    fake_global = tmp_path / "home" / ".pocket-agent"
    fake_global.mkdir(parents=True)
    global_env = fake_global / ".env"
    global_env.write_text("SECRET=123", encoding="utf-8")

    with (
        patch("pathlib.Path.cwd", return_value=project_dir),
        patch("pocket_agent.system.paths.user_dir", return_value=fake_global),
    ):
        with pytest.raises(ValueError, match="安全限制：禁止存取敏感目錄或檔案"):
            _validate_safe_path(str(global_env))


def test_fs_validate_safe_path_unauthorized_outside_dir(tmp_path):
    """測試非工作區、非全域目錄的外部未授權目錄依然會觸發工作區監禁。"""
    project_dir = tmp_path / "my_project"
    project_dir.mkdir()
    fake_global = tmp_path / "home" / ".pocket-agent"
    fake_global.mkdir(parents=True)
    outside_file = tmp_path / "outside" / "secret.txt"
    outside_file.parent.mkdir(parents=True)
    outside_file.write_text("confidential", encoding="utf-8")

    with (
        patch("pathlib.Path.cwd", return_value=project_dir),
        patch("pocket_agent.system.paths.user_dir", return_value=fake_global),
    ):
        with pytest.raises(ValueError, match="超出允許的工作區範圍"):
            _validate_safe_path(str(outside_file))


def test_fs_search_files_in_global_user_dir(tmp_path):
    """測試在全域目錄 (~/.pocket-agent) 下搜尋檔案不會被靜默過濾。"""
    fake_global = tmp_path / "home" / ".pocket-agent"
    scratch_dir = fake_global / "workspaces" / "project-slug" / "scratch"
    scratch_dir.mkdir(parents=True)
    log_file = scratch_dir / "spillover.log"
    log_file.write_text("Hello Spillover Content", encoding="utf-8")

    with patch("pocket_agent.system.paths.user_dir", return_value=fake_global):
        result = call_fs_tool(
            "search_files",
            {"query": "Hello Spillover", "path": str(scratch_dir)},
            on_confirm=None,
        )
        assert "[ERROR]" not in result
        assert "spillover.log" in result


def test_fs_validate_safe_path_global_root_files_forbidden(tmp_path):
    """測試全域根目錄下的系統設定檔 (如 allowlist.json, mcp_config.yaml) 屬於私有保護範圍，禁止存取。"""
    project_dir = tmp_path / "my_project"
    project_dir.mkdir()
    fake_global = tmp_path / "home" / ".pocket-agent"
    fake_global.mkdir(parents=True)
    allowlist_file = fake_global / "allowlist.json"
    allowlist_file.write_text("{}", encoding="utf-8")

    with (
        patch("pathlib.Path.cwd", return_value=project_dir),
        patch("pocket_agent.system.paths.user_dir", return_value=fake_global),
    ):
        with pytest.raises(ValueError, match="安全限制：禁止存取敏感目錄或檔案"):
            _validate_safe_path(str(allowlist_file))


def test_fs_confirm_contract_blocks_custom_text_refusal_when_accept_edits_off(tmp_path):
    """確認 Accept-edits 關閉時，on_confirm 回傳文字拒絕不會修改或建立檔案。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    f = workspace / "code.py"
    f.write_text("x = 1\n", encoding="utf-8")

    out = call_fs_tool(
        "write_to_file",
        {"path": str(f), "content": "x = 999\n", "overwrite": True},
        on_confirm=lambda _desc: "先不要寫入，請說明原因",
        workspace_dirs=[str(workspace)],
    )
    assert out.startswith("[使用者拒絕執行此檔案寫入操作")
    assert "先不要寫入，請說明原因" in out
    assert f.read_text(encoding="utf-8") == "x = 1\n"
