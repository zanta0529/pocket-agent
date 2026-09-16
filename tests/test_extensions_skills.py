from unittest.mock import patch

import pytest

from pocket_agent.extensions.skills import search_skills, skills_index_prompt, sync_index


@pytest.fixture
def mock_user_dir(tmp_path):
    """攔截 user_dir() 呼叫，確保測試建立的 db 在 pytest 的暫存目錄下"""
    with patch("pocket_agent.extensions.skills.user_dir", return_value=tmp_path):
        yield tmp_path


@pytest.fixture
def mock_skills():
    """提供一組固定的測試技能清單，以便驗證 BM25 檢索邏輯"""
    with (
        patch("pocket_agent.extensions.skills.list_skills") as m_list,
        patch("pocket_agent.extensions.skills.load_skill") as m_load,
    ):
        from pathlib import Path
        from unittest.mock import MagicMock

        fake_paths = []
        for name in ["python-async", "git-commit", "db-schema"]:
            p = MagicMock(spec=Path)
            p.__str__.return_value = f"/fake/{name}/SKILL.md"
            p.exists.return_value = True

            stat_mock = MagicMock()
            stat_mock.st_mtime = 1000.0
            p.stat.return_value = stat_mock
            fake_paths.append(p)

        m_list.return_value = [
            {"name": "python-async", "description": "Python asyncio 異步教學", "path": fake_paths[0]},
            {"name": "git-commit", "description": "專案 Git 提交規範", "path": fake_paths[1]},
            {"name": "db-schema", "description": "資料庫建表與 SQLite 範例", "path": fake_paths[2]},
        ]

        def mock_load(name, dirs):
            contents = {
                "python-async": "async await asyncio gather event loop",
                "git-commit": "feat fix docs style refactor commit msg",
                "db-schema": "create table insert select foreign key sqlite",
            }
            return contents.get(name, "")

        m_load.side_effect = mock_load
        yield m_list.return_value


def test_sync_index_creates_db(mock_user_dir, mock_skills):
    """測試同步函式能正確建立 SQLite 資料庫檔案"""
    sync_index()
    db_path = mock_user_dir / "db" / "skills_index.db"
    assert db_path.exists()
    assert db_path.stat().st_size > 0


def test_search_skills_exact_match(mock_user_dir, mock_skills):
    """測試 BM25 是否能精準命中內文關鍵字"""
    results = search_skills("請教我如何使用 asyncio", top_k=1)
    assert len(results) == 1
    assert results[0]["name"] == "python-async"

    results = search_skills("幫我寫一份 commit", top_k=1)
    assert len(results) == 1
    assert results[0]["name"] == "git-commit"


def test_search_skills_fallback_on_empty(mock_user_dir, mock_skills):
    """測試空字串查詢時，應回退回傳前 top_k 個技能"""
    results = search_skills("   ", top_k=2)
    assert len(results) == 2
    assert results[0]["name"] == "python-async"
    assert results[1]["name"] == "git-commit"


def test_search_skills_fallback_on_unmatched(mock_user_dir, mock_skills):
    """測試完全無關的關鍵字（或純特殊符號被濾除後）的回退機制"""
    # 測試特殊符號，正規表示式濾除後會變成空查詢
    results = search_skills("!@#$%^&*", top_k=3)
    assert len(results) == 3

    # 測試完全無關字眼，SQLite FTS5 MATCH 可能查無結果，應回傳備用資料
    results = search_skills("kubernetes deployment", top_k=1)
    # 此處依實作而定，查無結果時目前邏輯是直接返回原始 list_skills() 的 top_k
    assert len(results) == 1


def test_skills_index_prompt_with_query(mock_user_dir, mock_skills):
    """測試 Prompt 生成是否能正確包含檢索結果"""
    prompt = skills_index_prompt(None, query="教我用 sqlite 建表")

    assert "db-schema" in prompt
    assert "資料庫建表與 SQLite 範例" in prompt
    # 不應該出現完全不相關的 python-async (除非 top_k 大於命中數而混入)
    # 預設 top_k 是 5，我們 mock 有 3 個，如果完全沒 hit 會全回傳。
    # 但這裡 "sqlite" 會明確 hit db-schema，只會回傳 db-schema。
    assert "python-async" not in prompt
