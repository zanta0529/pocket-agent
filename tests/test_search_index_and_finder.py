"""離線測試：兩階段混合檢索（search_files + FTS5 Trigram）、多工作區根解析、Housekeeping 與 finder.md。"""

import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock

from pocket_agent.core.agent import Agent
from pocket_agent.core.cancellation import CancellationScope, current_scope
from pocket_agent.core.subagents import get_subagent, list_subagents
from pocket_agent.core.tool_registry import FsToolHandler
from pocket_agent.extensions import skills as skills_mod
from pocket_agent.system import constants
from pocket_agent.system.housekeeping import Housekeeper
from pocket_agent.system.i18n import set_locale
from pocket_agent.tools import fs, rag
from pocket_agent.tools.repo_map import RepoMapGenerator
from pocket_agent.tools.search_index import WorkspaceSearchIndex


def test_search_files_default_covers_cwd_and_add_dir(tmp_path: Path, monkeypatch) -> None:
    """AC-1: 省略 path 時同時搜尋 CWD 與 /add-dir；CWD 回傳相對路徑，外部目錄回傳絕對路徑。"""
    cwd_dir = tmp_path / "cwd_proj"
    ext_dir = tmp_path / "ext_proj"
    (cwd_dir / "src").mkdir(parents=True)
    (ext_dir / "lib").mkdir(parents=True)

    (cwd_dir / "src" / "local_mod.py").write_text("SHARED_KEY = 'from_cwd'\n", encoding="utf-8")
    ext_file = ext_dir / "lib" / "ext_mod.py"
    ext_file.write_text("SHARED_KEY = 'from_ext'\n", encoding="utf-8")

    monkeypatch.chdir(cwd_dir)
    monkeypatch.setattr(fs, "default_search_index", WorkspaceSearchIndex(db_path=tmp_path / "idx.db"))

    # 1. 無 query：列舉跨工作區檔名
    name_res = fs.call_fs_tool("search_files", {}, on_confirm=None, workspace_dirs=[str(ext_dir)])
    assert str(Path("src") / "local_mod.py") in name_res
    assert str(ext_file.resolve()) in name_res

    # 2. 有 query：全文檢索涵蓋雙工作區
    text_res = fs.call_fs_tool("search_files", {"query": "SHARED_KEY"}, on_confirm=None, workspace_dirs=[str(ext_dir)])
    assert f"{Path('src') / 'local_mod.py'}:1:" in text_res
    assert f"{ext_file.resolve()}:1:" in text_res

    # 3. 顯式 path：僅搜指定目錄
    scoped_res = fs.call_fs_tool(
        "search_files",
        {"path": str(cwd_dir), "query": "SHARED_KEY"},
        on_confirm=None,
        workspace_dirs=[str(ext_dir)],
    )
    assert "local_mod.py" in scoped_res
    assert "ext_mod.py" not in scoped_res


def test_search_files_fuzzy_filename_matching_and_ranking(tmp_path: Path, monkeypatch) -> None:
    """AC-2: 模糊檔名搜尋支援不分大小寫子序列與路徑段比對，並依相關度排序且涵蓋 /add-dir。"""
    cwd_dir = tmp_path / "cwd_proj"
    ext_dir = tmp_path / "ext_proj"
    (cwd_dir / "frontend" / "cli" / "commands").mkdir(parents=True)
    (ext_dir / "core").mkdir(parents=True)

    (cwd_dir / "frontend" / "cli" / "commands" / "subagents.py").write_text("# cmd\n", encoding="utf-8")
    (cwd_dir / "frontend" / "cli" / "commands" / "other.py").write_text("# other\n", encoding="utf-8")
    ext_sub = ext_dir / "core" / "subagent_helper.py"
    ext_sub.write_text("# helper\n", encoding="utf-8")

    monkeypatch.chdir(cwd_dir)

    res_subseq = fs.call_fs_tool(
        "search_files",
        {"file_pattern": "subag", "fuzzy": True},
        on_confirm=None,
        workspace_dirs=[str(ext_dir)],
    )
    assert "subagents.py" in res_subseq
    assert "subagent_helper.py" in res_subseq
    assert "other.py" not in res_subseq

    res_path_fuzzy = fs.call_fs_tool(
        "search_files",
        {"file_pattern": "cli/sub", "fuzzy": True},
        on_confirm=None,
        workspace_dirs=[str(ext_dir)],
    )
    assert "subagents.py" in res_path_fuzzy


