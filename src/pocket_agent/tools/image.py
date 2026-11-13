"""gpt-image-2 生圖 — 走 ChatGPT 訂閱後端（Codex）的 image_generation 工具。

原理：Codex 後端的 Responses API 支援 hosted image_generation 工具，
在單輪請求掛上該工具並強制 tool_choice，回傳的 image_generation_call
item 就帶有 base64 PNG。計費走 ChatGPT Plus/Pro 訂閱配額，不吃 API 餘額。

技術細節參考 opencode-gpt-imagegen 與 ima2-gen 的實作。
"""

import base64
import inspect
from pathlib import Path

from pocket_agent.system.auth import get_model, get_provider_client
from pocket_agent.system.constants import MAX_IMAGE_INPUT_BYTES, MAX_IMAGE_INPUT_COUNT

# gpt-image-2 支援的參數
VALID_SIZES = {"auto", "1024x1024", "1536x1024", "1024x1536"}
VALID_QUALITIES = {"auto", "low", "medium", "high"}

_SUPPORTED_IMAGE_MIMES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

IMAGE_MODEL = "gpt-image-2"

# 生圖是掛在文字模型上的 hosted tool，這裡的 model 只是「載具」
_WRAPPER_MODEL_ENV = "OPENAI_IMAGE_WRAPPER_MODEL"

_INSTRUCTIONS = (
    "You are an image generation assistant. Use the image_generation tool to create exactly what the user asks for."
)

# 後端若拒絕工具內的 model 參數，全域關掉、之後的請求都不再帶
_model_param_ok = True


def image_tool(size: str = "auto", quality: str = "auto") -> dict:
    """組出 hosted image_generation 工具定義（聊天迴圈與單次生圖共用）。"""
    tool = {
        "type": "image_generation",
        "output_format": "png",
        "size": size,
        "quality": quality,
    }
    if _model_param_ok:
        tool["model"] = IMAGE_MODEL
    return tool


def disable_model_param() -> None:
    global _model_param_ok
    _model_param_ok = False


def save_image_b64(b64_png: str, output_path: str | Path = "generated_images/image.png") -> Path:
    """base64 PNG 存檔（自動避開既有檔名），回傳實際路徑。"""
    out = _next_available(Path(output_path).expanduser().resolve())
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(base64.b64decode(b64_png))
    return out


def _has_valid_image_header(path: Path, ext: str) -> bool:
    """檢查檔案開頭魔術位元組是否符合對應圖片格式，避免非圖片檔偽裝送出。"""
    with path.open("rb") as fh:
        header = fh.read(16)
    if ext == ".png":
        return header.startswith(b"\x89PNG\r\n\x1a\n")
    if ext in (".jpg", ".jpeg"):
        return header.startswith(b"\xff\xd8\xff")
    if ext == ".gif":
        return header.startswith((b"GIF87a", b"GIF89a"))
    if ext == ".webp":
        return len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP"
    return False


def _image_part(path: str | Path, workspace_dirs: list[str] | None = None) -> dict:
    """本地圖片 → Responses API 的 input_image part（base64 data URL）。"""
    from pocket_agent.tools.fs import _validate_safe_path

    p = _validate_safe_path(str(path), workspace_dirs)
    if not p.exists():
        raise FileNotFoundError(f"找不到圖片：{p}")
    if not p.is_file():
        raise ValueError(f"參考圖路徑不是一般檔案（請先從待送附件移除）：{p.name}")

    ext = p.suffix.lower()
    mime = _SUPPORTED_IMAGE_MIMES.get(ext)
    if not mime:
        raise ValueError(f"不支援的參考圖格式「{p.name}」（僅支援 PNG、JPEG、WEBP、GIF，請先從待送附件移除）。")

    size_bytes = p.stat().st_size
    if size_bytes <= 0:
        raise ValueError(f"參考圖檔案為空「{p.name}」，請先從待送附件移除。")
    if size_bytes > MAX_IMAGE_INPUT_BYTES:
        limit_mb = MAX_IMAGE_INPUT_BYTES // (1024 * 1024)
        raise ValueError(f"參考圖「{p.name}」超過大小上限（{limit_mb} MB），請先移除或壓縮後再試。")
    if not _has_valid_image_header(p, ext):
        raise ValueError(f"檔案「{p.name}」內容與副檔名 {ext} 格式不符，請確認為有效圖片後再試。")

    b64 = base64.b64encode(p.read_bytes()).decode()
    return {"type": "input_image", "image_url": f"data:{mime};base64,{b64}"}


