import os
import time
from pathlib import Path
from unittest.mock import patch

from pocket_agent.system.housekeeping import Housekeeper
from pocket_agent.system.paths import (
    get_workspace_slug,
    log_dir,
    scratch_dir,
    workspace_dir,
)


def test_get_workspace_slug_windows():
    # 模擬 Windows 磁碟代號與路徑
    slug1 = get_workspace_slug(r"C:\Users\test\project")
    assert slug1 == "C--Users-test-project"

    slug_root = get_workspace_slug("D:\\")
    assert slug_root == "D--"

    slug_spaces = get_workspace_slug(r"E:\My Projects\app (v2)")
    assert slug_spaces == "E--My-Projects-app-v2"


def test_get_workspace_slug_posix():
    slug = get_workspace_slug("/home/user/projects/web-app")
    assert slug == "-home-user-projects-web-app"


def test_get_workspace_slug_long_path_truncated_with_hash():
    long_path = "/home/user/" + ("very_long_nested_directory_name/" * 5) + "project"
    slug = get_workspace_slug(long_path)
    # 驗證長度在安全範圍 (<= 60)
    assert len(slug) <= 60
    # 驗證包含底線與 8 碼雜湊後綴
    assert "_" in slug
    suffix = slug.split("_")[-1]
    assert len(suffix) == 8


def test_centralized_workspace_dirs(tmp_path):
    with patch("pocket_agent.system.paths.user_dir", return_value=tmp_path):
        target_cwd = Path("/fake/test/repo")
        ws = workspace_dir(target_cwd)
        assert ws.parent == tmp_path / "workspaces"

        s_dir = scratch_dir(target_cwd)
        assert s_dir == ws / "scratch"
        assert s_dir.exists()

        l_dir = log_dir(target_cwd)
        assert l_dir == ws / "logs"
        assert l_dir.exists()


def test_housekeeper_clean_all_workspaces(tmp_path):
    with patch("pocket_agent.system.housekeeping.user_dir", return_value=tmp_path):
        ws_root = tmp_path / "workspaces"
        ws1_scratch = ws_root / "ws1" / "scratch" / "outputs"
        ws1_scratch.mkdir(parents=True)
        ws2_scratch = ws_root / "ws2" / "scratch" / "outputs"
        ws2_scratch.mkdir(parents=True)

        now = time.time()
        ten_days_ago = now - (10 * 86400)

        # ws1 包含 10 天前的過期落盤日誌
        expired_log = ws1_scratch / "old_out.log"
        expired_log.write_text("old", encoding="utf-8")
        os.utime(expired_log, (ten_days_ago, ten_days_ago))

        # ws2 包含當前的新日誌
        fresh_log = ws2_scratch / "fresh_out.log"
        fresh_log.write_text("fresh", encoding="utf-8")
        os.utime(fresh_log, (now, now))

        # 模擬從第三個工作區 (current_ws) 執行 Housekeeper 進行全域掃描
        current_scratch = ws_root / "current_ws" / "scratch"
        current_scratch.mkdir(parents=True)
        hk = Housekeeper(
            target_log_dir=ws_root / "current_ws" / "logs",
            target_scratch_dir=current_scratch,
            cwd=Path("/fake/current"),
        )

        cleaned, freed = hk.clean_all_workspaces(ttl_days=7)
        assert cleaned == 1
        assert freed > 0
        assert not expired_log.exists()
        assert fresh_log.exists()
