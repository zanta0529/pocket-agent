"""gpt-image-2 生圖 — 走 ChatGPT 訂閱後端（Codex）的 image_generation 工具。

原理：Codex 後端的 Responses API 支援 hosted image_generation 工具，
在單輪請求掛上該工具並強制 tool_choice，回傳的 image_generation_call
item 就帶有 base64 PNG。計費走 ChatGPT Plus/Pro 訂閱配額，不吃 API 餘額。

技術細節參考 opencode-gpt-imagegen 與 ima2-gen 的實作。
"""

import base64
import mimetypes
from pathlib import Path

from pocket_agent.system.auth import get_model, get_openai_client

# gpt-image-2 支援的參數
VALID_SIZES = {"auto", "1024x1024", "1536x1024", "1024x1536"}
VALID_QUALITIES = {"auto", "low", "medium", "high"}

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


def _image_part(path: str | Path) -> dict:
    """本地圖片 → Responses API 的 input_image part（base64 data URL）。"""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"找不到圖片：{p}")
    mime = mimetypes.guess_type(str(p))[0] or "image/png"
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
) -> Path:
    """生成（或用 input_images 編輯）圖片，存成 PNG，回傳實際存檔路徑。"""
    if size not in VALID_SIZES:
        raise ValueError(f"size 必須是 {sorted(VALID_SIZES)}，收到：{size}")
    if quality not in VALID_QUALITIES:
        raise ValueError(f"quality 必須是 {sorted(VALID_QUALITIES)}，收到：{quality}")

    client = get_openai_client(provider="openai")
    from pocket_agent.system.config import config

    wrapper_model = config.get("openai_image_wrapper_model") or get_model()

    content: list[dict] = [{"type": "input_text", "text": prompt}]
    for img in input_images or []:
        content.append(_image_part(img))

    try:
        b64_png = await _request(client, wrapper_model, content, image_tool(size, quality))
    except Exception as e:
        # 後端不吃 tool 的 model 參數時，退回預設圖片模型再試一次
        if _model_param_ok and ("model" in str(e).lower() or "unknown" in str(e).lower()):
            disable_model_param()
            b64_png = await _request(client, wrapper_model, content, image_tool(size, quality))
        else:
            raise

    return save_image_b64(b64_png, output_path)


async def _request(client, model: str, content: list[dict], tool: dict) -> str:
    """發出單輪 Responses API 請求，回傳 base64 PNG。"""
    stream = await client.responses.create(
        model=model,
        instructions=_INSTRUCTIONS,
        input=[{"role": "user", "content": content}],
        tools=[tool],
        tool_choice={"type": "image_generation"},
        store=False,
        stream=True,
    )

    result: str | None = None
    error_msg: str | None = None

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

    if error_msg:
        raise RuntimeError(f"生圖失敗：{error_msg}")
    if not result:
        raise RuntimeError("生圖失敗：後端沒有回傳圖片資料")
    return result
