"""Housekeeper 自動管家維護單元測試。"""

import asyncio
import json
import os
import time
from unittest.mock import Mock, patch

from pocket_agent.frontend.cli.commands import CommandHandler
from pocket_agent.frontend.cli.state import CliState
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


def test_housekeeper_env_config_parsing(tmp_path, monkeypatch):
    hk = Housekeeper(
        target_log_dir=tmp_path / "logs",
        target_scratch_dir=tmp_path / "scratch",
    )
    monkeypatch.setenv("POCKET_AGENT_HOUSEKEEPING_ENABLED", "false")
    assert hk.is_enabled is False
    monkeypatch.setenv("POCKET_AGENT_HOUSEKEEPING_ENABLED", "0")
    assert hk.is_enabled is False
    monkeypatch.setenv("POCKET_AGENT_HOUSEKEEPING_ENABLED", "true")
    assert hk.is_enabled is True

    monkeypatch.setenv("POCKET_AGENT_HOUSEKEEPING_TTL_DAYS", "not_a_number")
    assert hk.ttl_days == 7
    monkeypatch.setenv("POCKET_AGENT_HOUSEKEEPING_TTL_DAYS", "-5")
    assert hk.ttl_days == 1
    monkeypatch.setenv("POCKET_AGENT_HOUSEKEEPING_MAX_FILES", "0")
    assert hk.max_files == 1
    monkeypatch.setenv("POCKET_AGENT_HOUSEKEEPING_MAX_SIZE_MB", "0")
    assert hk.max_size_mb == 1


def test_housekeeper_clean_scratch_clipboard_and_checkpoints(tmp_path):
    scratch_dir = tmp_path / "scratch"
    clip_dir = scratch_dir / "clipboard"
    clip_dir.mkdir(parents=True)

    now = time.time()
    ten_days_ago = now - (10 * 86400)

    old_shot = clip_dir / "screenshot_20260101_000000_abcd1234.png"
    old_shot.write_bytes(b"old png")
    os.utime(old_shot, (ten_days_ago, ten_days_ago))

    new_shot = clip_dir / "screenshot_20261116_000000_efgh5678.png"
    new_shot.write_bytes(b"new png")

    ckpt = scratch_dir / "research_checkpoint.md"
    ckpt.write_text("old checkpoint", encoding="utf-8")
    os.utime(ckpt, (ten_days_ago, ten_days_ago))

    hk = Housekeeper(
        target_log_dir=tmp_path / "logs",
        target_scratch_dir=scratch_dir,
    )
    c1, _ = hk.clean_scratch_clipboard(ttl_days=7)
    c2, _ = hk.clean_scratch_checkpoints(ttl_days=7)

    assert c1 == 1
    assert not old_shot.exists()
    assert new_shot.exists()
    assert c2 == 1
    assert not ckpt.exists()


def test_housekeeper_clean_all_workspaces(tmp_path, monkeypatch):
    fake_user_dir = tmp_path / "user"
    ws_other = fake_user_dir / "workspaces" / "other-ws"
    (ws_other / "scratch" / "outputs").mkdir(parents=True)
    old_out = ws_other / "scratch" / "outputs" / "tool_1.log"
    old_out.write_text("stale", encoding="utf-8")
    ten_days_ago = time.time() - (10 * 86400)
    os.utime(old_out, (ten_days_ago, ten_days_ago))

    cur_ws = fake_user_dir / "workspaces" / "current-ws"
    (cur_ws / "logs").mkdir(parents=True)
    (cur_ws / "scratch").mkdir(parents=True)

    monkeypatch.setattr("pocket_agent.system.housekeeping.user_dir", lambda: fake_user_dir)

    hk = Housekeeper(
        target_log_dir=cur_ws / "logs",
        target_scratch_dir=cur_ws / "scratch",
    )
    cleaned, freed = hk.clean_all_workspaces(ttl_days=7)
    assert cleaned == 1
    assert freed > 0
    assert not old_out.exists()


