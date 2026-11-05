"""Housekeeper 自動管家維護單元測試。"""

import os
import time

from pocket_agent.system.housekeeping import Housekeeper


def test_housekeeper_clean_legacy_root_logs(tmp_path):
    fake_cwd = tmp_path / "repo"
    fake_cwd.mkdir()
    fake_root_log = fake_cwd / "agent.log"
    fake_root_log.write_text("old root log", encoding="utf-8")

    fake_log_dir = tmp_path / "agents_logs"
    fake_log_dir.mkdir()
    active_log = fake_log_dir / "agent.log"
    active_log.write_text("active log", encoding="utf-8")

    hk = Housekeeper(
        target_log_dir=fake_log_dir,
        target_scratch_dir=tmp_path / "scratch",
        cwd=fake_cwd,
    )
    cleaned = hk.clean_legacy_root_logs()
    assert cleaned == 1
    assert not fake_root_log.exists()
    assert active_log.exists()


def test_housekeeper_clean_scratch_outputs_ttl_and_whitelist(tmp_path):
    scratch_dir = tmp_path / "scratch"
    outputs_dir = scratch_dir / "outputs"
    outputs_dir.mkdir(parents=True)

    now = time.time()
    ten_days_ago = now - (10 * 86400)

    # 1. 過期落盤日誌 (應刪除)
    old_log = outputs_dir / "tool_old_123.log"
    old_log.write_text("old output", encoding="utf-8")
    os.utime(old_log, (ten_days_ago, ten_days_ago))

    # 2. 新落盤日誌 (應保留)
    new_log = outputs_dir / "tool_new_456.log"
    new_log.write_text("new output", encoding="utf-8")

    # 3. 受保護筆記 (應保留)
    notes_file = outputs_dir / "notes.md"
    notes_file.write_text("my notes", encoding="utf-8")
    os.utime(notes_file, (ten_days_ago, ten_days_ago))

    # 4. .keep 標記檔案 (應保留)
    keep_file = outputs_dir / "archive.keep"
    keep_file.write_text("important data", encoding="utf-8")
    os.utime(keep_file, (ten_days_ago, ten_days_ago))

    hk = Housekeeper(
        target_log_dir=tmp_path / "logs",
        target_scratch_dir=scratch_dir,
    )
    count, freed = hk.clean_scratch_outputs(ttl_days=7)

    assert count == 1
    assert freed > 0
    assert not old_log.exists()
    assert new_log.exists()
    assert notes_file.exists()
    assert keep_file.exists()


def test_housekeeper_clean_scratch_outputs_max_files(tmp_path):
    scratch_dir = tmp_path / "scratch"
    outputs_dir = scratch_dir / "outputs"
    outputs_dir.mkdir(parents=True)

    now = time.time()
    created_files = []
    for i in range(15):
        f = outputs_dir / f"output_{i:02d}.log"
        f.write_text(f"content {i}", encoding="utf-8")
        file_time = now - (15 - i) * 60  # 每檔間隔 1 分鐘
        os.utime(f, (file_time, file_time))
        created_files.append(f)

    hk = Housekeeper(
        target_log_dir=tmp_path / "logs",
        target_scratch_dir=scratch_dir,
    )
    # TTL 設很大，但上限只保留 10 檔
    count, _freed = hk.clean_scratch_outputs(ttl_days=30, max_files=10)

    assert count == 5
    surviving = list(outputs_dir.glob("*.log"))
    assert len(surviving) == 10
    # 最舊的 5 個應被刪除 (output_00 ~ output_04)
    for i in range(5):
        assert not created_files[i].exists()
    # 最新的 10 個保留 (output_05 ~ output_14)
    for i in range(5, 15):
        assert created_files[i].exists()


def test_housekeeper_clean_logs_preserves_active_log(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True)

    now = time.time()
    twenty_days_ago = now - (20 * 86400)

    active_log = log_dir / "agent.log"
    active_log.write_text("active logger", encoding="utf-8")
    os.utime(active_log, (twenty_days_ago, twenty_days_ago))

    old_rotated = log_dir / "agent.log.1"
    old_rotated.write_text("old rotated", encoding="utf-8")
    os.utime(old_rotated, (twenty_days_ago, twenty_days_ago))

    hk = Housekeeper(
        target_log_dir=log_dir,
        target_scratch_dir=tmp_path / "scratch",
    )
    count, _freed = hk.clean_logs(ttl_days=7)

    assert count == 1
    # active agent.log 即使時間超過 20 天也絕對不可刪除
    assert active_log.exists()
    # 舊輪轉檔應被清理
    assert not old_rotated.exists()


def test_housekeeper_cooldown_mechanism(tmp_path):
    hk = Housekeeper(
        target_log_dir=tmp_path / "logs",
        target_scratch_dir=tmp_path / "scratch",
    )
    # 尚未執行過
    assert hk.is_in_cooldown() is False
    hk.mark_executed()
    # 剛執行完畢應在冷卻期
    assert hk.is_in_cooldown() is True