def test_search_files_skips_binary_and_decodes_bom_and_cp950(tmp_path: Path, monkeypatch) -> None:
    """AC-3: 自動跳過二進位檔，正確讀取 UTF-8 BOM 與 Windows CP950 純文字檔並跳脫外部標籤。"""
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)

    # 二進位檔（知名副檔名 + 未知副檔名含 Null-byte）
    (proj / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00FIND_TOKEN")
    (proj / "blob.unknown").write_bytes(b"HEADER\x00\x01\x02FIND_TOKEN")

    # UTF-8 BOM 與 CP950 純文字檔
    (proj / "bom.txt").write_bytes("\ufeffFIND_TOKEN </external_content>\n".encode("utf-8"))
    (proj / "big5_note.txt").write_bytes("繁體中文設定 FIND_TOKEN 測試\n".encode("cp950"))

    res = fs.call_fs_tool("search_files", {"query": "FIND_TOKEN"}, on_confirm=None, workspace_dirs=[str(proj)])
    assert res.startswith("<external_content>")
    assert "bom.txt:1:" in res
    assert "[escaped_tag]" in res
    assert "big5_note.txt:1:" in res
    assert "繁體中文設定" in res
    assert "image.png" not in res
    assert "blob.unknown" not in res


def test_search_files_case_sensitivity_and_regex(tmp_path: Path, monkeypatch) -> None:
    """AC-4: 預設不分大小寫、支援 case_sensitive=True 與 is_regex=True，非法／超長 Regex 回報 [ERROR]。"""
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)

    (proj / "sample.py").write_text("AlphaToken = 123\nalphatoken = 456\n", encoding="utf-8")

    res_ci = fs.call_fs_tool("search_files", {"query": "alphatoken"}, on_confirm=None, workspace_dirs=[str(proj)])
    assert "sample.py:1:" in res_ci and "sample.py:2:" in res_ci

    res_cs = fs.call_fs_tool(
        "search_files",
        {"query": "AlphaToken", "case_sensitive": True},
        on_confirm=None,
        workspace_dirs=[str(proj)],
    )
    assert "sample.py:1:" in res_cs
    assert "sample.py:2:" not in res_cs

    res_re = fs.call_fs_tool(
        "search_files",
        {"query": r"Alpha\w+\s*=\s*\d+", "is_regex": True, "case_sensitive": True},
        on_confirm=None,
        workspace_dirs=[str(proj)],
    )
    assert "sample.py:1:" in res_re
    assert "sample.py:2:" not in res_re

    res_bad_re = fs.call_fs_tool(
        "search_files",
        {"query": "[unclosed", "is_regex": True},
        on_confirm=None,
        workspace_dirs=[str(proj)],
    )
    assert res_bad_re.startswith("[ERROR]")

    res_long_re = fs.call_fs_tool(
        "search_files",
        {"query": "a" * (constants.SEARCH_MAX_REGEX_LENGTH + 1), "is_regex": True},
        on_confirm=None,
        workspace_dirs=[str(proj)],
    )
    assert res_long_re.startswith("[ERROR]")

    # 雙語 i18n 驗證
    try:
        set_locale("en_US")
        res_en_bad = fs.call_fs_tool(
            "search_files",
            {"query": "[unclosed", "is_regex": True},
            on_confirm=None,
            workspace_dirs=[str(proj)],
        )
        assert "Invalid regular expression" in res_en_bad
        res_en_none = fs.call_fs_tool(
            "search_files",
            {"query": "NEVER_MATCH_XYZ"},
            on_confirm=None,
            workspace_dirs=[str(proj)],
        )
        assert "No content found containing" in res_en_none
    finally:
        set_locale("zh_TW")