def test_housekeeper_run_cleanup_returns_stats_and_respects_cooldown(tmp_path, monkeypatch):
    fake_user_dir = tmp_path / "user"
    ws_dir = fake_user_dir / "workspaces" / "ws"
    log_d = ws_dir / "logs"
    scratch_d = ws_dir / "scratch"
    (scratch_d / "outputs").mkdir(parents=True)
    log_d.mkdir(parents=True)

    active_log = log_d / "agent.log"
    active_log.write_text("active", encoding="utf-8")
    fresh_out = scratch_d / "outputs" / "fresh.log"
    fresh_out.write_text("fresh", encoding="utf-8")

    monkeypatch.setattr("pocket_agent.system.housekeeping.user_dir", lambda: fake_user_dir)

    hk = Housekeeper(
        target_log_dir=log_d,
        target_scratch_dir=scratch_d,
        cwd=tmp_path,
        timestamp_file=tmp_path / "ts",
    )
    res1 = hk.run_cleanup(force=False)
    assert res1["status"] == "success"
    assert res1["cleaned_files"] == 0
    assert res1["remaining_files"] == 2
    assert res1["ttl_days"] == 7
    assert res1["log_ttl_days"] == 14

    res2 = hk.run_cleanup(force=False)
    assert res2["status"] == "cooldown"

    res3 = hk.run_cleanup(force=True)
    assert res3["status"] == "success"
    assert res3["remaining_files"] == 2


def test_cli_clean_command_output():
    renderer = Mock()
    agent = Mock()
    agent.workspace_dirs = []
    handler = CommandHandler(agent, CliState("openai", "model"), Mock(), renderer, [])

    with patch(
        "pocket_agent.system.housekeeping.default_housekeeper.run_cleanup",
        return_value={
            "status": "success",
            "cleaned_files": 0,
            "freed_bytes": 0,
            "freed_mb": 0.0,
            "remaining_files": 3,
            "remaining_mb": 0.12,
            "ttl_days": 7,
            "log_ttl_days": 14,
        },
    ):
        asyncio.run(handler.dispatch("/clean"))

    messages = [call.args[0] for call in renderer.message.call_args_list]
    assert len(messages) == 2
    assert "保留 3 個未過期或受保護檔案" in messages[0]
    assert "0.12 MB" in messages[0]
    assert "暫存 7 天／日誌 14 天" in messages[0]
    assert "/prune" in messages[1]


def test_housekeeper_clean_logs_preserves_today_daily_log(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True)

    now = time.time()
    twenty_days_ago = now - (20 * 86400)

    today_log = log_dir / f"agent_{time.strftime('%Y-%m-%d')}.log"
    today_log.write_text("today log", encoding="utf-8")
    os.utime(today_log, (twenty_days_ago, twenty_days_ago))

    old_daily_log = log_dir / "agent_2025-01-01.log"
    old_daily_log.write_text("stale daily log", encoding="utf-8")
    os.utime(old_daily_log, (twenty_days_ago, twenty_days_ago))

    hk = Housekeeper(
        target_log_dir=log_dir,
        target_scratch_dir=tmp_path / "scratch",
    )
    count, _ = hk.clean_logs(ttl_days=7)

    assert count == 1
    assert today_log.exists()
    assert not old_daily_log.exists()


def test_housekeeper_clean_backups_tiered_ttl_and_map_sync(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True)

    now = time.time()
    ten_days_ago = now - (10 * 86400)
    thirty_days_ago = now - (30 * 86400)

    # 1. 孤兒備份（10 天，超過 7 天應刪除）
    old_orphan = backup_dir / "20260101_000000_aaaa1111_deadbeef_orphan.py"
    old_orphan.write_text("old orphan", encoding="utf-8")
    os.utime(old_orphan, (ten_days_ago, ten_days_ago))

    # 2. 活躍備份（10 天，未達 28 天應保留）
    fresh_active = backup_dir / "20260101_000000_bbbb2222_deadbeef_keep.py"
    fresh_active.write_text("fresh active", encoding="utf-8")
    os.utime(fresh_active, (ten_days_ago, ten_days_ago))

    # 3. 過期活躍備份（30 天，超過 28 天應刪除並從映射表移除）
    stale_active = backup_dir / "20251201_000000_cccc3333_deadbeef_stale.py"
    stale_active.write_text("stale active", encoding="utf-8")
    os.utime(stale_active, (thirty_days_ago, thirty_days_ago))

    map_file = backup_dir / "latest_backup_map.json"
    map_file.write_text(
        json.dumps(
            {
                "/repo/keep.py": str(fresh_active),
                "/repo/stale.py": str(stale_active),
            }
        ),
        encoding="utf-8",
    )

    hk = Housekeeper(
        target_log_dir=tmp_path / "logs",
        target_scratch_dir=tmp_path / "scratch",
    )
    count, freed = hk.clean_backups(ttl_days=7, max_size_mb=200, backup_dir=backup_dir)

    assert count == 2
    assert freed > 0
    assert not old_orphan.exists()
    assert fresh_active.exists()
    assert not stale_active.exists()

    updated_map = json.loads(map_file.read_text(encoding="utf-8"))
    assert "/repo/keep.py" in updated_map
    assert "/repo/stale.py" not in updated_map
