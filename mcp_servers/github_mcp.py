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
    github_create_pr_review,
    github_get_check_annotations,
    github_get_file_content,
    github_get_pr,
    github_get_pr_checks,
    github_get_pr_files,
    github_get_pr_review_comments,
    github_get_pr_reviews,
    github_get_repo,
    github_get_workflow_run_jobs,
    github_list_branches,
    github_list_commits,
    github_list_issues,
    github_list_labels,
    github_list_prs,
    github_list_releases,
    github_list_tags,
    github_list_workflow_runs,
    github_prepare_pr_context,
    github_read_comments,
    github_read_issue,
    github_search_issues,
    github_update_issue,
    github_update_pr,
    mcp,
)

__all__ = [
    "github_create_comment",
    "github_create_issue",
    "github_create_pr",
    "github_create_pr_review",
    "github_get_check_annotations",
    "github_get_file_content",
    "github_get_pr",
    "github_get_pr_checks",
    "github_get_pr_files",
    "github_get_pr_review_comments",
    "github_get_pr_reviews",
    "github_get_repo",
    "github_get_workflow_run_jobs",
    "github_list_branches",
    "github_list_commits",
    "github_list_issues",
    "github_list_labels",
    "github_list_prs",
    "github_list_releases",
    "github_list_tags",
    "github_list_workflow_runs",
    "github_prepare_pr_context",
    "github_read_comments",
    "github_read_issue",
    "github_search_issues",
    "github_update_issue",
    "github_update_pr",
    "mcp",
]

if __name__ == "__main__":
    mcp.run()
