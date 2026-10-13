"""RepoMapGenerator 專案結構地圖單元測試。"""

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
