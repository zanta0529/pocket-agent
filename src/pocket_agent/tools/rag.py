"""統一工作區多關鍵字相關度檢索模組（共用 WorkspaceSearchIndex 單一索引引擎）。"""

import logging
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from pocket_agent.core.cancellation import check_cancelled
from pocket_agent.system.constants import (
    DEFAULT_RAG_TOP_K,
    MAX_RAG_TOP_K,
    RAG_SNIPPET_MAX_CHARS,
    RAG_SNIPPET_MAX_LINES,
    SEARCH_FTS_THRESHOLD_FILES,
)
from pocket_agent.system.i18n import t
from pocket_agent.system.paths import format_display_path
from pocket_agent.tools.fs import (
    iter_workspace_files,
    read_text_file_safe,
    resolve_workspace_roots,
    wrap_external_content,
)
from pocket_agent.tools.search_index import ScannedFile, SearchCandidateIndex, default_search_index

log = logging.getLogger("pocket-agent")

_TERM_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+")


def _resolve_workspace_roots(workspace_dirs: list[str] | None) -> list[Path]:
    return resolve_workspace_roots(workspace_dirs)


def _extract_query_terms(query: str) -> list[str]:
    """抽取中英文檢索詞彙並依大小寫不敏感去重，保留原始順序。"""
    terms: list[str] = []
    seen: set[str] = set()
    for match in _TERM_RE.finditer(query):
        tok = match.group(0)
        low = tok.lower()
        if low not in seen:
            seen.add(low)
            terms.append(tok)
    return terms


class _RagCandidateStrategy(ABC):
    """RAG 候選檔案篩選策略介面（Strategy）。"""

    @abstractmethod
    def select_candidates(
        self,
        scanned_files: list[ScannedFile],
        roots: list[Path],
        terms: list[str],
    ) -> list[ScannedFile]:
        """依策略回傳待進入內容計分之候選檔案清單。"""


class _DirectScanCandidateStrategy(_RagCandidateStrategy):
    """純記憶體直接掃描策略（適用敏感會話、小型工作區或短詞查詢）。"""

    def select_candidates(
        self,
        scanned_files: list[ScannedFile],
        roots: list[Path],
        terms: list[str],
    ) -> list[ScannedFile]:
        _ = (roots, terms)
        return scanned_files


class _FtsTrigramCandidateStrategy(_RagCandidateStrategy):
    """委派至統一 FTS5 Trigram 索引引擎進行 Stage 1 候選預篩；異常或降級時回退全量。"""

    def __init__(self, index: SearchCandidateIndex) -> None:
        self._index = index

    def select_candidates(
        self,
        scanned_files: list[ScannedFile],
        roots: list[Path],
        terms: list[str],
    ) -> list[ScannedFile]:
        candidates = self._index.sync_and_query_terms(scanned_files, roots, terms, read_text_file_safe)
        if candidates is None:
            return scanned_files
        return candidates


def _build_rag_candidate_strategy(
    *,
    allow_disk_index: bool,
    file_count: int,
    terms: list[str],
    index: SearchCandidateIndex,
) -> _RagCandidateStrategy:
    """依安全旗標、檔案數量門檻與詞彙長度建立對應檢索策略（Factory）。"""
    if not allow_disk_index or file_count <= SEARCH_FTS_THRESHOLD_FILES or any(len(term) < 3 for term in terms):
        return _DirectScanCandidateStrategy()
    return _FtsTrigramCandidateStrategy(index)


def _format_snippet_line(line_no: int, raw_line: str, highlight_re: re.Pattern[str]) -> str:
    """將單行命中文字加上 >>><<< 標記並裁切至安全長度。"""
    clean = raw_line.strip()
    if len(clean) > RAG_SNIPPET_MAX_CHARS:
        m = highlight_re.search(clean)
        if m is not None:
            half = RAG_SNIPPET_MAX_CHARS // 2
            start = max(0, m.start() - half)
            end = min(len(clean), start + RAG_SNIPPET_MAX_CHARS)
            if end - start < RAG_SNIPPET_MAX_CHARS:
                start = max(0, end - RAG_SNIPPET_MAX_CHARS)
            prefix = "..." if start > 0 else ""
            suffix = "..." if end < len(clean) else ""
            clean = prefix + clean[start:end] + suffix
        else:
            clean = clean[:RAG_SNIPPET_MAX_CHARS] + "..."
    highlighted = highlight_re.sub(lambda match: f">>>{match.group(0)}<<<", clean)
    return f"L{line_no}: {highlighted}"


