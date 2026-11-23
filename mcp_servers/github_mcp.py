"""GitHub REST API 整合 MCP 伺服器薄入口。"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp_servers.github import (  # noqa: E402
    github_create_comment,
    github_create_issue,
    github_create_pr,
    github_get_pr_checks,
    github_get_pr_files,
    github_get_pr_reviews,
    github_list_issues,
    github_list_prs,
    github_prepare_pr_context,
    github_read_comments,
    github_read_issue,
    github_search_issues,
    mcp,
)

__all__ = [
    "github_create_comment",
    "github_create_issue",
    "github_create_pr",
    "github_get_pr_checks",
    "github_get_pr_files",
    "github_get_pr_reviews",
    "github_list_issues",
    "github_list_prs",
    "github_prepare_pr_context",
    "github_read_comments",
    "github_read_issue",
    "github_search_issues",
    "mcp",
]

if __name__ == "__main__":
    mcp.run()