def _next_available(path: Path) -> Path:
    """避免覆蓋：存在時自動加 -v2、-v3 後綴。"""
    if not path.exists():
        return path
    n = 2
    while True:
        candidate = path.with_name(f"{path.stem}-v{n}{path.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


async def generate_image(
    prompt: str,
    output_path: str | Path = "image.png",
    input_images: list[str] | None = None,
    size: str = "auto",
    quality: str = "auto",
    workspace_dirs: list[str] | None = None,
) -> Path:
    """生成（或用 input_images 編輯）圖片，存成 PNG，回傳實際存檔路徑。"""
    if size not in VALID_SIZES:
        raise ValueError(f"size 必須是 {sorted(VALID_SIZES)}，收到：{size}")
    if quality not in VALID_QUALITIES:
        raise ValueError(f"quality 必須是 {sorted(VALID_QUALITIES)}，收到：{quality}")

    imgs = list(input_images or [])
    if len(imgs) > MAX_IMAGE_INPUT_COUNT:
        raise ValueError(f"參考圖數量超過上限（最多 {MAX_IMAGE_INPUT_COUNT} 張），請先移除多餘附件。")

    # 送出網路請求前先整批驗證參考圖並轉為 input_image
    content: list[dict] = [{"type": "input_text", "text": prompt}]
    for img in imgs:
        content.append(_image_part(img, workspace_dirs=workspace_dirs))

    client = get_provider_client(provider="openai")
    from pocket_agent.system.config import config

    wrapper_model = config.get("providers.openai.image_wrapper_model") or get_model()
    tool = image_tool(size, quality)

    try:
        b64_png = await _request(client, wrapper_model, content, tool)
    finally:
        close_fn = getattr(client, "close", None)
        if callable(close_fn):
            try:
                res = close_fn()
                if inspect.isawaitable(res):
                    await res
            except Exception:
                pass

    return save_image_b64(b64_png, output_path)


async def _request(client, model: str, content: list[dict], tool: dict) -> str:
    """發出單輪 Responses API 請求，回傳 base64 PNG。"""
    from pocket_agent.engines.openai_engine import OpenAIResponsesEngine

    try:
        stream = await client.responses.create(
            model=model,
            instructions=_INSTRUCTIONS,
            input=[{"role": "user", "content": content}],
            tools=[tool],
            tool_choice={"type": "image_generation"},
            store=False,
            stream=True,
        )
    except Exception as exc:
        if OpenAIResponsesEngine._is_recoverable_image_tool_model_error(exc, [tool]):
            retry_tool = {k: v for k, v in tool.items() if k != "model"}
            stream = await client.responses.create(
                model=model,
                instructions=_INSTRUCTIONS,
                input=[{"role": "user", "content": content}],
                tools=[retry_tool],
                tool_choice={"type": "image_generation"},
                store=False,
                stream=True,
            )
        else:
            raise

    result: str | None = None
    error_msg: str | None = None

    try:
        async for event in stream:
            etype = getattr(event, "type", "")
            if etype == "response.output_item.done":
                item = getattr(event, "item", None)
                if item and getattr(item, "type", "") == "image_generation_call":
                    result = getattr(item, "result", None)
            elif etype in ("error", "response.failed"):
                error_msg = str(
                    getattr(event, "message", None)
                    or getattr(getattr(event, "response", None), "error", None)
                    or "unknown error"
                )
    finally:
        close_fn = getattr(stream, "close", None)
        if callable(close_fn):
            try:
                res = close_fn()
                if inspect.isawaitable(res):
                    await res
            except Exception:
                pass

    if error_msg:
        raise RuntimeError(f"生圖失敗：{error_msg}")
    if not result:
        raise RuntimeError("生圖失敗：後端沒有回傳圖片資料")
    return result
