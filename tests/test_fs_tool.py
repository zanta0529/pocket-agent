from pathlib import Path

from pocket_agent.system.i18n import set_locale
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

    # 注意：驗證傳遞給 on_confirm 的訊息是否包含正確的風險提示
    result = call_fs_tool(
        name="append_to_file", args=args, on_confirm=lambda msg: "附加內容至檔案" in msg, workspace_dirs=[str(tmp_path)]
    )

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
    assert "內容過長，共 600 行，已自動截斷前 500 行" in result_full

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
        name="replace_file_content",
        args=args_fail,
        on_confirm=lambda msg: "局部修改檔案" in msg,
        workspace_dirs=[str(tmp_path)],
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
        name="replace_file_content",
        args=args_success,
        on_confirm=lambda msg: "局部修改檔案" in msg,
        workspace_dirs=[str(tmp_path)],
    )

    assert "[SUCCESS]" in result_success

    final_content = test_file.read_text(encoding="utf-8")
    assert 'def func_a():\n    print("Hello")' in final_content  # func_a 保持不變
    assert 'def func_b():\n    print("World")' in final_content  # func_b 已被修改


def test_fs_write_tools_reject_string_and_non_true_confirmations(tmp_path: Path, monkeypatch):
    """測試檔案寫入工具僅接受 True 核准，文字指示與非 True 值均拒絕且不寫入。"""
    backup_calls = []
    monkeypatch.setattr("pocket_agent.tools.fs._backup_file", lambda p: backup_calls.append(p))

    existing = tmp_path / "existing.txt"
    existing.write_text("original\n", encoding="utf-8")
    new_target = tmp_path / "nested_dir" / "new_file.txt"

    custom_instruction = "不要修改，先給我差異"

    # 1. write_to_file 新檔：文字拒絕時新檔與父目錄均不建立
    res_new = call_fs_tool(
        name="write_to_file",
        args={"path": str(new_target), "content": "new content\n"},
        on_confirm=lambda _msg: custom_instruction,
        workspace_dirs=[str(tmp_path)],
    )
    assert res_new.startswith("[使用者拒絕執行此檔案寫入操作")
    assert custom_instruction in res_new
    assert not new_target.exists()
    assert not new_target.parent.exists()
    assert len(backup_calls) == 0

    # 2. write_to_file 覆寫既有檔案：文字拒絕時原檔不變、不備份
    res_overwrite = call_fs_tool(
        name="write_to_file",
        args={"path": str(existing), "content": "overwritten\n", "overwrite": True},
        on_confirm=lambda _msg: custom_instruction,
        workspace_dirs=[str(tmp_path)],
    )
    assert res_overwrite.startswith("[使用者拒絕執行此檔案寫入操作")
    assert custom_instruction in res_overwrite
    assert existing.read_text(encoding="utf-8") == "original\n"
    assert len(backup_calls) == 0

    # 3. append_to_file：文字拒絕時原檔不變、不備份
    res_append = call_fs_tool(
        name="append_to_file",
        args={"path": str(existing), "content": "extra\n"},
        on_confirm=lambda _msg: custom_instruction,
        workspace_dirs=[str(tmp_path)],
    )
    assert res_append.startswith("[使用者拒絕執行此檔案寫入操作")
    assert custom_instruction in res_append
    assert existing.read_text(encoding="utf-8") == "original\n"
    assert len(backup_calls) == 0

    # 4. replace_file_content：文字拒絕時原檔不變、不備份
    res_replace = call_fs_tool(
        name="replace_file_content",
        args={"path": str(existing), "target": "original", "replacement": "changed"},
        on_confirm=lambda _msg: custom_instruction,
        workspace_dirs=[str(tmp_path)],
    )
    assert res_replace.startswith("[使用者拒絕執行此檔案替換操作")
    assert custom_instruction in res_replace
    assert existing.read_text(encoding="utf-8") == "original\n"
    assert len(backup_calls) == 0

    # 5. 其他非 True 值（False、None、空字串、tuple）一律拒絕
    for non_true in (False, None, "", (False, "請改用其他方式"), (True, "not a bool")):
        res_other = call_fs_tool(
            name="replace_file_content",
            args={"path": str(existing), "target": "original", "replacement": "changed"},
            on_confirm=lambda _msg, val=non_true: val,
            workspace_dirs=[str(tmp_path)],
        )
        assert res_other.startswith("[使用者拒絕")
        assert existing.read_text(encoding="utf-8") == "original\n"
    assert len(backup_calls) == 0


