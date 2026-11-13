"""系統與工作區規範（instructions.md / AGENTS.md）集中載入服務。"""

import logging
import re
from pathlib import Path

from pocket_agent.core.cancellation import run_sync
from pocket_agent.system.constants import DEFAULT_MAX_LINKED_DOCS_CHARS

log = logging.getLogger("pocket-agent")


def _expand_linked_markdown(
    base_dir: Path,
    allowed_root: Path,
    source_text: str,
    used_chars: int,
    *,
    section_prefix: str,
    note_for_global: bool,
) -> tuple[str, int, bool]:
    """展開規範檔內連結的 Markdown 檔案，並受總字元上限與沙箱根目錄限制。"""
    linked_mds = re.findall(r"\[.*?\]\((.*?\.md)\)", source_text)
    seen: set[str] = set()
    extra = ""
    capped = False

    for md_rel_path in linked_mds:
        if md_rel_path in seen:
            continue
        seen.add(md_rel_path)
        try:
            raw_path = Path(md_rel_path).expanduser() if note_for_global else Path(md_rel_path)
            md_target = (base_dir / raw_path).resolve() if not raw_path.is_absolute() else raw_path.resolve()
            try:
                md_target.relative_to(allowed_root.resolve())
            except ValueError:
                log.debug("跳過展開 %s：超出允許目錄範圍 %s", md_target, allowed_root)
                continue

            if not (md_target.exists() and md_target.is_file()):
                continue
            md_content = md_target.read_text(encoding="utf-8").strip()
            if not md_content:
                continue
            if used_chars + len(md_content) > DEFAULT_MAX_LINKED_DOCS_CHARS:
                capped = True
                break
            used_chars += len(md_content)
            if note_for_global:
                extra += (
                    f"\n\n---\n## [系統提示] 已為您自動載入 {md_target.name}，"
                    f"請勿使用檔案工具嘗試讀取此路徑：\n{md_content}"
                )
            else:
                extra += f"\n\n---\n## {section_prefix}{md_target.name}\n{md_content}"
        except Exception as inner_e:
            log.debug("展開連結文件 %s 失敗：%s", md_rel_path, inner_e)

    if capped:
        extra += "\n\n（註：部分連結文件因超出啟動自動展開字元上限而略過，需要時請使用 `read_file` 按需讀取。）"
    return extra, used_chars, capped


def _load_instructions_sync(
    system_prompt: str,
    workspace_root: Path | None = None,
    user_root: Path | None = None,
) -> tuple[str, list[tuple[str, Path]]]:
    """同步讀取全域與工作區規範，回傳 (合併後 system_prompt, [(標籤, 已載入檔案路徑), ...])。"""
    from pocket_agent.system.paths import user_dir

    ws_root = (workspace_root or Path.cwd()).resolve()
    u_root = (user_root or user_dir()).resolve()
    loaded_sources: list[tuple[str, Path]] = []
    linked_docs_chars = 0

    global_ctx_file = u_root / "instructions.md"
    if global_ctx_file.exists() and global_ctx_file.is_file():
        try:
            g_ctx = global_ctx_file.read_text(encoding="utf-8").strip()
            if g_ctx:
                extra, linked_docs_chars, _ = _expand_linked_markdown(
                    global_ctx_file.parent,
                    Path.home(),
                    g_ctx,
                    linked_docs_chars,
                    section_prefix="",
                    note_for_global=True,
                )
                system_prompt = f"{system_prompt}\n\n---\n## 全域規範\n{g_ctx}{extra}"
                loaded_sources.append(("全域規範", global_ctx_file.resolve()))
        except Exception as exc:
            log.warning("全域規範讀取失敗：%s", exc)

    candidates = [
        ws_root / "AGENTS.md",
        ws_root / ".agents" / "AGENTS.md",
    ]
    ws_ctx_file = next((p for p in candidates if p.exists() and p.is_file()), None)
    if ws_ctx_file is not None:
        try:
            ws_ctx = ws_ctx_file.read_text(encoding="utf-8").strip()
            if ws_ctx:
                extra, linked_docs_chars, _ = _expand_linked_markdown(
                    ws_ctx_file.parent,
                    ws_root,
                    ws_ctx,
                    linked_docs_chars,
                    section_prefix="自動展開文件：",
                    note_for_global=False,
                )
                system_prompt = f"{system_prompt}\n\n---\n## 工作區規範\n{ws_ctx}{extra}"
                loaded_sources.append(("工作區規範", ws_ctx_file.resolve()))
        except Exception as exc:
            log.warning("工作區規範讀取失敗：%s", exc)

    return system_prompt, loaded_sources


async def load_system_instructions(
    system_prompt: str,
    workspace_root: Path | None = None,
    user_root: Path | None = None,
) -> tuple[str, list[tuple[str, Path]]]:
    """非同步載入全域與工作區規範並展開關聯 Markdown 文件。"""
    return await run_sync(_load_instructions_sync, system_prompt, workspace_root, user_root)