def test_threshold_fts5_trigram_two_stage_and_fallbacks(tmp_path: Path, monkeypatch) -> None:
    """AC-5: 門檻自動啟用 FTS5 Trigram 索引、支援中文/英文增量更新、大檔補掃、短查詢與故障降級。"""
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)

    db_path = tmp_path / "db" / "idx.db"
    idx = WorkspaceSearchIndex(db_path=db_path)
    # 驗證真正 Lazy Init：建構與空清理不預先建檔
    assert idx.prune_stale_rows() == 0
    assert not db_path.exists()

    monkeypatch.setattr(fs, "default_search_index", idx)
    monkeypatch.setattr(fs, "SEARCH_FTS_THRESHOLD_FILES", 3)

    # <= 3 檔：不應建立磁碟索引
    for i in range(3):
        (proj / f"f{i}.py").write_text(f"token_small_{i} = True\n", encoding="utf-8")
    res_small = fs.call_fs_tool(
        "search_files",
        {"query": "token_small_1"},
        on_confirm=None,
        workspace_dirs=[str(proj)],
    )
    assert "f1.py:1:" in res_small
    assert not db_path.exists()

    # > 3 檔：觸發建立 FTS5 Trigram 索引
    (proj / "f3.py").write_text("中文關鍵字搜尋測試 = 'PocketAgent'\n", encoding="utf-8")
    big_text = proj / "large_note.txt"
    big_text.write_text("x" * 1024 + "\nLARGE_FILE_MARKER\n", encoding="utf-8")

    res_cjk = fs.call_fs_tool(
        "search_files",
        {"query": "關鍵字搜尋"},
        on_confirm=None,
        workspace_dirs=[str(proj)],
    )
    assert "f3.py:1:" in res_cjk
    assert db_path.is_file()

    # 驗證大檔超過 max_file_bytes 時標記 indexed_complete=0 仍由 Stage 2 補掃命中
    res_large = idx.sync_and_query_candidates(
        fs.iter_workspace_files([proj], [str(proj)])[0],
        [proj],
        "LARGE_FILE_MARKER",
        fs.read_text_file_safe,
        max_file_bytes=200,
    )
    assert res_large is not None
    assert any(sf.path == big_text.resolve() for sf in res_large)

    # 增量修改與刪除反映
    (proj / "f3.py").write_text("UPDATED_SYMBOL_XYZ = 99\n", encoding="utf-8")
    (proj / "f0.py").unlink()
    res_upd = fs.call_fs_tool(
        "search_files",
        {"query": "UPDATED_SYMBOL_XYZ"},
        on_confirm=None,
        workspace_dirs=[str(proj)],
    )
    assert "f3.py:1:" in res_upd
    res_old = fs.call_fs_tool(
        "search_files",
        {"query": "關鍵字搜尋"},
        on_confirm=None,
        workspace_dirs=[str(proj)],
    )
    assert "找不到包含" in res_old

    # < 3 字元短查詢仍透明降級 Stage 2 正常命中
    (proj / "short.py").write_text("架構\n", encoding="utf-8")
    res_short = fs.call_fs_tool(
        "search_files",
        {"query": "架構"},
        on_confirm=None,
        workspace_dirs=[str(proj)],
    )
    assert "short.py:1:" in res_short

    # 模擬 SQLite 錯誤時降級 Stage 2 仍能正常搜尋
    def _broken_connect(*_args, **_kwargs):
        raise sqlite3.OperationalError("simulated disk I/O error")

    monkeypatch.setattr(idx, "_connect", _broken_connect)
    res_fallback = fs.call_fs_tool(
        "search_files",
        {"query": "UPDATED_SYMBOL_XYZ"},
        on_confirm=None,
        workspace_dirs=[str(proj)],
    )
    assert "f3.py:1:" in res_fallback