def test_fs_read_only_not_found_returns_non_error_marker_i18n(tmp_path: Path):
    """測試唯讀工具探測不存在路徑時回傳 [NOT_FOUND] 與雙語系訊息，真正錯誤仍維持 [ERROR]。"""
    missing_dir = tmp_path / ".generate-system-docs"
    missing_file = tmp_path / "missing.txt"
    ws = [str(tmp_path)]

    # 1. zh_TW 語系
    set_locale("zh_TW")
    res_dir = call_fs_tool("list_dir", {"path": str(missing_dir)}, on_confirm=None, workspace_dirs=ws)
    assert res_dir.startswith("[NOT_FOUND] ")
    assert "目錄不存在：" in res_dir
    assert "[ERROR]" not in res_dir

    res_file = call_fs_tool("read_file", {"path": str(missing_file)}, on_confirm=None, workspace_dirs=ws)
    assert res_file.startswith("[NOT_FOUND] ")
    assert "檔案不存在：" in res_file
    assert "[ERROR]" not in res_file

    res_info = call_fs_tool("get_file_info", {"path": str(missing_file)}, on_confirm=None, workspace_dirs=ws)
    assert res_info.startswith("[NOT_FOUND] ")
    assert "檔案或目錄不存在：" in res_info
    assert "[ERROR]" not in res_info

    # 2. en_US 語系
    set_locale("en_US")
    res_dir_en = call_fs_tool("list_dir", {"path": str(missing_dir)}, on_confirm=None, workspace_dirs=ws)
    assert res_dir_en.startswith("[NOT_FOUND] ")
    assert "Directory not found:" in res_dir_en

    res_file_en = call_fs_tool("read_file", {"path": str(missing_file)}, on_confirm=None, workspace_dirs=ws)
    assert res_file_en.startswith("[NOT_FOUND] ")
    assert "File not found:" in res_file_en

    res_info_en = call_fs_tool("get_file_info", {"path": str(missing_file)}, on_confirm=None, workspace_dirs=ws)
    assert res_info_en.startswith("[NOT_FOUND] ")
    assert "File or directory not found:" in res_info_en

    # 3. 真正型別錯誤仍維持 [ERROR]
    regular = tmp_path / "regular.txt"
    regular.write_text("hi", encoding="utf-8")
    err_list_file = call_fs_tool("list_dir", {"path": str(regular)}, on_confirm=None, workspace_dirs=ws)
    assert err_list_file.startswith("[ERROR]")
    err_read_dir = call_fs_tool("read_file", {"path": str(tmp_path)}, on_confirm=None, workspace_dirs=ws)
    assert err_read_dir.startswith("[ERROR]")


def test_write_to_file_manual_confirm_allows_overwrite_without_flag(tmp_path: Path, monkeypatch):
    """手動確認模式（require_overwrite_flag=False）寫入既有檔案時，不需 overwrite=true 即提示確認並覆寫。"""
    monkeypatch.chdir(tmp_path)
    backup_calls: list[Path] = []
    monkeypatch.setattr("pocket_agent.tools.fs._backup_file", lambda p: backup_calls.append(p))

    target = tmp_path / "docs" / "report.md"
    target.parent.mkdir(parents=True)
    target.write_text("old report\n", encoding="utf-8")

    # 1. 省略 overwrite：拒絕時不備份、原檔不變
    seen_prompts: list[str] = []
    refused = call_fs_tool(
        "write_to_file",
        {"path": "docs/report.md", "content": "new report\n"},
        on_confirm=lambda msg: (seen_prompts.append(msg), False)[1],
        workspace_dirs=[str(tmp_path)],
    )
    assert seen_prompts == ["覆寫既有檔案：docs/report.md"]
    assert refused.startswith("[使用者拒絕")
    assert target.read_text(encoding="utf-8") == "old report\n"
    assert len(backup_calls) == 0

    # 2. 顯式 overwrite=False 仍可直接進入人工確認並在核准後覆寫
    seen_prompts.clear()
    ok_res = call_fs_tool(
        "write_to_file",
        {"path": "docs/report.md", "content": "new report\n", "overwrite": False},
        on_confirm=lambda msg: (seen_prompts.append(msg), True)[1],
        workspace_dirs=[str(tmp_path)],
    )
    assert seen_prompts == ["覆寫既有檔案：docs/report.md"]
    assert ok_res.startswith("[SUCCESS] 成功覆寫檔案 ")
    assert target.read_text(encoding="utf-8") == "new report\n"
    assert len(backup_calls) == 1


