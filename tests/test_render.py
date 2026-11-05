import json
from io import StringIO

from rich.console import Console

from pocket_agent.frontend.cli.render import (
    CliRenderer,
    format_display_path,
    middle_truncate,
)


def test_format_display_path_relative_and_fallback(tmp_path):
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    child_file = cwd / "src" / "main.py"
    child_file.parent.mkdir(parents=True)
    child_file.write_text("code")

    # 1. 位於工作目錄內：轉為相對路徑（POSIX 風格）
    rel_path = format_display_path(str(child_file), base_dir=cwd)
    assert rel_path == "src/main.py"

    # 2. 空字串或非字串安全返回
    assert format_display_path("") == ""
    assert format_display_path(None) == ""

    # 3. 跨目錄但無法相對化（例如非子目錄且非 home）安全 fallback
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("data")
    assert format_display_path(str(outside_file), base_dir=cwd) != ""


def test_middle_truncate():
    # 1. 長度未超限時維持原貌
    s_short = "src/utils.py"
    assert middle_truncate(s_short, 20) == s_short

    # 2. 長路徑中間截斷，保留開頭與結尾檔名
    long_path = "very/deeply/nested/directory/structure/and/long/path/target_file.py"
    truncated = middle_truncate(long_path, 30)
    assert len(truncated) <= 30
    assert "..." in truncated
    assert truncated.endswith("target_file.py")

    # 3. 含有中文字元（全形寬度為 2）
    chinese_text = "專案說明文件_這是一份非常非常長的中文字串測試內容_結尾標記.md"
    tr_ch = middle_truncate(chinese_text, 25)
    assert "..." in tr_ch
    assert tr_ch.endswith("結尾標記.md")

    # 4. 極短邊界
    assert middle_truncate("abcdef", 0) == ""
    short_res = middle_truncate("abcdef", 4)
    assert len(short_res) <= 4
    res_6 = middle_truncate("abcdefghij", 6)
    assert len(res_6) <= 6
    assert "..." in res_6


def test_extract_tool_target(tmp_path):
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=100)
    renderer = CliRenderer(console)

    # 1. read_file 包含絕對路徑與行號
    target_f = tmp_path / "app.py"
    target_f.write_text("print(1)")
    args_read = json.dumps(
        {
            "file_path": str(target_f),
            "start_line": 10,
            "end_line": 25,
        }
    )
    res_read = renderer._extract_tool_target("read_file", args_read)
    assert "#L10-L25" in res_read

    # 2. run_command 多行壓縮為單行
    args_cmd = json.dumps({"command": "echo hello\necho world"})
    res_cmd = renderer._extract_tool_target("run_command", args_cmd)
    assert res_cmd == "echo hello echo world"

    # 3. URL 目標
    sample_url = "https://example.com/api/v1/resources"
    args_web = json.dumps({"url": sample_url})
    res_web = renderer._extract_tool_target("web_fetch", args_web)
    assert res_web == sample_url

    # 4. 向後相容 _summarise_args
    assert renderer._summarise_args("run_command", args_cmd) == "echo hello echo world"


def test_cli_renderer_two_tier_tool_done_output(tmp_path):
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=120)
    renderer = CliRenderer(console)

    # 模擬 tool_start 與 tool_done
    target_file = tmp_path / "agent_test.py"
    target_file.write_text("x = 1")
    args = json.dumps({"file_path": str(target_file)})

    renderer.event(
        "tool_start",
        {
            "name": "read_file",
            "arguments": args,
            "call_id": "call_1",
            "is_safe_read": True,
        },
    )
    renderer.event(
        "tool_done",
        {
            "name": "read_file",
            "call_id": "call_1",
            "ok": True,
        },
    )

    rendered_output = output.getvalue()
    lines = [line.strip() for line in rendered_output.splitlines() if line.strip()]

    # 驗證雙行輸出
    # 第一行：● Read (read_file)
    assert any("Read" in line and "read_file" in line for line in lines)
    # 第二行：└─ 目標檔案路徑
    assert any("└─" in line and "agent_test.py" in line for line in lines)


def test_cli_renderer_security_warning_in_sub_tier():
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=120)
    renderer = CliRenderer(console)

    args = json.dumps({"command": "【安全警告】 rm -rf /"})
    renderer.event(
        "tool_start",
        {
            "name": "run_command",
            "arguments": args,
            "call_id": "call_danger",
            "is_safe_read": False,
        },
    )
    renderer.event(
        "tool_done",
        {
            "name": "run_command",
            "call_id": "call_danger",
            "ok": True,
        },
    )

    rendered_output = output.getvalue()
    lines = [line.strip() for line in rendered_output.splitlines() if line.strip()]
    assert any("Action" in line and "run_command" in line for line in lines)
    assert any("└─" in line and "【安全警告】" in line and "rm -rf /" in line for line in lines)