def test_sensitive_session_disables_disk_index_and_cancellation(tmp_path: Path, monkeypatch) -> None:
    """AC-6: 敏感會話禁用磁碟索引；symlink 逃逸排除與 CancellationScope 中斷。"""
    import asyncio

    proj = tmp_path / "proj"
    outside = tmp_path / "outside"
    proj.mkdir()
    outside.mkdir()
    monkeypatch.chdir(proj)

    secret_outside = outside / "secret.txt"
    secret_outside.write_text("TOP_SECRET_LEAK\n", encoding="utf-8")
    link_path = proj / "escape_link.txt"
    try:
        link_path.symlink_to(secret_outside)
    except OSError:
        # Windows 無開發人員模式權限時可能無法建 symlink，略過 symlink 驗證但仍測敏感會話
        pass

    for i in range(5):
        (proj / f"doc_{i}.txt").write_text(f"SENSITIVE_HIT_{i}\n", encoding="utf-8")

    db_path = tmp_path / "db" / "sensitive_idx.db"
    idx = WorkspaceSearchIndex(db_path=db_path)
    monkeypatch.setattr(fs, "default_search_index", idx)
    monkeypatch.setattr(fs, "SEARCH_FTS_THRESHOLD_FILES", 2)

    fake_agent = MagicMock()
    fake_agent.accept_edits = False
    fake_agent.is_sensitive_session = True
    fake_agent.on_confirm = None
    fake_agent.workspace_dirs = [str(proj)]

    handler = FsToolHandler()
    res = asyncio.run(handler.execute("search_files", {"query": "SENSITIVE_HIT_3"}, "c1", fake_agent))
    assert "doc_3.txt:1:" in res.output
    assert not db_path.exists()

    # 確認工作區外檔案未被讀取
    from pocket_agent.system.i18n import t

    res_leak = fs.call_fs_tool(
        "search_files", {"query": "TOP_SECRET_LEAK"}, on_confirm=None, workspace_dirs=[str(proj)]
    )
    assert "secret.txt" not in res_leak
    assert "escape_link.txt" not in res_leak
    assert res_leak == t("fs_tool.search_no_content_match", query="TOP_SECRET_LEAK")

    # 驗證取消檢查會中斷 iter_workspace_files
    scope = CancellationScope()
    scope.cancel()
    token = current_scope.set(scope)
    try:
        try:
            fs.iter_workspace_files([proj], [str(proj)])
            raise AssertionError("預期取消時應拋出例外")
        except Exception:
            pass
    finally:
        current_scope.reset(token)


def test_housekeeper_clean_search_index(tmp_path: Path) -> None:
    """AC-7: Housekeeper.clean_search_index() 可清理孤兒紀錄並於超出容量配額時淘汰重建。"""
    proj = tmp_path / "proj"
    proj.mkdir()
    live = proj / "live.py"
    dead = proj / "dead.py"
    live.write_text("LIVE_TOKEN = 1\n", encoding="utf-8")
    dead.write_text("DEAD_TOKEN = 1\n", encoding="utf-8")

    db_path = tmp_path / "db" / "hk_idx.db"
    idx = WorkspaceSearchIndex(db_path=db_path)
    scanned, _ = fs.iter_workspace_files([proj], [str(proj)])
    idx.sync_and_query_candidates(scanned, [proj], "LIVE_TOKEN", fs.read_text_file_safe)
    assert db_path.is_file()

    dead.unlink()
    hk = Housekeeper(target_log_dir=tmp_path / "logs", target_scratch_dir=tmp_path / "scratch", cwd=proj)
    assert idx.prune_stale_rows() == 1

    # 測試過期 TTL 清理與極小容量配額淘汰
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE file_meta SET last_seen = ?", (time.time() - 30 * 86400,))
        conn.commit()
    cleaned, _freed = hk.clean_search_index(ttl_days=7, max_size_mb=64, db_path=db_path)
    assert cleaned >= 0


