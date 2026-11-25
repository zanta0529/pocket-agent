import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from pocket_agent.system.housekeeping import Housekeeper
from pocket_agent.system.paths import (
    DEFAULT_SCRATCH_SESSION_ID,
    delete_session_scratch,
    get_active_session_id,
    get_session_scratch_size,
    log_dir,
    scratch_dir,
    scratch_root,
    session_scope,
    validate_session_id,
)


def test_centralized_log_and_session_scratch_dirs(tmp_path):
    with patch("pocket_agent.system.paths.user_dir", return_value=tmp_path):
        l_dir_a = log_dir(Path("/fake/repo_a"))
        l_dir_b = log_dir(Path("/fake/repo_b"))
        assert l_dir_a == tmp_path / "logs"
        assert l_dir_b == tmp_path / "logs"
        assert l_dir_a.is_dir()

        # scratch_dir 採延遲建立：未傳 create=True 時不主動建資料夾
        s_default = scratch_dir()
        assert s_default == scratch_root() / DEFAULT_SCRATCH_SESSION_ID
        assert not s_default.exists()

        s_created = scratch_dir("20261125-090000-abc123", create=True)
        assert s_created == tmp_path / "scratch" / "20261125-090000-abc123"
        assert s_created.is_dir()


def test_session_scope_and_invalid_session_id(tmp_path):
    with patch("pocket_agent.system.paths.user_dir", return_value=tmp_path):
        assert get_active_session_id() is None
        with session_scope("sess-a"):
            assert get_active_session_id() == "sess-a"
            assert scratch_dir() == tmp_path / "scratch" / "sess-a"
            with session_scope("sess-b"):
                assert get_active_session_id() == "sess-b"
                assert scratch_dir() == tmp_path / "scratch" / "sess-b"
            assert scratch_dir() == tmp_path / "scratch" / "sess-a"
        assert get_active_session_id() is None
        assert scratch_dir() == tmp_path / "scratch" / DEFAULT_SCRATCH_SESSION_ID

    for bad_id in ("", "   ", "../escape", "a/b", "a\\b", "bad id"):
        with pytest.raises(ValueError, match="無效的 Session ID 格式"):
            validate_session_id(bad_id)


def test_delete_session_scratch_and_protects_default(tmp_path):
    with patch("pocket_agent.system.paths.user_dir", return_value=tmp_path):
        default_dir = scratch_dir(DEFAULT_SCRATCH_SESSION_ID, create=True)
        (default_dir / "keep.txt").write_text("keep", encoding="utf-8")
        assert delete_session_scratch(DEFAULT_SCRATCH_SESSION_ID) == 0
        assert default_dir.exists()

        sess_dir = scratch_dir("sess-del-1", create=True)
        (sess_dir / "notes.md").write_text("hello", encoding="utf-8")
        assert get_session_scratch_size("sess-del-1") == 5

        freed = delete_session_scratch("sess-del-1")
        assert freed == 5
        assert not sess_dir.exists()
        assert delete_session_scratch("sess-del-1") == 0


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
