from pathlib import Path

from pocket_agent.tools.fs import call_fs_tool


def test_list_dir_tree_structure(tmp_path: Path):
    """測試 list_dir 工具是否能正確回傳受限深度的樹狀結構"""

    (tmp_path / "root_file.txt").write_text("root", encoding="utf-8")

    subdir_1 = tmp_path / "subdir_1"
    subdir_1.mkdir()
    (subdir_1 / "file_1.txt").write_text("file1", encoding="utf-8")

    subdir_2 = tmp_path / "subdir_2"
    subdir_2.mkdir()
    subsubdir = subdir_2 / "subsubdir"
    subsubdir.mkdir()
    (subsubdir / "deep_file.txt").write_text("deep", encoding="utf-8")

    args = {"path": str(tmp_path), "depth": 2}

    result = call_fs_tool(name="list_dir", args=args, on_confirm=None, workspace_dirs=[str(tmp_path)])

    assert "[DIR]" in result
    assert "├── " in result or "└── " in result

    assert "root_file.txt" in result
    assert "[DIR] subdir_1/" in result
    assert "file_1.txt" in result
    assert "[DIR] subdir_2/" in result
    assert "[DIR] subsubdir/" in result

    # 深度限制保護
    assert "deep_file.txt" not in result
    assert "... (已達深度限制 2)" in result


def test_get_file_info(tmp_path: Path):
    """測試 get_file_info 工具是否能正確讀取檔案資訊"""

    test_file = tmp_path / "info_test.txt"
    test_file.write_text("Line 1\nLine 2\nLine 3", encoding="utf-8")

    args = {"path": str(test_file)}

    result = call_fs_tool(name="get_file_info", args=args, on_confirm=None, workspace_dirs=[str(tmp_path)])

    assert "類型: 一般檔案" in result
    assert "大小:" in result
    assert "總行數: 3" in result
    assert "最後修改時間:" in result


def test_append_to_file(tmp_path: Path):
    """測試 append_to_file 是否能成功將字串附加至檔尾"""

    test_file = tmp_path / "append_test.txt"
    test_file.write_text("Initial Line\n", encoding="utf-8")

    args = {"path": str(test_file), "content": "Appended Line\n"}

    # 注意：這裡將 on_confirm 設為 lambda _: True 模擬使用者同意授權
    result = call_fs_tool(name="append_to_file", args=args, on_confirm=lambda _: True, workspace_dirs=[str(tmp_path)])

    assert "[SUCCESS]" in result

    final_content = test_file.read_text(encoding="utf-8")
    assert "Initial Line" in final_content
    assert "Appended Line" in final_content
    assert final_content == "Initial Line\nAppended Line\n"


def test_read_file_truncation_and_range(tmp_path: Path):
    """測試 read_file 的 500 行截斷防護以及指定範圍讀取"""

    test_file = tmp_path / "read_test.txt"

    # 建立一個 600 行的檔案
    lines = [f"This is line {i}" for i in range(1, 601)]
    test_file.write_text("\n".join(lines), encoding="utf-8")

    # 測試情境一：無指定範圍，應觸發 500 行截斷
    args_full = {"path": str(test_file)}
    result_full = call_fs_tool(name="read_file", args=args_full, on_confirm=None, workspace_dirs=[str(tmp_path)])

    assert "This is line 500" in result_full
    assert "This is line 501" not in result_full
    assert "內容過長，共 600 行，目前僅顯示前 500 行" in result_full

    # 測試情境二：指定範圍讀取 (例如讀取 550 到 555 行)
    args_range = {"path": str(test_file), "start_line": 550, "end_line": 555}
    result_range = call_fs_tool(name="read_file", args=args_range, on_confirm=None, workspace_dirs=[str(tmp_path)])

    assert "This is line 550" in result_range
    assert "This is line 555" in result_range
    assert "This is line 549" not in result_range
    assert "This is line 556" not in result_range
    assert "內容過長" not in result_range


def test_replace_file_content_with_scope(tmp_path: Path):
    """測試 replace_file_content 的 start_line/end_line 區間鎖定功能"""

    test_file = tmp_path / "replace_test.txt"

    # 建立一個包含重複特徵的檔案
    original_content = """def func_a():
    print("Hello")

def func_b():
    print("Hello")
"""
    test_file.write_text(original_content, encoding="utf-8")

    # 如果不指定範圍，因為 "print("Hello")" 有兩個，應該會報錯
    args_fail = {"path": str(test_file), "target": '    print("Hello")', "replacement": '    print("World")'}
    result_fail = call_fs_tool(
        name="replace_file_content", args=args_fail, on_confirm=lambda _: True, workspace_dirs=[str(tmp_path)]
    )
    assert "[ERROR]" in result_fail
    assert "請設定 allow_multiple=true、提供更多上下文，或指定 start_line/end_line 確保唯一性" in result_fail

    # 指定範圍，僅替換 func_b (約第 4-6 行) 中的內容
    args_success = {
        "path": str(test_file),
        "target": '    print("Hello")',
        "replacement": '    print("World")',
        "start_line": 4,
        "end_line": 6,
    }
    result_success = call_fs_tool(
        name="replace_file_content", args=args_success, on_confirm=lambda _: True, workspace_dirs=[str(tmp_path)]
    )

    assert "[SUCCESS]" in result_success

    final_content = test_file.read_text(encoding="utf-8")
    assert 'def func_a():\n    print("Hello")' in final_content  # func_a 保持不變
    assert 'def func_b():\n    print("World")' in final_content  # func_b 已被修改
