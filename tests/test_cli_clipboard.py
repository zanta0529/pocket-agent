import time
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from pocket_agent.frontend.cli.clipboard import (
    get_clipboard_attachments,
    save_clipboard_image,
)
from pocket_agent.system.housekeeping import Housekeeper


def test_save_clipboard_image(tmp_path):
    # 建立 50x50 測試圖像
    test_img = Image.new("RGB", (50, 50), color="red")

    with patch("pocket_agent.frontend.cli.clipboard.scratch_dir", return_value=tmp_path):
        saved_path = save_clipboard_image(test_img)

        assert saved_path.exists()
        assert saved_path.suffix == ".png"
        assert saved_path.name.startswith("screenshot_")
        assert saved_path.parent == tmp_path / "clipboard"

        # 驗證檔案確實為有效 PNG
        with Image.open(saved_path) as opened:
            assert opened.size == (50, 50)

        # 驗證重複儲存相同圖像時會重用既有檔案
        second_path = save_clipboard_image(test_img)
        assert second_path == saved_path
        all_files = list((tmp_path / "clipboard").glob("*.png"))
        assert len(all_files) == 1


def test_get_clipboard_attachments_with_image(tmp_path):
    test_img = Image.new("RGBA", (20, 20), color="blue")

    with (
        patch("pocket_agent.frontend.cli.clipboard.scratch_dir", return_value=tmp_path),
        patch("platform.system", return_value="Linux"),
        patch("pocket_agent.frontend.cli.clipboard.get_clipboard_image", return_value=test_img),
    ):
        paths, is_screenshot = get_clipboard_attachments()
        assert is_screenshot is True
        assert len(paths) == 1
        assert Path(paths[0]).exists()
        assert Path(paths[0]).name.startswith("screenshot_")


def test_get_clipboard_attachments_with_text_path(tmp_path):
    sample_file = tmp_path / "doc.txt"
    sample_file.write_text("hello", encoding="utf-8")

    with (
        patch("platform.system", return_value="Darwin"),
        patch("pocket_agent.frontend.cli.clipboard.get_clipboard_image", return_value=None),
        patch("subprocess.run") as mock_run,
    ):
        mock_run.return_value.stdout = str(sample_file)
        paths, is_screenshot = get_clipboard_attachments()
        assert is_screenshot is False
        assert paths == [str(sample_file.resolve())]


def test_get_clipboard_attachments_with_empty_or_non_file_text():
    with (
        patch("platform.system", return_value="Darwin"),
        patch("pocket_agent.frontend.cli.clipboard.get_clipboard_image", return_value=None),
        patch("subprocess.run") as mock_run,
    ):
        mock_run.return_value.stdout = "This is just a normal message without any valid path."
        paths, is_screenshot = get_clipboard_attachments()
        assert is_screenshot is False
        assert paths == []


def test_housekeeper_clean_scratch_clipboard(tmp_path):
    clipboard_dir = tmp_path / "clipboard"
    clipboard_dir.mkdir(parents=True, exist_ok=True)

    # 建立 3 個暫存截圖檔案
    f1 = clipboard_dir / "screenshot_old_aaa.png"
    f2 = clipboard_dir / "screenshot_mid_bbb.png"
    f3 = clipboard_dir / "screenshot_new_ccc.png"

    for f in (f1, f2, f3):
        f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"dummy")

    now = time.time()
    # f1: 10 天前 (過期)
    import os

    os.utime(f1, (now - 10 * 86400, now - 10 * 86400))
    # f2: 3 天前
    os.utime(f2, (now - 3 * 86400, now - 3 * 86400))
    # f3: 剛剛
    os.utime(f3, (now, now))

    hk = Housekeeper(target_scratch_dir=tmp_path)
    # TTL 7 天，max_files 10
    deleted, freed = hk.clean_scratch_clipboard(ttl_days=7, max_files=10)
    assert deleted == 1
    assert not f1.exists()
    assert f2.exists()
    assert f3.exists()

    # 測試 max_files 限制 (設為 1，應刪除較舊的 f2)
    deleted2, _ = hk.clean_scratch_clipboard(ttl_days=30, max_files=1)
    assert deleted2 == 1
    assert not f2.exists()
    assert f3.exists()
