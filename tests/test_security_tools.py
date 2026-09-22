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
        result = call_fs_tool("grep_search", args, on_confirm=None, workspace_dirs=[str(workspace)])

        assert "找不到包含" in result