def test_write_to_file_require_overwrite_flag_in_accept_edits_i18n(tmp_path: Path, monkeypatch):
    """自動編輯模式（require_overwrite_flag=True）覆寫既有檔案須帶 overwrite=true，新檔直接建立。"""
    monkeypatch.chdir(tmp_path)
    existing = tmp_path / "app.py"
    existing.write_text("v1\n", encoding="utf-8")
    ws = [str(tmp_path)]
    confirm_calls: list[str] = []

    def auto_confirm(msg: str) -> bool:
        confirm_calls.append(msg)
        return True

    # 1. zh_TW：未帶 overwrite=True 時回傳 [OVERWRITE_REQUIRED]，不觸發確認且不覆寫
    set_locale("zh_TW")
    res_zh = call_fs_tool(
        "write_to_file",
        {"path": "app.py", "content": "v2\n"},
        on_confirm=auto_confirm,
        workspace_dirs=ws,
        require_overwrite_flag=True,
    )
    assert res_zh.startswith("[OVERWRITE_REQUIRED] ")
    assert "overwrite=true" in res_zh
    assert "replace_file_content" in res_zh
    assert existing.read_text(encoding="utf-8") == "v1\n"
    assert confirm_calls == []

    # 2. en_US：英文提示語系一致
    set_locale("en_US")
    res_en = call_fs_tool(
        "write_to_file",
        {"path": "app.py", "content": "v2\n"},
        on_confirm=auto_confirm,
        workspace_dirs=ws,
        require_overwrite_flag=True,
    )
    assert res_en.startswith("[OVERWRITE_REQUIRED] ")
    assert "Accept-edits mode" in res_en
    assert "overwrite=true" in res_en
    assert existing.read_text(encoding="utf-8") == "v1\n"
    assert confirm_calls == []

    # 3. 補帶 overwrite=True 後成功覆寫
    res_ok = call_fs_tool(
        "write_to_file",
        {"path": "app.py", "content": "v2\n", "overwrite": True},
        on_confirm=auto_confirm,
        workspace_dirs=ws,
        require_overwrite_flag=True,
    )
    assert res_ok.startswith("[SUCCESS] 成功覆寫檔案 ")
    assert existing.read_text(encoding="utf-8") == "v2\n"

    # 4. 不存在的新檔不需 overwrite 即可建立
    new_file = tmp_path / "fresh.py"
    res_new = call_fs_tool(
        "write_to_file",
        {"path": "fresh.py", "content": "fresh\n"},
        on_confirm=auto_confirm,
        workspace_dirs=ws,
        require_overwrite_flag=True,
    )
    assert res_new.startswith("[SUCCESS] 成功建立檔案 ")
    assert new_file.read_text(encoding="utf-8") == "fresh\n"


def test_write_to_file_rejects_existing_directory_i18n(tmp_path: Path):
    """目標路徑為既有目錄時應回傳 [ERROR]，不觸發人工確認。"""
    sub_dir = tmp_path / "folder"
    sub_dir.mkdir()
    ws = [str(tmp_path)]
    confirm_calls: list[str] = []

    for locale, expected in (("zh_TW", "是一個目錄"), ("en_US", "is a directory")):
        set_locale(locale)
        for req_flag in (False, True):
            res = call_fs_tool(
                "write_to_file",
                {"path": str(sub_dir), "content": "bad"},
                on_confirm=lambda msg: (confirm_calls.append(msg), True)[1],
                workspace_dirs=ws,
                require_overwrite_flag=req_flag,
            )
            assert res.startswith("[ERROR] ")
            assert expected in res
    assert confirm_calls == []


