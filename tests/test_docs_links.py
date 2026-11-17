"""離線 Markdown 斷鏈與標題錨點檢查測試。"""

import re
import unicodedata
from pathlib import Path
from urllib.parse import unquote

REPO_ROOT = Path(__file__).resolve().parent.parent
_LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")


def _iter_doc_files() -> list[Path]:
    files: list[Path] = []
    for name in ("README.md", "README.zh-TW.md"):
        p = REPO_ROOT / name
        if p.is_file():
            files.append(p)
    docs_dir = REPO_ROOT / "docs"
    if docs_dir.is_dir():
        files.extend(sorted(docs_dir.rglob("*.md")))
    return files


def _strip_inline_markdown(text: str) -> str:
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = text.replace("`", "")
    return text.strip()


def _github_slug(heading: str, *, keep_variation_selector: bool = True) -> str:
    clean = _strip_inline_markdown(heading).lower()
    out: list[str] = []
    for ch in clean:
        if ch == " ":
            out.append("-")
        elif ch in ("-", "_"):
            out.append(ch)
        elif ch == "\ufe0f" and keep_variation_selector:
            out.append(ch)
        else:
            cat = unicodedata.category(ch)
            if cat.startswith(("L", "N")):
                out.append(ch)
    return "".join(out)


def _extract_heading_anchors(md_text: str) -> set[str]:
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    in_fence = False
    fence_char = ""
    fence_len = 0

    for line in md_text.splitlines():
        fence_match = _FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if not in_fence:
                in_fence = True
                fence_char = marker[0]
                fence_len = len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_len:
                in_fence = False
            continue
        if in_fence:
            continue

        m = _HEADING_RE.match(line)
        if not m:
            continue
        heading_raw = m.group(2)
        base_keep = _github_slug(heading_raw, keep_variation_selector=True)
        idx = counts.get(base_keep, 0)
        counts[base_keep] = idx + 1
        slug = base_keep if idx == 0 else f"{base_keep}-{idx}"
        anchors.add(slug)
        anchors.add(slug.replace("\ufe0f", ""))
    return anchors


def _iter_markdown_links(md_text: str) -> list[tuple[int, str]]:
    results: list[tuple[int, str]] = []
    in_fence = False
    fence_char = ""
    fence_len = 0

    for lineno, line in enumerate(md_text.splitlines(), 1):
        fence_match = _FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if not in_fence:
                in_fence = True
                fence_char = marker[0]
                fence_len = len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_len:
                in_fence = False
            continue
        if in_fence:
            continue

        # 略過行內程式碼區塊內的範例連結
        stripped_line = re.sub(r"`[^`]*`", "", line)
        for match in _LINK_RE.finditer(stripped_line):
            target = match.group(1).strip()
            # 去除可選的 title，例如 (path.md "title")
            if target.startswith("<") and ">" in target:
                target = target[1 : target.index(">")].strip()
            elif " " in target:
                target = target.split(" ", 1)[0].strip()
            results.append((lineno, target))
    return results


def test_markdown_internal_links_and_anchors():
    """驗證 README.md、README.zh-TW.md 與 docs/**/*.md 的本機連結與標題錨點皆有效。"""
    doc_files = _iter_doc_files()
    assert doc_files, "Expected documentation markdown files to exist"

    anchor_cache: dict[Path, set[str]] = {}
    errors: list[str] = []

    for doc_path in doc_files:
        text = doc_path.read_text(encoding="utf-8")
        rel_doc = doc_path.relative_to(REPO_ROOT).as_posix()

        for lineno, raw_target in _iter_markdown_links(text):
            if not raw_target or raw_target.startswith(("http://", "https://", "mailto:")):
                continue

            if raw_target.startswith("#"):
                target_file = doc_path
                anchor = unquote(raw_target[1:])
            else:
                path_part, _, anchor_part = raw_target.partition("#")
                target_file = (doc_path.parent / unquote(path_part)).resolve()
                anchor = unquote(anchor_part) if anchor_part else ""

                if not target_file.is_relative_to(REPO_ROOT.resolve()):
                    errors.append(f"{rel_doc}:{lineno}: link escapes repo root -> {raw_target}")
                    continue
                if not target_file.exists():
                    errors.append(f"{rel_doc}:{lineno}: broken file link -> {raw_target}")
                    continue

            if anchor and target_file.suffix.lower() == ".md":
                if target_file not in anchor_cache:
                    anchor_cache[target_file] = _extract_heading_anchors(
                        target_file.read_text(encoding="utf-8"),
                    )
                norm_anchor = anchor.lower().replace("\ufe0f", "")
                if anchor.lower() not in anchor_cache[target_file] and norm_anchor not in anchor_cache[target_file]:
                    errors.append(f"{rel_doc}:{lineno}: broken anchor -> {raw_target}")

    assert not errors, "Broken documentation links found:\n" + "\n".join(errors)
