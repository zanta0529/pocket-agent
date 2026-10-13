"""SpilloverManager 巨量輸出落盤虛擬化單元測試。"""

from pocket_agent.core.spillover import SpilloverManager


def test_spillover_bypasses_short_text(tmp_path):
    mgr = SpilloverManager(output_dir=tmp_path)
    short_text = "This is a normal output from tool."
    res = mgr.spill_or_pass(short_text, tool_name="read_file", call_id="c1", max_chars=1000)
    assert res == short_text
    # 不應產生落盤檔案
    assert list(tmp_path.glob("*.log")) == []


def test_spillover_dumps_large_text_and_formats_preview(tmp_path):
    mgr = SpilloverManager(output_dir=tmp_path)

    head = "HEADER_START\n" + ("padding_head_line\n" * 40)
    middle = "MIDDLE_SECRET\n" * 200
    tail = ("padding_tail_line\n" * 40) + "\nFOOTER_CONCLUSION"
    large_text = head + middle + tail  # 遠超過 2000 字元

    res = mgr.spill_or_pass(
        large_text,
        tool_name="run_command",
        call_id="call_9999",
        max_chars=1000,  # 門檻設為 1000 字元
    )

    # 驗證回傳的文字包含結構化預覽
    assert "[系統日誌虛擬化]" in res
    assert "HEADER_START" in res
    assert "FOOTER_CONCLUSION" in res
    assert "MIDDLE_SECRET" not in res  # 中間應被省略
    assert "完整日誌路徑" in res

    # 驗證落盤檔案存在且包含完整全文
    log_files = list(tmp_path.glob("run_command_call_999_*.log"))
    assert len(log_files) == 1
    disk_content = log_files[0].read_text(encoding="utf-8")
    assert disk_content == large_text
    assert "MIDDLE_SECRET" in disk_content


def test_spillover_non_string_passthrough(tmp_path):
    mgr = SpilloverManager(output_dir=tmp_path)
    assert mgr.spill_or_pass(None) is None
    assert mgr.spill_or_pass(12345) == 12345
