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
    assert is_command_allowed("ls -la") is True
    assert is_command_allowed("pwd") is True
    assert is_command_allowed("echo 'hello world'") is True

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
    assert prefix == "npm run"  # 預期只截取前兩個詞

    # 確認檔案已正確寫入
    assert mock_allowlist_path.exists()
    data = json.loads(mock_allowlist_path.read_text(encoding="utf-8"))
    assert "npm run" in data["allowed_prefixes"]

    # 再次檢查：現在應該允許了
    assert is_command_allowed("npm run dev") is True
    assert is_command_allowed("npm run build") is True  # 同前綴也應允許
    assert is_command_allowed("npm install") is False  # 不同前綴應攔截


def test_add_single_word_command(mock_allowlist_path):
    """測試單字指令加入白名單的行為"""
    assert is_command_allowed("docker") is False

    prefix = add_command_to_allowlist("docker")
    assert prefix == "docker"

    assert is_command_allowed("docker ps") is True

    # 注意：字串 startswith("docker") 的確會包含 docker-compose。
    # 這在我們目前前綴比對的實作中是被允許的。
    assert is_command_allowed("docker-compose") is True


def test_add_duplicate_prevention(mock_allowlist_path):
    """測試重複加入相同的指令，不會在 json 中產生重複元素"""
    add_command_to_allowlist("poetry run")
    add_command_to_allowlist("poetry run test")

    data = json.loads(mock_allowlist_path.read_text(encoding="utf-8"))
    prefixes = data["allowed_prefixes"]

    # 由於 "poetry run" 已經加入，第二次呼叫擷取到相同的 "poetry run"，應該不重複
    assert prefixes.count("poetry run") == 1
