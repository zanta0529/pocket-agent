import json
from unittest.mock import patch

import pytest

from pocket_agent.system.permissions import add_command_to_allowlist, is_command_allowed


@pytest.fixture
def mock_allowlist_path(tmp_path):
    """攔截 ALLOWLIST_PATH，讓測試環境寫入 pytest 的暫存目錄"""
    fake_path = tmp_path / "allowlist.json"
    with patch("pocket_agent.system.permissions.ALLOWLIST_PATH", fake_path):
        yield fake_path


def test_builtin_allowlist(mock_allowlist_path):
    """測試內建的純唯讀指令，不需要 allowlist.json 也能通過"""
    assert is_command_allowed("git rev-parse --show-toplevel") is True
    assert is_command_allowed("GIT STATUS") is True  # 測試大小寫不敏感

    # ls, pwd, echo 已經從內建白名單中移除
    assert is_command_allowed("ls -la") is False
    assert is_command_allowed("pwd") is False
    assert is_command_allowed("echo 'hello world'") is False

    # 危險或不在清單的指令應被拒絕
    assert is_command_allowed("rm -rf /") is False
    assert is_command_allowed("npm install") is False
    assert is_command_allowed("python script.py") is False


def test_add_to_allowlist_and_check(mock_allowlist_path):
    """測試使用者按下 A 後，指令前綴寫入 allowlist.json 並能生效"""
    # 初始狀態：不被允許
    assert is_command_allowed("npm run dev") is False

    # 加入白名單
    prefix = add_command_to_allowlist("npm run dev")
    assert prefix == "npm run dev"  # 新邏輯預期為完整指令

    # 確認檔案已正確寫入
    assert mock_allowlist_path.exists()
    data = json.loads(mock_allowlist_path.read_text(encoding="utf-8"))
    assert "npm run dev" in data["allowed_prefixes"]

    # 再次檢查：現在應該允許了
    assert is_command_allowed("npm run dev") is True
    assert is_command_allowed("npm run build") is False  # 嚴格比對，不同指令應攔截
    assert is_command_allowed("npm install") is False  # 不同前綴應攔截


def test_add_single_word_command(mock_allowlist_path):
    """測試單字指令加入白名單的行為"""
    assert is_command_allowed("docker") is False

    prefix = add_command_to_allowlist("docker")
    assert prefix == "docker"

    assert is_command_allowed("docker ps") is False  # 嚴格比對，所以為 False
    assert is_command_allowed("docker") is True

    # 新版白名單以整詞比對，所以 docker 不會再錯誤匹配到 docker-compose
    assert is_command_allowed("docker-compose") is False


def test_add_duplicate_prevention(mock_allowlist_path):
    """測試重複加入相同的指令，不會在 json 中產生重複元素"""
    add_command_to_allowlist("poetry run")
    add_command_to_allowlist("poetry run test")

    data = json.loads(mock_allowlist_path.read_text(encoding="utf-8"))
    prefixes = data["allowed_prefixes"]

    # 現已存入完整指令，故兩個指令皆會成功存入，總長度為 2
    assert "poetry run" in prefixes
    assert "poetry run test" in prefixes
    assert len(prefixes) == 2


def test_allowlist_preserves_argument_boundaries_and_fails_closed(mock_allowlist_path):
    """驗證引號單一參數與拆開參數不共用白名單，且分詞失敗時 fail-closed。"""
    quoted_cmd = 'python tool.py "--delete target"'
    split_cmd = "python tool.py --delete target"

    assert add_command_to_allowlist(quoted_cmd) == quoted_cmd
    assert is_command_allowed(quoted_cmd) is True
    # 拆開引號後的選項與參數絕不可通過白名單
    assert is_command_allowed(split_cmd) is False
    # 內部空白差異與大小寫差異亦不混同
    assert is_command_allowed('python tool.py "--delete  target"') is False
    assert is_command_allowed('Python tool.py "--delete target"') is False

    # Windows 路徑反斜線保留測試
    win_cmd = r'venv\Scripts\python.exe -m pytest "tests\test_fs_tool.py"'
    assert add_command_to_allowlist(win_cmd) == win_cmd
    assert is_command_allowed(win_cmd) is True
    assert is_command_allowed(r"venv\Scripts\python.exe -m pytest tests\test_fs_tool.py") is False

    # 未閉合引號或空白命令必須 fail-closed 拒絕
    assert add_command_to_allowlist('python tool.py "--unclosed') == ""
    assert is_command_allowed('python tool.py "--unclosed') is False
    assert add_command_to_allowlist("   ") == ""
    assert is_command_allowed("   ") is False