def _score_and_build_snippet(
    sf: ScannedFile,
    text: str,
    lower_terms: list[str],
    highlight_re: re.Pattern[str],
) -> tuple[float, str] | None:
    """計算檔案多關鍵字相關度分數並擷取精簡命中片段。"""
    text_lower = text.lower()
    name_lower = sf.display_path.lower()

    matched_distinct = sum(1 for lt in lower_terms if lt in text_lower or lt in name_lower)
    if matched_distinct == 0:
        return None

    freq_score = sum(min(text_lower.count(lt), 20) for lt in lower_terms) * 2.0
    name_bonus = sum(15.0 for lt in lower_terms if lt in name_lower)
    score = matched_distinct * 100.0 + freq_score + name_bonus

    lines = text.splitlines()
    picked: list[tuple[int, str]] = []
    picked_indices: set[int] = set()
    covered_terms: set[str] = set()

    for _ in range(RAG_SNIPPET_MAX_LINES):
        best_idx = -1
        best_line_score = (0, 0)
        for idx, line in enumerate(lines):
            if idx in picked_indices or not line.strip():
                continue
            low_line = line.lower()
            hits = [lt for lt in lower_terms if lt in low_line]
            if not hits:
                continue
            new_hits = sum(1 for lt in hits if lt not in covered_terms)
            line_score = (new_hits, len(hits))
            if line_score > best_line_score:
                best_line_score = line_score
                best_idx = idx
        if best_idx == -1:
            break
        picked_indices.add(best_idx)
        raw_line = lines[best_idx]
        picked.append((best_idx + 1, raw_line))
        low_picked = raw_line.lower()
        covered_terms.update(lt for lt in lower_terms if lt in low_picked)

    if picked:
        picked.sort(key=lambda item: item[0])
        snippet = "\n".join(_format_snippet_line(ln, txt, highlight_re) for ln, txt in picked)
    else:
        first_line = next(((i + 1, ln) for i, ln in enumerate(lines) if ln.strip()), None)
        snippet = _format_snippet_line(first_line[0], first_line[1], highlight_re) if first_line else sf.display_path

    return score, snippet


def sync_codebase_index(workspace_dirs: list[str] | None = None) -> None:
    """將 workspace 中的純文字檔同步至統一 SQLite FTS5 Trigram 索引。"""
    roots = _resolve_workspace_roots(workspace_dirs)
    scanned_files, _ = iter_workspace_files(roots, workspace_dirs)
    default_search_index.sync_workspace(scanned_files, roots, read_text_file_safe)


def sync_index_if_needed(workspace_dirs: list[str] | None = None) -> None:
    """增量同步工作區索引（內部自動比對 mtime/size 略過未變更檔案）。"""
    sync_codebase_index(workspace_dirs)


def _resolve_top_k(raw_top_k: Any) -> int:
    if raw_top_k is None:
        return DEFAULT_RAG_TOP_K
    try:
        val = int(raw_top_k)
    except TypeError, ValueError:
        return DEFAULT_RAG_TOP_K
    if val <= 0:
        return DEFAULT_RAG_TOP_K
    return max(1, min(val, MAX_RAG_TOP_K))


def call_indexed_search(
    args: dict[str, Any],
    workspace_dirs: list[str] | None = None,
    *,
    allow_disk_index: bool = True,
    search_index: SearchCandidateIndex | None = None,
) -> str:
    """提供給 Agent 使用之多關鍵字相關度檢索工具（統一共用 WorkspaceSearchIndex）。"""
    query = str(args.get("query") or "").strip()
    if not query:
        return f"[ERROR] {t('rag_tool.missing_query')}"

    terms = _extract_query_terms(query)
    if not terms:
        return f"[ERROR] {t('rag_tool.invalid_keywords')}"

    top_k = _resolve_top_k(args.get("top_k"))
    roots = _resolve_workspace_roots(workspace_dirs)
    scanned_files, _ = iter_workspace_files(roots, workspace_dirs)
    if not scanned_files:
        return t("rag_tool.no_results", query=query)

    active_index = search_index if search_index is not None else default_search_index
    strategy = _build_rag_candidate_strategy(
        allow_disk_index=allow_disk_index,
        file_count=len(scanned_files),
        terms=terms,
        index=active_index,
    )
    candidates = strategy.select_candidates(scanned_files, roots, terms)

    lower_terms = [term.lower() for term in terms]
    highlight_pattern = "|".join(re.escape(term) for term in sorted(terms, key=len, reverse=True))
    highlight_re = re.compile(highlight_pattern, re.IGNORECASE)

    scored_hits: list[tuple[float, str, str]] = []
    for sf in candidates:
        check_cancelled()
        text, _enc = read_text_file_safe(sf.path)
        if text is None:
            continue
        scored = _score_and_build_snippet(sf, text, lower_terms, highlight_re)
        if scored is None:
            continue
        score, snippet = scored
        display_path = format_display_path(str(sf.path)) or sf.display_path
        scored_hits.append((score, display_path, snippet))

    if not scored_hits:
        return t("rag_tool.no_results", query=query)

    scored_hits.sort(key=lambda item: (-item[0], len(item[1]), item[1]))
    results = [
        t("rag_tool.result_item", path=display_path, snippet=snippet)
        for _score, display_path, snippet in scored_hits[:top_k]
    ]
    inner = f"{t('rag_tool.result_header')}\n" + "-" * 40 + "\n" + "\n".join(results)
    return wrap_external_content(inner)


INDEXED_SEARCH_TOOL = {
    "type": "function",
    "name": "indexed_search",
    "description": (
        "Multi-keyword ranked search across workspace text files. Useful when you remember several "
        "unrelated concepts, identifiers, or terms and want relevance-ranked snippets rather than "
        "an exact substring match. Shares the unified local incremental FTS5 index with `search_files`."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The concept, keywords, or function names to search for."},
            "top_k": {"type": "integer", "description": "Number of top results to return (default 5, max 10)."},
        },
        "required": ["query"],
    },
    "_is_safe_read": True,
}

RAG_TOOLS = [INDEXED_SEARCH_TOOL]


def is_rag_tool(name: str) -> bool:
    return any(tool["name"] == name for tool in RAG_TOOLS)
