"""GitHub PR 範本三階段載入與預設目標分支解析。"""

import base64
from pathlib import Path

import httpx

from mcp_servers.github import client as gh_client
from mcp_servers.github import security

_LOCAL_PR_TEMPLATE_PATHS: tuple[str, ...] = (
    ".github/pull_request_template.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
    "docs/pull_request_template.md",
    "docs/PULL_REQUEST_TEMPLATE.md",
    "pull_request_template.md",
    "PULL_REQUEST_TEMPLATE.md",
)

_DEFAULT_PR_TEMPLATE = (
    "## Summary\n"
    "<!-- Briefly describe what this PR does and why -->\n\n"
    "## Key Changes\n"
    "<!-- List the main changes made in this branch -->\n\n"
    "## Type of Change\n"
    "- [ ] Bug fix\n"
    "- [ ] New feature\n"
    "- [ ] Refactor / Performance improvement\n"
    "- [ ] Security hardening\n"
    "- [ ] Docs / Tests / Chore\n\n"
    "## Verification & Testing\n"
    "<!-- Describe how the changes were tested or verified -->"
)


async def resolve_base_branch(
    client: httpx.AsyncClient,
    safe_path: str,
    headers: dict[str, str],
    base: str,
) -> str:
    """解析 PR 目標分支 (base)；若未指定則自動向 GitHub 查詢儲存庫的 default_branch。"""
    cleaned = (base or "").strip()
    if cleaned:
        return security.validate_git_ref(cleaned)
    repo_url = f"{gh_client.GITHUB_API_BASE}/repos/{safe_path}"
    repo_data = await gh_client.request_json(client, "GET", repo_url, headers=headers)
    default_branch = "main"
    if isinstance(repo_data, dict) and isinstance(repo_data.get("default_branch"), str):
        candidate = repo_data["default_branch"].strip()
        if candidate:
            default_branch = candidate
    return security.validate_git_ref(default_branch)


async def load_pr_template(
    client: httpx.AsyncClient,
    safe_path: str,
    headers: dict[str, str],
) -> tuple[str, str]:
    """依「本機工作區 → 遠端儲存庫 API → 內建業界標準慣例範本」三階段取得 PR 範本。"""
    cwd = Path.cwd()
    max_bytes = security.MAX_RESPONSE_BYTES
    for rel_path in _LOCAL_PR_TEMPLATE_PATHS:
        candidate = cwd / rel_path
        try:
            if candidate.is_file() and candidate.stat().st_size <= max_bytes:
                content = candidate.read_text(encoding="utf-8", errors="replace").strip()
                if content:
                    return content, f"本機工作區範本 ({rel_path})"
        except OSError:
            continue

    template_dir = cwd / ".github" / "PULL_REQUEST_TEMPLATE"
    try:
        if template_dir.is_dir():
            for md_file in sorted(template_dir.glob("*.md")):
                if md_file.is_file() and md_file.stat().st_size <= max_bytes:
                    content = md_file.read_text(encoding="utf-8", errors="replace").strip()
                    if content:
                        rel_str = md_file.relative_to(cwd).as_posix()
                        return content, f"本機工作區範本 ({rel_str})"
    except OSError:
        pass

    remote_url = f"{gh_client.GITHUB_API_BASE}/repos/{safe_path}/contents/.github/pull_request_template.md"
    try:
        data = await gh_client.request_json(client, "GET", remote_url, headers=headers, allow_not_found=True)
        if isinstance(data, dict) and isinstance(data.get("content"), str):
            decoded = base64.b64decode(data["content"]).decode("utf-8", errors="replace").strip()
            if decoded:
                return decoded, "遠端儲存庫範本 (.github/pull_request_template.md)"
    except PermissionError:
        raise
    except Exception:
        pass

    return _DEFAULT_PR_TEMPLATE, "預設業界標準範本 (Conventional PR Template)"