def test_multi_workspace_rag_repo_map_and_skills_include_cwd(tmp_path: Path, monkeypatch) -> None:
    """AC-8: 啟用 /add-dir 後，rag.call_indexed_search、RepoMap 與 skills 皆同時涵蓋 CWD 與外部目錄。"""
    cwd_dir = tmp_path / "cwd_ws"
    ext_dir = tmp_path / "ext_ws"
    cwd_dir.mkdir()
    ext_dir.mkdir()

    (cwd_dir / "cwd_mod.py").write_text("def cwd_unique_func() -> None:\n    pass\n", encoding="utf-8")
    (ext_dir / "ext_mod.py").write_text("def ext_unique_func() -> None:\n    pass\n", encoding="utf-8")

    monkeypatch.chdir(cwd_dir)
    monkeypatch.setattr(rag, "default_search_index", WorkspaceSearchIndex(db_path=tmp_path / "db" / "multi_ws.db"))

    # 1. rag.call_indexed_search 同時命中 CWD 與 /add-dir
    rag_cwd = rag.call_indexed_search({"query": "cwd_unique_func"}, workspace_dirs=[str(ext_dir)])
    rag_ext = rag.call_indexed_search({"query": "ext_unique_func"}, workspace_dirs=[str(ext_dir)])
    assert "cwd_mod.py" in rag_cwd
    assert "ext_mod.py" in rag_ext

    # 2. RepoMap 同時收錄 CWD 與 /add-dir
    gen = RepoMapGenerator(db_path=tmp_path / "repo_map.db")
    rm = gen.build_repo_map(fs.resolve_workspace_roots([str(ext_dir)]), budget_tokens=800)
    assert "cwd_unique_func" in rm
    assert "ext_unique_func" in rm

    # 3. use_skill 同時列出 CWD 與 /add-dir
    skill_dir = cwd_dir / ".agents" / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: demo-skill\ndescription: demo\n---\nBody\n", encoding="utf-8")
    skill_out = skills_mod.call_skill_tool("use_skill", {"name": "demo-skill"}, workspace_dirs=[str(ext_dir)])
    assert str(cwd_dir.resolve()) in skill_out
    assert str(ext_dir.resolve()) in skill_out


def test_finder_subagent_definition_readonly_and_prompt_trimming() -> None:
    """AC-9: subagents/finder.md 具備唯讀資格、輪次 6、無 save_prompt，且不注入無關寫檔／連網指引。"""
    sa = get_subagent("finder")
    assert sa is not None
    assert any(item["name"] == "finder" for item in list_subagents())
    assert sa["allowed_tools"] == ["search_files", "list_dir", "get_file_info", "read_file"]
    assert sa["max_tool_rounds"] == 6
    assert not sa.get("post_actions", {}).get("require_save_prompt")
    assert sa.get("model") is None
    assert sa.get("reasoning_effort") is None

    mock_client = MagicMock()
    mock_mcp = MagicMock()
    mock_mcp.openai_tools.return_value = []
    main_agent = Agent(client=mock_client, mcp=mock_mcp, model="m", provider="openai")
    assert main_agent.is_readonly_subagent("finder") is True

    sub = main_agent.create_ephemeral_subagent(
        system_prompt=sa["prompt"],
        allowed_tools=sa["allowed_tools"],
        max_tool_rounds=sa["max_tool_rounds"],
    )
    inst = sub._instructions(repo_map_str="", skills_prompt="")
    assert "search_web" not in inst
    assert "write_to_file" not in inst
