import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from pocket_agent.core.cancellation import CancellationScope, current_scope
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


def test_save_image_b64_parallel_atomic_no_overwrite(tmp_path, monkeypatch):
    """CR-003：多執行緒同秒並行寫入同一目標時，應透過 xb 排他建檔各自落盤，互不覆寫。"""
    monkeypatch.setattr(image_tool_mod, "datetime", _FixedDatetime)
    target = tmp_path / "parallel.png"
    payloads = [base64.b64encode(f"img-{i}".encode()).decode() for i in range(8)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(lambda p: image_tool_mod.save_image_b64(p, target), payloads))

    assert len(set(paths)) == 8
    saved_contents = {p.read_bytes().decode() for p in paths}
    assert saved_contents == {f"img-{i}" for i in range(8)}


def test_request_rejects_response_incomplete_even_after_image_item(monkeypatch):
    """CR-002：串流若在產生圖片後接著回傳 response.incomplete，仍須視為未完成並關閉串流。"""
    b64_data = base64.b64encode(b"partial-or-unverified").decode()
    events = [
        SimpleNamespace(
            type="response.output_item.done",
            item=SimpleNamespace(type="image_generation_call", result=b64_data),
        ),
        SimpleNamespace(
            type="response.incomplete",
            response=SimpleNamespace(incomplete_details=SimpleNamespace(reason="content_filter")),
        ),
    ]
    closed = []

    class _FakeStream:
        def __aiter__(self):
            async def _gen():
                for ev in events:
                    yield ev

            return _gen()

        async def close(self):
            closed.append(True)

    client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=_FakeStream())))

    with pytest.raises(RuntimeError, match="生圖未完成 \\(incomplete\\)：content_filter"):
        asyncio.run(image_tool_mod._request(client, "gpt-5", [], image_tool_mod.image_tool()))

    assert closed == [True]


def test_request_model_param_fallback_disables_global_flag(monkeypatch):
    """驗證 _request 遇到未知 tools[0].model 參數重試時，會同步呼叫 disable_model_param()。"""
    monkeypatch.setattr(image_tool_mod, "_model_param_ok", True)
    b64_data = base64.b64encode(b"ok-png").decode()

    err = Exception("Unknown parameter: 'tools[0].model'")
    err.status_code = 400
    err.code = "unknown_parameter"
    err.param = "tools[0].model"

    class _OkStream:
        def __aiter__(self):
            async def _gen():
                yield SimpleNamespace(
                    type="response.output_item.done",
                    item=SimpleNamespace(type="image_generation_call", result=b64_data),
                )

            return _gen()

        async def close(self):
            pass

    client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(side_effect=[err, _OkStream()])))
    res = asyncio.run(image_tool_mod._request(client, "gpt-5", [], image_tool_mod.image_tool()))
    assert res == b64_data
    assert image_tool_mod._model_param_ok is False


def test_generate_image_preflight_blocks_invalid_output_before_network(tmp_path):
    """CR-004：輸出路徑之父目錄為一般檔案或目標本身為既有資料夾時，應在取得 Client 前直接攔截。"""
    file_as_parent = tmp_path / "regular_file.txt"
    file_as_parent.write_text("not a dir", encoding="utf-8")

    with patch("pocket_agent.tools.image.get_provider_client") as mock_client:
        with pytest.raises(ValueError, match="父路徑不是資料夾"):
            asyncio.run(image_tool_mod.generate_image("cat", output_path=file_as_parent / "out.png"))
        with pytest.raises(ValueError, match="不能是資料夾"):
            asyncio.run(image_tool_mod.generate_image("cat", output_path=tmp_path))
        mock_client.assert_not_called()


def test_generate_image_rescues_to_scratch_when_final_save_fails(tmp_path, monkeypatch):
    """CR-004：遠端生圖成功後若目標寫入遭遇 OSError，應自動救援落盤至沙盒暫存區並回報路徑。"""
    monkeypatch.setattr(image_tool_mod, "datetime", _FixedDatetime)
    b64_data = base64.b64encode(b"rescued-image-bytes").decode()
    fake_client = SimpleNamespace(close=AsyncMock())
    real_save = image_tool_mod.save_image_b64
    call_count = 0

    def flaky_save(b64_png, out_path):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise PermissionError("Target disk read-only")
        return real_save(b64_png, out_path)

    with (
        patch("pocket_agent.tools.image.get_provider_client", return_value=fake_client),
        patch("pocket_agent.tools.image._request", new=AsyncMock(return_value=b64_data)),
        patch("pocket_agent.tools.image.save_image_b64", side_effect=flaky_save),
        patch("pocket_agent.system.paths.scratch_dir", return_value=tmp_path / "scratch"),
    ):
        with pytest.raises(RuntimeError, match="緊急保存至沙盒暫存區") as exc_info:
            asyncio.run(image_tool_mod.generate_image("cat", output_path=tmp_path / "out.png"))

    fake_client.close.assert_awaited_once()
    rescued_file = tmp_path / "scratch" / "generated_images" / "out-20261116_084609.png"
    assert str(rescued_file) in str(exc_info.value)
    assert rescued_file.read_bytes() == b"rescued-image-bytes"


def test_get_access_token_respects_active_cancellation_scope():
    """CR-001：處於已取消之 CancellationScope 時，get_access_token 應立即中斷，不啟動瀏覽器 OAuth。"""
    from pocket_agent.system.auth import get_access_token

    scope = CancellationScope()
    scope.cancel()
    token = current_scope.set(scope)
    try:
        with (
            patch("pocket_agent.system.auth._load_token", return_value=None),
            patch("pocket_agent.system.auth._browser_oauth") as mock_browser,
        ):
            with pytest.raises(RuntimeError, match="登入已取消"):
                get_access_token(provider="openai")
            mock_browser.assert_not_called()
    finally:
        current_scope.reset(token)


def test_image_output_path_blocks_sensitive_names_and_supports_i18n(tmp_path):
    """驗證 save_image_b64 / _prepare_output_path 阻擋敏感路徑 (.env / .git) 並支援雙語系錯誤訊息。"""
    from pocket_agent.system.i18n import set_locale

    payload = base64.b64encode(b"fake-png-bytes").decode()

    set_locale("zh_TW")
    with pytest.raises(ValueError, match="安全限制：禁止將圖片輸出至敏感目錄或檔案"):
        image_tool_mod.save_image_b64(payload, tmp_path / ".env" / "out.png")

    set_locale("en_US")
    with pytest.raises(ValueError, match="Security restriction: cannot write image to sensitive directory"):
        image_tool_mod.save_image_b64(payload, tmp_path / ".git" / "out.png")
    with pytest.raises(ValueError, match="Output path cannot be a directory"):
        image_tool_mod._prepare_output_path(tmp_path)