def test_replace_file_content_trailing_whitespace_fallback(tmp_path: Path, monkeypatch):
    """行尾多餘空白與 CRLF 差異不應阻止 replace_file_content 唯一匹配與替換。"""
    monkeypatch.chdir(tmp_path)

    def _auto_confirm(_msg: str) -> bool:
        return True

    sample = tmp_path / "ws.py"
    sample.write_text("def foo():   \n    return 1\t \nend = True\n", encoding="utf-8")
    ws = [str(tmp_path)]

    res = call_fs_tool(
        "replace_file_content",
        {
            "path": "ws.py",
            "start_line": 1,
            "end_line": 2,
            "target": "def foo():\r\n    return 1",
            "replacement": "def foo():\n    return 2",
        },
        on_confirm=_auto_confirm,
        workspace_dirs=ws,
    )
    assert res.startswith("[SUCCESS]")
    assert sample.read_text(encoding="utf-8") == "def foo():\n    return 2\nend = True\n"


def test_replace_file_content_bounded_drift_auto_align(tmp_path: Path, monkeypatch):
    """行號在上下 20 行內偏移且唯一時應自動對齊，但展開範圍內若有多處相符則不得誤替換。"""
    monkeypatch.chdir(tmp_path)

    def _auto_confirm(_msg: str) -> bool:
        return True

    ws = [str(tmp_path)]
    drift_file = tmp_path / "drift.py"
    lines = [f"x_{i} = {i}\n" for i in range(60)]
    lines[47] = "def target_fn():\n"
    lines[48] = "    return 42\n"
    drift_file.write_text("".join(lines), encoding="utf-8")

    res_ok = call_fs_tool(
        "replace_file_content",
        {
            "path": "drift.py",
            "start_line": 25,
            "end_line": 35,
            "target": "def target_fn():\n    return 42",
            "replacement": "def target_fn():\n    return 99",
        },
        on_confirm=_auto_confirm,
        workspace_dirs=ws,
    )
    assert res_ok.startswith("[SUCCESS]")
    assert "return 99" in drift_file.read_text(encoding="utf-8")

    ambig_file = tmp_path / "ambig_drift.py"
    ambig_lines = [f"row_{i}\n" for i in range(40)]
    ambig_lines[8] = "dup_marker = 1\n"
    ambig_lines[16] = "dup_marker = 1\n"
    ambig_file.write_text("".join(ambig_lines), encoding="utf-8")

    res_ambig = call_fs_tool(
        "replace_file_content",
        {
            "path": "ambig_drift.py",
            "start_line": 1,
            "end_line": 3,
            "target": "dup_marker = 1",
            "replacement": "dup_marker = 2",
        },
        on_confirm=_auto_confirm,
        workspace_dirs=ws,
    )
    assert res_ambig.startswith("[TARGET_NOT_FOUND]")
    assert "L9" in res_ambig and "L17" in res_ambig
    assert ambig_file.read_text(encoding="utf-8").count("dup_marker = 1") == 2


def test_replace_file_content_far_or_ambiguous_returns_target_not_found_with_locations(tmp_path: Path, monkeypatch):
    """超出偏移範圍或全檔找不到目標時，應回傳 [TARGET_NOT_FOUND] 與實際行號線索（雙語皆然）。"""
    monkeypatch.chdir(tmp_path)

    def _auto_confirm(_msg: str) -> bool:
        return True

    ws = [str(tmp_path)]
    far_file = tmp_path / "far.py"
    lines = [f"line_{i}\n" for i in range(80)]
    lines[74] = "target_token = 1\n"
    far_file.write_text("".join(lines), encoding="utf-8")

    for locale, total_hint in (("zh_TW", "全檔共 80 行"), ("en_US", "80 lines")):
        set_locale(locale)
        res_far = call_fs_tool(
            "replace_file_content",
            {
                "path": "far.py",
                "start_line": 1,
                "end_line": 20,
                "target": "target_token = 1",
                "replacement": "target_token = 2",
            },
            on_confirm=_auto_confirm,
            workspace_dirs=ws,
        )
        assert res_far.startswith("[TARGET_NOT_FOUND]")
        assert "L75" in res_far
        assert total_hint in res_far

        res_missing = call_fs_tool(
            "replace_file_content",
            {
                "path": "far.py",
                "target": "never_present_symbol",
                "replacement": "updated",
            },
            on_confirm=_auto_confirm,
            workspace_dirs=ws,
        )
        assert res_missing.startswith("[TARGET_NOT_FOUND]")
        assert total_hint in res_missing


