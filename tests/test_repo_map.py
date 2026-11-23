"""RepoMapGenerator 專案結構地圖單元測試。"""

import sqlite3

from pocket_agent.tools.repo_map import RepoMapGenerator, extract_file_skeleton


def test_extract_file_skeleton(tmp_path):
    code = """
class MyService(BaseService):
    '''Service description.'''
    def __init__(self, port: int = 8080) -> None:
        self.port = port
        print("initializing")

    async def run_job(self, task_id: str) -> bool:
        if task_id:
            return True
        return False

def standalone_helper(x: int, y: int) -> int:
    return x + y
"""
    py_file = tmp_path / "sample.py"
    py_file.write_text(code, encoding="utf-8")

    skeleton = extract_file_skeleton(py_file)
    assert "class MyService(BaseService):" in skeleton
    assert "def __init__(self, port: int=8080) -> None: ..." in skeleton
    assert "async def run_job(self, task_id: str) -> bool: ..." in skeleton
    assert "def standalone_helper(x: int, y: int) -> int: ..." in skeleton
    # 驗證實作細節被省略
    assert "self.port = port" not in skeleton
    assert "initializing" not in skeleton


def test_repo_map_generator_caching_and_budget(tmp_path):
    db_file = tmp_path / "test_cache.db"
    generator = RepoMapGenerator(db_path=db_file)

    p1 = tmp_path / "mod_a.py"
    p1.write_text("class Alpha:\n    def do_a(self): pass\n", encoding="utf-8")
    p2 = tmp_path / "mod_b.py"
    p2.write_text("class Beta:\n    def do_b(self): pass\n", encoding="utf-8")

    # 首次建置
    res1 = generator.build_repo_map([tmp_path], budget_tokens=500)
    assert "mod_a.py" in res1
    assert "class Alpha:" in res1
    assert "mod_b.py" in res1
    assert "class Beta:" in res1

    # 驗證第二次直接讀取快取（mtime 一致）
    res2 = generator.build_repo_map([tmp_path], budget_tokens=500)
    assert res1 == res2

    # 驗證嚴格預算控制截斷
    tight_res = generator.build_repo_map([tmp_path], budget_tokens=30)
    # 預算極小時可能只放下開頭或截斷第二個檔案
    assert len(tight_res) < len(res1)


def test_repo_map_prune_stale_entries(tmp_path):
    db_file = tmp_path / "test_prune.db"
    generator = RepoMapGenerator(db_path=db_file)

    live_file = tmp_path / "live.py"
    live_file.write_text("def keep_me(): pass\n", encoding="utf-8")
    dead_file = tmp_path / "dead.py"
    dead_file.write_text("def remove_me(): pass\n", encoding="utf-8")

    generator.build_repo_map([tmp_path], budget_tokens=500)
    dead_file.unlink()

    removed = generator.prune_stale_entries(vacuum=True)
    assert removed == 1

    with sqlite3.connect(db_file) as conn:
        paths = [r[0] for r in conn.execute("SELECT path FROM file_ast_cache").fetchall()]
    assert str(live_file.resolve()) in paths
    assert str(dead_file.resolve()) not in paths


def test_repo_map_build_auto_prunes_stale_entries(tmp_path):
    db_file = tmp_path / "test_auto_prune.db"
    generator = RepoMapGenerator(db_path=db_file)

    live_file = tmp_path / "live.py"
    live_file.write_text("def keep_me(): pass\n", encoding="utf-8")
    dead_file = tmp_path / "dead.py"
    dead_file.write_text("def remove_me(): pass\n", encoding="utf-8")

    generator.build_repo_map([tmp_path], budget_tokens=500)
    dead_file.unlink()

    # 重新建置時應自動清除已不存在路徑的快取
    generator.build_repo_map([tmp_path], budget_tokens=500)
    with sqlite3.connect(db_file) as conn:
        paths = [r[0] for r in conn.execute("SELECT path FROM file_ast_cache").fetchall()]
    assert str(live_file.resolve()) in paths
    assert str(dead_file.resolve()) not in paths


def test_repo_map_lazy_db_init_and_external_root_relative_path(tmp_path, monkeypatch):
    """驗證 RepoMapGenerator(db_path=None) 延遲初始化不立即建立檔案，且非 cwd 工作區保留子目錄相對路徑。"""
    fake_user = tmp_path / "lazy_user"
    monkeypatch.setattr("pocket_agent.tools.repo_map.user_dir", lambda: fake_user)

    gen = RepoMapGenerator()
    # 建構時尚未觸發任何磁碟或資料庫建立
    assert not (fake_user / "db" / "repo_map_cache.db").exists()

    ext_root = tmp_path / "external_repo"
    sub_pkg = ext_root / "subpkg"
    sub_pkg.mkdir(parents=True)
    (sub_pkg / "worker.py").write_text("def run_worker() -> None:\n    pass\n", encoding="utf-8")

    # 將 cwd 設為另一個無關目錄，驗證仍能顯示 subpkg/worker.py 而非僅 worker.py
    other_cwd = tmp_path / "other_cwd"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)

    res = gen.build_repo_map([ext_root], budget_tokens=500)
    assert (fake_user / "db" / "repo_map_cache.db").is_file()
    assert "subpkg" in res and "worker.py" in res
    assert "def run_worker() -> None: ..." in res


def test_rag_indexed_search_skips_sensitive_clamps_top_k_and_i18n(tmp_path, monkeypatch):
    """驗證 indexed_search 過濾敏感 .env* 檔案、夾限 top_k、支援多語系並包裹 <external_content>。"""
    from pocket_agent.system.constants import MAX_RAG_TOP_K
    from pocket_agent.system.i18n import set_locale
    from pocket_agent.tools import rag

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(rag, "_last_sync_state", None)

    (tmp_path / "auth_service.py").write_text(
        "def verify_jwt_token(token: str) -> bool:\n    return True\n",
        encoding="utf-8",
    )
    (tmp_path / ".env.json").write_text(
        '{"verify_jwt_token": "super_secret_leaked_key"}\n',
        encoding="utf-8",
    )

    assert rag._resolve_top_k(None) == 5
    assert rag._resolve_top_k(-3) == 5
    assert rag._resolve_top_k("bad") == 5
    assert rag._resolve_top_k(999) == MAX_RAG_TOP_K

    set_locale("zh_TW")
    res_zh = rag.call_indexed_search({"query": "verify_jwt_token", "top_k": 50}, workspace_dirs=[str(tmp_path)])
    assert res_zh.startswith("<external_content>")
    assert "【RAG 檢索結果】" in res_zh
    assert "auth_service.py" in res_zh
    assert "super_secret_leaked_key" not in res_zh
    assert ".env.json" not in res_zh

    set_locale("en_US")
    res_en = rag.call_indexed_search({"query": "verify_jwt_token"}, workspace_dirs=[str(tmp_path)])
    assert "【RAG Search Results】" in res_en
    assert "File: auth_service.py" in res_en

    res_none_en = rag.call_indexed_search({"query": "nonexistent_symbol_xyz"}, workspace_dirs=[str(tmp_path)])
    assert "No relevant code found" in res_none_en
