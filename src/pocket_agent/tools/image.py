"""gpt-image-2 生圖 — 走 ChatGPT 訂閱後端（Codex）的 image_generation 工具。

原理：Codex 後端的 Responses API 支援 hosted image_generation 工具，
在單輪請求掛上該工具並強制 tool_choice，回傳的 image_generation_call
item 就帶有 base64 PNG。計費走 ChatGPT Plus/Pro 訂閱配額，不吃 API 餘額。

技術細節參考 opencode-gpt-imagegen 與 ima2-gen 的實作。
"""

import base64
import inspect
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from pocket_agent.core.cancellation import check_cancelled, run_sync
from pocket_agent.system.auth import get_model, get_provider_client
from pocket_agent.system.constants import MAX_IMAGE_INPUT_BYTES, MAX_IMAGE_INPUT_COUNT, PROVIDER_OPENAI
from pocket_agent.system.i18n import t
from pocket_agent.tools.fs import is_sensitive_part, validate_safe_path

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


def _prepare_output_path(output_path: str | Path) -> Path:
    """於遠端生圖或寫檔前預先驗證輸出目標路徑並確保父目錄存在。"""
    raw = str(output_path if output_path is not None else "").strip()
    if not raw or raw.startswith(("\\\\", "//")):
        raise ValueError(t("image_tool.invalid_output_path"))

    resolved = Path(raw).expanduser().resolve(strict=False)
    for part in resolved.parts:
        if is_sensitive_part(part):
            raise ValueError(t("image_tool.sensitive_output_path", part=part))

    if resolved.exists() and resolved.is_dir():
        raise ValueError(
            t(
                "image_tool.output_is_dir",
                example=resolved / "image.png",
                path=resolved,
            )
        )

    for parent in resolved.parents:
        if parent.exists():
            if not parent.is_dir():
                raise ValueError(t("image_tool.parent_not_dir", parent=parent))
            break

    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(t("image_tool.mkdir_failed", parent=resolved.parent, error=exc)) from exc

    return resolved


def _iter_candidates(path: Path) -> Iterator[Path]:
    """依本地時間戳記（yyyyMMdd_hhmmss）依序產生候選路徑（-2、-3…）。"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name = f"{path.stem}-{ts}"
    yield path.with_name(f"{base_name}{path.suffix}")
    n = 2
    while True:
        yield path.with_name(f"{base_name}-{n}{path.suffix}")
        n += 1


def save_image_b64(b64_png: str, output_path: str | Path = "generated_images/image.png") -> Path:
    """base64 PNG 原子存檔（自動加上 yyyyMMdd_hhmmss 時間戳記並避開既有檔名），回傳實際路徑。"""
    raw_bytes = base64.b64decode(b64_png)
    target = _prepare_output_path(output_path)
    for candidate in _iter_candidates(target):
        try:
            with candidate.open("xb") as fh:
                fh.write(raw_bytes)
            return candidate
        except FileExistsError:
            continue
        except BaseException:
            candidate.unlink(missing_ok=True)
            raise
    raise RuntimeError(t("image_tool.write_failed"))


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
    p = validate_safe_path(str(path), workspace_dirs)
    if not p.exists():
        raise FileNotFoundError(t("image_tool.ref_not_found", path=p))
    if not p.is_file():
        raise ValueError(t("image_tool.ref_not_regular", name=p.name))

    ext = p.suffix.lower()
    mime = _SUPPORTED_IMAGE_MIMES.get(ext)
    if not mime:
        raise ValueError(t("image_tool.ref_unsupported_format", name=p.name))

    size_bytes = p.stat().st_size
    if size_bytes <= 0:
        raise ValueError(t("image_tool.ref_empty", name=p.name))
    if size_bytes > MAX_IMAGE_INPUT_BYTES:
        limit_mb = MAX_IMAGE_INPUT_BYTES // (1024 * 1024)
        raise ValueError(t("image_tool.ref_too_large", name=p.name, limit_mb=limit_mb))
    if not _has_valid_image_header(p, ext):
        raise ValueError(t("image_tool.ref_header_mismatch", name=p.name, ext=ext))

    b64 = base64.b64encode(p.read_bytes()).decode()
    return {"type": "input_image", "image_url": f"data:{mime};base64,{b64}"}


def next_available_path(path: Path) -> Path:
    """加上本地時間戳記（yyyyMMdd_hhmmss），同秒碰撞時附加 -2、-3 後綴避免覆蓋。"""
    for candidate in _iter_candidates(path):
        if not candidate.exists():
            return candidate
    raise RuntimeError(t("image_tool.no_available_path"))


# 向後相容別名
_next_available = next_available_path


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
        raise ValueError(t("image_tool.invalid_size", valid=sorted(VALID_SIZES), size=size))
    if quality not in VALID_QUALITIES:
        raise ValueError(t("image_tool.invalid_quality", valid=sorted(VALID_QUALITIES), quality=quality))

    imgs = list(input_images or [])
    if len(imgs) > MAX_IMAGE_INPUT_COUNT:
        raise ValueError(t("image_tool.too_many_refs", max_count=MAX_IMAGE_INPUT_COUNT))

    # 送出網路請求前先預檢輸出目的地並整批驗證參考圖
    target_output = _prepare_output_path(output_path)
    content: list[dict] = [{"type": "input_text", "text": prompt}]
    for img in imgs:
        content.append(_image_part(img, workspace_dirs=workspace_dirs))

    client = await run_sync(get_provider_client, PROVIDER_OPENAI)
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

    try:
        return save_image_b64(b64_png, target_output)
    except OSError as exc:
        from pocket_agent.system.paths import scratch_dir

        try:
            rescue_target = scratch_dir() / "generated_images" / target_output.name
            rescued = save_image_b64(b64_png, rescue_target)
        except Exception:
            raise exc
        raise RuntimeError(
            t(
                "image_tool.rescued_to_scratch",
                target=target_output,
                error=exc,
                rescued=rescued,
            )
        ) from exc


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
            disable_model_param()
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
    incomplete_reason: str | None = None

    try:
        async for event in stream:
            check_cancelled()
            etype = getattr(event, "type", "")
            if etype == "response.output_item.done":
                item = getattr(event, "item", None)
                if item and getattr(item, "type", "") == "image_generation_call":
                    result = getattr(item, "result", None)
            elif etype in ("error", "response.failed"):
                err_obj = getattr(event, "error", None) or getattr(getattr(event, "response", None), "error", None)
                error_msg = str(
                    getattr(event, "message", None) or getattr(err_obj, "message", None) or err_obj or "unknown error"
                )
            elif etype == "response.incomplete":
                resp_obj = getattr(event, "response", None)
                details = getattr(resp_obj, "incomplete_details", None)
                reason = getattr(details, "reason", None) or details or "incomplete"
                incomplete_reason = str(reason)
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
        raise RuntimeError(t("image_tool.gen_failed", error=error_msg))
    if incomplete_reason:
        raise RuntimeError(t("image_tool.gen_incomplete", reason=incomplete_reason))
    if not result:
        raise RuntimeError(t("image_tool.gen_empty_result"))
    return result