def test_scratch_dir_write_append_replace_auto_allowed_only_for_current_workspace(tmp_path: Path, monkeypatch):
    """開啟 auto_allow_scratch 時，僅目前工作區 scratch_dir 內免確認；專案與其他全域目錄仍須確認。"""
    from pocket_agent.system.paths import scratch_dir, user_dir

    monkeypatch.chdir(tmp_path)
    ws = [str(tmp_path)]
    confirm_calls: list[str] = []

    def _record_confirm(msg: str) -> bool:
        confirm_calls.append(msg)
        return True

    # 1. 目前工作區 scratch_dir：新建、覆寫（含 require_overwrite_flag=True）、附加與替換皆免確認
    assess_file = scratch_dir() / "assess" / "assess.md"
    res_create = call_fs_tool(
        "write_to_file",
        {"path": str(assess_file), "content": "v1\n"},
        on_confirm=_record_confirm,
        workspace_dirs=ws,
        auto_allow_scratch=True,
    )
    assert res_create.startswith("[SUCCESS]")

    res_overwrite = call_fs_tool(
        "write_to_file",
        {"path": str(assess_file), "content": "v2\n"},
        on_confirm=_record_confirm,
        workspace_dirs=ws,
        require_overwrite_flag=True,
        auto_allow_scratch=True,
    )
    assert res_overwrite.startswith("[SUCCESS]")

    res_append = call_fs_tool(
        "append_to_file",
        {"path": str(assess_file), "content": "v3\n"},
        on_confirm=_record_confirm,
        workspace_dirs=ws,
        auto_allow_scratch=True,
    )
    assert res_append.startswith("[SUCCESS]")

    res_replace = call_fs_tool(
        "replace_file_content",
        {"path": str(assess_file), "target": "v2\nv3", "replacement": "v2_updated\nv3"},
        on_confirm=_record_confirm,
        workspace_dirs=ws,
        auto_allow_scratch=True,
    )
    assert res_replace.startswith("[SUCCESS]")
    assert assess_file.read_text(encoding="utf-8") == "v2_updated\nv3\n"
    assert confirm_calls == []

    # 2. 專案檔、其他工作區 scratch 與 skills 目錄仍須觸發人工確認
    other_scratch = user_dir() / "workspaces" / "other-slug" / "scratch" / "a.md"
    skill_file = user_dir() / "skills" / "demo" / "SKILL.md"
    for must_confirm_path in (tmp_path / "repo.py", other_scratch, skill_file):
        res_other = call_fs_tool(
            "write_to_file",
            {"path": str(must_confirm_path), "content": "x\n"},
            on_confirm=_record_confirm,
            workspace_dirs=ws,
            auto_allow_scratch=True,
        )
        assert res_other.startswith("[SUCCESS]")
    assert len(confirm_calls) == 3

    # 3. 敏感檔名 (.env) 即使在 scratch_dir 下仍應被安全限制拒絕
    res_env = call_fs_tool(
        "write_to_file",
        {"path": str(scratch_dir() / ".env"), "content": "SECRET=1\n"},
        on_confirm=_record_confirm,
        workspace_dirs=ws,
        auto_allow_scratch=True,
    )
    assert res_env.startswith("[ERROR]")
    assert len(confirm_calls) == 3


def test_fs_tool_handler_parallel_safety_distinguishes_read_and_write():
    """驗證 FsToolHandler.is_parallel_safe 僅對唯讀 FS 工具回傳 True，寫入型工具回傳 False。"""
    from pocket_agent.core.tool_registry import FsToolHandler, default_tool_registry
    from pocket_agent.tools.fs import FS_TOOLS, SAFE_READ_FS_TOOLS

    handler = default_tool_registry.resolve("read_file")
    assert isinstance(handler, FsToolHandler)

    expected_read = {"read_file", "list_dir", "get_file_info", "search_files"}
    expected_write = {"write_to_file", "append_to_file", "replace_file_content"}
    assert SAFE_READ_FS_TOOLS == expected_read

    for tool_def in FS_TOOLS:
        t_name = tool_def["name"]
        if t_name in expected_read:
            assert handler.is_parallel_safe(t_name, {}, None) is True
        elif t_name in expected_write:
            assert handler.is_parallel_safe(t_name, {}, None) is False
        else:
            raise AssertionError(f"未分類的 FS 工具: {t_name}")
