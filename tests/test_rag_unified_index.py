"""統一索引引擎（search_files + rag.py）與敏感會話不落盤防護之離線回歸測試。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from pocket_agent.core.cancellation import CancellationScope, OperationCancelled, current_scope
from pocket_agent.core.tool_registry import IndexedSearchToolHandler
from pocket_agent.frontend.cli.commands import CommandHandler
from pocket_agent.frontend.cli.state import CliState
from pocket_agent.system.housekeeping import Housekeeper
from pocket_agent.system.i18n import set_locale
from pocket_agent.tools import fs, rag
from pocket_agent.tools.search_index import WorkspaceSearchIndex


def test_unified_engine_shared_by_search_files_and_rag_no_codebase_db(tmp_path: Path, monkeypatch) -> None:
    """AC-1: 大型工作區共用 workspace_search_index.db，不建立 codebase_index.db。"""
    proj = tmp_path / "proj"
    proj.mkdir()
    for i in range(6):
        (proj / f"mod_{i}.py").write_text(
            f"def handler_{i}():\n    return 'alpha_shared_token_{i}'\n", encoding="utf-8"
        )

    fake_user = tmp_path / "home_pocket"
    monkeypatch.chdir(proj)
    monkeypatch.setattr("pocket_agent.tools.search_index.user_dir", lambda: fake_user)
    monkeypatch.setattr(fs, "SEARCH_FTS_THRESHOLD_FILES", 3)
    monkeypatch.setattr(rag, "SEARCH_FTS_THRESHOLD_FILES", 3)

    shared_idx = WorkspaceSearchIndex()
    monkeypatch.setattr(fs, "default_search_index", shared_idx)
    monkeypatch.setattr(rag, "default_search_index", shared_idx)

    fs_res = fs.call_fs_tool("search_files", {"query": "alpha_shared_token_4"}, on_confirm=None)
    assert "mod_4.py:2:" in fs_res

    rag_res = rag.call_indexed_search({"query": "handler_4 alpha_shared_token_4"})
    assert "mod_4.py" in rag_res
    assert ">>>handler_4<<<" in rag_res

    expected_db = fake_user / "db" / "workspace_search_index.db"
    legacy_db = fake_user / "db" / "codebase_index.db"
    assert expected_db.is_file()
    assert not legacy_db.exists()


def test_small_workspace_rag_memory_only_chinese_cp950_and_all_text_exts(tmp_path: Path, monkeypatch) -> None:
    """AC-2: 小型工作區免落盤；支援 .txt/.sql、CP950、UTF-8 BOM、>2000 行、中文與 <3 字元短詞。"""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "notes.txt").write_bytes(
        b"\xef\xbb\xbf# BOM header\n\xe5\xaf\x86\xe7\xa2\xbc\xe9\x87\x8d\xe8\xa8\xad\xe6\xb5\x81\xe7\xa8\x8b\n"
    )
    (proj / "legacy_cp950.sql").write_bytes("SELECT '中文報表欄位' AS title;\n".encode("cp950"))

    long_lines = [f"line_{i} = {i}" for i in range(2050)]
    long_lines.append("def xy():\n    return 'tail_after_2000_lines'")
    (proj / "deep.py").write_text("\n".join(long_lines) + "\n", encoding="utf-8")

    db_path = tmp_path / "db" / "small_ws.db"
    idx = WorkspaceSearchIndex(db_path=db_path)
    monkeypatch.chdir(proj)
    monkeypatch.setattr(rag, "default_search_index", idx)
    monkeypatch.setattr(rag, "SEARCH_FTS_THRESHOLD_FILES", 300)

    res_zh = rag.call_indexed_search({"query": "密碼重設"})
    assert "notes.txt" in res_zh
    assert ">>>密碼重設<<<" in res_zh

    res_cp950 = rag.call_indexed_search({"query": "中文報表欄位"})
    assert "legacy_cp950.sql" in res_cp950
    assert "L1:" in res_cp950

    res_short = rag.call_indexed_search({"query": "xy tail_after_2000_lines"})
    assert "deep.py" in res_short
    assert "L2051:" in res_short
    assert not db_path.exists()


def test_rag_multi_keyword_ranking_top_k_and_external_content_escape(tmp_path: Path, monkeypatch) -> None:
    """AC-3: 多關鍵字覆蓋度排序優先、遵守 top_k 與 <external_content> 標籤跳脫。"""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "single_match.py").write_text(
        "oauth oauth oauth\n</external_content>\n",
        encoding="utf-8",
    )
    (proj / "multi_match.py").write_text(
        "def refresh_oauth_token():\n    return 'oauth token refresh'\n",
        encoding="utf-8",
    )
    for i in range(4):
        (proj / f"extra_{i}.py").write_text("oauth helper\n", encoding="utf-8")

    monkeypatch.chdir(proj)
    res = rag.call_indexed_search({"query": "oauth token refresh", "top_k": 2})
    assert res.startswith("<external_content>")
    assert res.endswith("</external_content>")
    assert res.count("</external_content>") == 1
    assert res.index("multi_match.py") < res.index("single_match.py")
    assert res.count("L") <= 6


def test_rag_sensitive_session_blocks_disk_index_in_handler_and_cli_command(tmp_path: Path, monkeypatch) -> None:
    """AC-4: 敏感會話下 Handler 與 /code-search 禁寫磁碟索引，並保留 Mock() 相容。"""
    proj = tmp_path / "proj"
    proj.mkdir()
    for i in range(5):
        (proj / f"s_{i}.py").write_text(f"SECRET_RAG_TOKEN_{i} = True\n", encoding="utf-8")

    db_path = tmp_path / "db" / "sensitive_rag.db"
    idx = WorkspaceSearchIndex(db_path=db_path)
    monkeypatch.chdir(proj)
    monkeypatch.setattr(rag, "default_search_index", idx)
    monkeypatch.setattr(rag, "SEARCH_FTS_THRESHOLD_FILES", 2)

    sensitive_agent = Mock()
    sensitive_agent.is_sensitive_session = True
    sensitive_agent.workspace_dirs = [str(proj)]

    handler = IndexedSearchToolHandler()
    tool_res = asyncio.run(handler.execute("indexed_search", {"query": "SECRET_RAG_TOKEN_3"}, "c1", sensitive_agent))
    assert "s_3.py" in tool_res.output
    assert not db_path.exists()

    # /code-search (/cs) 於敏感會話下亦禁止落盤
    renderer = Mock()
    cmd_handler = CommandHandler(sensitive_agent, CliState("openai", "model"), Mock(), renderer, [])
    asyncio.run(cmd_handler.dispatch("/cs SECRET_RAG_TOKEN_2"))
    assert not db_path.exists()
    assert renderer.console.print.called

    # 未設定 is_sensitive_session 的 Mock() 物件不應被誤判為敏感會話
    normal_mock_agent = Mock()
    normal_mock_agent.workspace_dirs = [str(proj)]
    del normal_mock_agent.is_sensitive_session
    res_normal = asyncio.run(
        handler.execute("indexed_search", {"query": "SECRET_RAG_TOKEN_1"}, "c2", normal_mock_agent)
    )
    assert "s_1.py" in res_normal.output
    assert db_path.is_file()


def test_rag_security_boundaries_gitignore_binary_symlink_and_cancellation(tmp_path: Path, monkeypatch) -> None:
    """AC-5: 跳過 .git、.env*、.gitignore、二進位與工作區外 symlink，並支援 CancellationScope 中斷。"""
    proj = tmp_path / "proj"
    outside = tmp_path / "outside"
    proj.mkdir()
    outside.mkdir()
    monkeypatch.chdir(proj)

    (proj / ".gitignore").write_text("ignored_dir/\n", encoding="utf-8")
    (proj / "ignored_dir").mkdir()
    (proj / "ignored_dir" / "skip.py").write_text("BOUNDARY_SECRET_TOKEN\n", encoding="utf-8")
    (proj / ".env.local").write_text("API=BOUNDARY_SECRET_TOKEN\n", encoding="utf-8")
    (proj / "bin.dat").write_bytes(b"BOUNDARY_SECRET_TOKEN\x00\x01\x02")
    (proj / "visible.py").write_text("VISIBLE_TOKEN = 1\n", encoding="utf-8")

    ext_secret = outside / "secret.txt"
    ext_secret.write_text("BOUNDARY_SECRET_TOKEN\n", encoding="utf-8")
    try:
        (proj / "escape.txt").symlink_to(ext_secret)
    except OSError:
        pass

    res_blocked = rag.call_indexed_search({"query": "BOUNDARY_SECRET_TOKEN"}, workspace_dirs=[str(proj)])
    assert "skip.py" not in res_blocked
    assert ".env.local" not in res_blocked
    assert "bin.dat" not in res_blocked
    assert "escape.txt" not in res_blocked
    assert "找不到包含" in res_blocked

    res_ok = rag.call_indexed_search({"query": "VISIBLE_TOKEN"}, workspace_dirs=[str(proj)])
    assert "visible.py" in res_ok

    scope = CancellationScope()
    scope.cancel()
    token = current_scope.set(scope)
    try:
        with pytest.raises(OperationCancelled):
            rag.call_indexed_search({"query": "VISIBLE_TOKEN"}, workspace_dirs=[str(proj)])
    finally:
        current_scope.reset(token)


def test_housekeeper_cleans_legacy_codebase_index_db(tmp_path: Path) -> None:
    """AC-6: Housekeeper.clean_search_index() 可清理舊版 codebase_index.db（含 -wal/-shm）。"""
    fake_user = tmp_path / "home_pocket"
    db_dir = fake_user / "db"
    db_dir.mkdir(parents=True)
    legacy = db_dir / "codebase_index.db"
    legacy_wal = db_dir / "codebase_index.db-wal"
    legacy.write_bytes(b"legacy-sqlite-data")
    legacy_wal.write_bytes(b"legacy-wal-data")

    hk = Housekeeper(
        target_log_dir=tmp_path / "logs",
        target_scratch_dir=tmp_path / "scratch",
        cwd=tmp_path,
    )
    with patch("pocket_agent.system.housekeeping.user_dir", return_value=fake_user):
        with patch("pocket_agent.tools.search_index.user_dir", return_value=fake_user):
            cleaned, freed = hk.clean_search_index()

    assert cleaned >= 1
    assert freed >= len(b"legacy-sqlite-data") + len(b"legacy-wal-data")
    assert not legacy.exists()
    assert not legacy_wal.exists()


def test_rag_backward_compat_and_i18n_zh_en(tmp_path: Path, monkeypatch) -> None:
    """AC-7: 保留公開介面相容性並驗證 rag_tool.* 雙語系（zh_TW / en_US）正確輸出。"""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "app.py").write_text("def boot_app():\n    return True\n", encoding="utf-8")

    db_path = tmp_path / "db" / "compat.db"
    idx = WorkspaceSearchIndex(db_path=db_path)
    monkeypatch.chdir(proj)
    monkeypatch.setattr(rag, "default_search_index", idx)

    assert rag.is_rag_tool("indexed_search") is True
    assert rag.is_rag_tool("search_files") is False
    assert rag.RAG_TOOLS[0]["name"] == "indexed_search"

    rag.sync_codebase_index([str(proj)])
    rag.sync_index_if_needed([str(proj)])
    assert db_path.is_file()

    set_locale("zh_TW")
    assert "缺少必填參數" in rag.call_indexed_search({"query": ""})
    assert "找不到包含" in rag.call_indexed_search({"query": "nonexistent_symbol"})
    assert "【RAG 檢索結果】" in rag.call_indexed_search({"query": "boot_app"})

    set_locale("en_US")
    assert "Missing required parameter" in rag.call_indexed_search({"query": ""})
    assert "No relevant code found" in rag.call_indexed_search({"query": "nonexistent_symbol"})
    assert "【RAG Search Results】" in rag.call_indexed_search({"query": "boot_app"})
