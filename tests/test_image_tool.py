import base64
from datetime import datetime

from pocket_agent.tools import image as image_tool_mod


class _FixedDatetime(datetime):
    """固定本地時間的 datetime 替身，用於驗證時間戳記檔名格式。"""

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 16, 8, 46, 9)


def test_save_image_b64_uses_timestamp_filename(tmp_path, monkeypatch):
    monkeypatch.setattr(image_tool_mod, "datetime", _FixedDatetime)
    payload = base64.b64encode(b"fake-png-bytes").decode()

    out = image_tool_mod.save_image_b64(payload, tmp_path / "nested" / "image.png")

    assert out == (tmp_path / "nested" / "image-20261116_084609.png").resolve()
    assert out.read_bytes() == b"fake-png-bytes"


def test_save_image_b64_avoids_same_second_collision(tmp_path, monkeypatch):
    monkeypatch.setattr(image_tool_mod, "datetime", _FixedDatetime)
    first_payload = base64.b64encode(b"first").decode()
    second_payload = base64.b64encode(b"second").decode()
    third_payload = base64.b64encode(b"third").decode()
    target = tmp_path / "image.png"

    out1 = image_tool_mod.save_image_b64(first_payload, target)
    out2 = image_tool_mod.save_image_b64(second_payload, target)
    out3 = image_tool_mod.save_image_b64(third_payload, target)

    assert out1.name == "image-20261116_084609.png"
    assert out2.name == "image-20261116_084609-2.png"
    assert out3.name == "image-20261116_084609-3.png"
    assert out1.read_bytes() == b"first"
    assert out2.read_bytes() == b"second"
    assert out3.read_bytes() == b"third"


def test_save_image_b64_preserves_custom_stem_and_suffix(tmp_path, monkeypatch):
    monkeypatch.setattr(image_tool_mod, "datetime", _FixedDatetime)
    payload = base64.b64encode(b"custom").decode()

    out = image_tool_mod.save_image_b64(payload, tmp_path / "portrait.webp")

    assert out.name == "portrait-20261116_084609.webp"
    assert out.read_bytes() == b"custom"
