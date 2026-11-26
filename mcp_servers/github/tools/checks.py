"""GitHub CI Checks、Annotations 與 Actions Workflow 工具群組（Command / Registrar Pattern）。"""

from typing import Any
from urllib.parse import quote

from mcp_servers._shared import validate_positive_int
from mcp_servers.github import client as gh_client
from mcp_servers.github import helpers, security

_MAX_CHECK_SUMMARY_CHARS = 300


async def github_get_pr_checks(
    owner: str,
    repo: str,
    ref: str,
    per_page: int = 30,
    page: int = 1,
    github_token: str = "",
) -> str:
    """查詢指定 Commit SHA、分支或標籤的 GitHub Actions / CI Check Runs 狀態。"""
    active_token = ""
    target = f"{owner}/{repo}@{ref}"
    cmd_desc = f"GET commits/{ref}/check-runs per_page={per_page} page={page}"
    try:
        clean_owner, clean_repo = security.check_repo_allowed(owner, repo)
        clean_ref = security.validate_git_ref(ref)
        clean_per_page = validate_positive_int(per_page, "per_page", max_val=100)
        clean_page = validate_positive_int(page, "page")
        target = f"{clean_owner}/{clean_repo}@{clean_ref}"

        headers = gh_client.get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")
        safe_ref = quote(clean_ref, safe="")
        url = helpers.build_repo_url(clean_owner, clean_repo, f"/commits/{safe_ref}/check-runs")

        async with gh_client.create_http_client() as client:
            data = await gh_client.request_json(
                client,
                "GET",
                url,
                headers=headers,
                params={"per_page": clean_per_page, "page": clean_page},
            )

        if not isinstance(data, dict):
            raise ValueError("GitHub API 回傳的 Check Runs 資料格式異常。")

        runs = data.get("check_runs")
        total_count = data.get("total_count", 0)
        if not isinstance(runs, list) or not runs:
            gh_client.AUDIT.log("github_get_pr_checks", cmd_desc, target=target, status="OK")
            return f"目前 {target} 沒有任何 Check Runs 紀錄（第 {clean_page} 頁）。"

        lines: list[str] = [f"CI Check Runs（共 {total_count} 項，目前顯示第 {clean_page} 頁）："]
        for run in runs:
            if not isinstance(run, dict):
                continue
            name = run.get("name") or "?"
            status = run.get("status") or "unknown"
            conclusion = run.get("conclusion")
            state_str = f"{status}/{conclusion}" if conclusion else status
            run_id = run.get("id")
            id_suffix = f" (id={run_id})" if isinstance(run_id, int) and not isinstance(run_id, bool) else ""
            line = f"- {name}: [{state_str}]{id_suffix}"
            out_obj = run.get("output")
            if isinstance(out_obj, dict) and out_obj.get("title"):
                line += f" — {str(out_obj['title']).strip()}"
            lines.append(line)
            if isinstance(out_obj, dict) and isinstance(out_obj.get("summary"), str) and out_obj["summary"].strip():
                clipped_summary = security.GITHUB_REDACTION.clip_text(
                    " ".join(out_obj["summary"].split()),
                    _MAX_CHECK_SUMMARY_CHARS,
                    "...",
                )
                lines.append(f"  摘要：{clipped_summary}")

        output, was_truncated = security.GITHUB_REDACTION.sanitize_and_truncate(
            "\n".join(lines),
            active_token,
            "請縮小 per_page 或搭配 page 分頁檢視。",
        )
        gh_client.AUDIT.log(
            "github_get_pr_checks",
            cmd_desc,
            target=target,
            status="TRUNCATED" if was_truncated else "OK",
            detail=f"count={len(runs)}",
        )
        return output
    except Exception as exc:
        return gh_client.handle_tool_error(
            "github_get_pr_checks",
            cmd_desc,
            target,
            exc,
            "無法取得 CI Check 狀態",
            active_token=active_token,
        )


async def github_get_check_annotations(
    owner: str,
    repo: str,
    check_run_id: int,
    per_page: int = 30,
    page: int = 1,
    github_token: str = "",
) -> str:
    """取得指定 Check Run 的詳細錯誤／警告註記（檔案、行號與訊息）。"""
    active_token = ""
    target = f"{owner}/{repo}#{check_run_id}"
    cmd_desc = f"GET check-runs/{check_run_id}/annotations per_page={per_page} page={page}"
    try:
        clean_owner, clean_repo = security.check_repo_allowed(owner, repo)
        clean_run_id = validate_positive_int(check_run_id, "check_run_id")
        clean_per_page = validate_positive_int(per_page, "per_page", max_val=100)
        clean_page = validate_positive_int(page, "page")
        target = f"{clean_owner}/{clean_repo}#{clean_run_id}"

        headers = gh_client.get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")
        url = helpers.build_repo_url(clean_owner, clean_repo, f"/check-runs/{clean_run_id}/annotations")

        async with gh_client.create_http_client() as client:
            annotations = await gh_client.request_json(
                client,
                "GET",
                url,
                headers=headers,
                params={"per_page": clean_per_page, "page": clean_page},
            )

        if not isinstance(annotations, list) or not annotations:
            gh_client.AUDIT.log("github_get_check_annotations", cmd_desc, target=target, status="OK")
            return f"Check Run {target} 目前沒有任何 Annotations（第 {clean_page} 頁）。"

        blocks: list[str] = []
        for item in annotations:
            if not isinstance(item, dict):
                continue
            level = item.get("annotation_level") or "notice"
            path = item.get("path") or "?"
            start_line = item.get("start_line")
            end_line = item.get("end_line")
            if isinstance(start_line, int) and isinstance(end_line, int) and end_line != start_line:
                line_range = f"{start_line}-{end_line}"
            elif isinstance(start_line, int):
                line_range = str(start_line)
            else:
                line_range = "?"
            ann_title = (item.get("title") or "").strip()
            title_suffix = f" — {ann_title}" if ann_title else ""
            message = (item.get("message") or "").strip()
            blocks.append(f"- [{level}] {path}:{line_range}{title_suffix}\n  {message}")

        output, was_truncated = security.GITHUB_REDACTION.sanitize_and_truncate(
            "\n".join(blocks),
            active_token,
            "請縮小 per_page 或搭配 page 分頁檢視。",
        )
        gh_client.AUDIT.log(
            "github_get_check_annotations",
            cmd_desc,
            target=target,
            status="TRUNCATED" if was_truncated else "OK",
            detail=f"count={len(blocks)}",
        )
        return output
    except Exception as exc:
        return gh_client.handle_tool_error(
            "github_get_check_annotations",
            cmd_desc,
            target,
            exc,
            "無法取得 Check Annotations",
            active_token=active_token,
        )


async def github_list_workflow_runs(
    owner: str,
    repo: str,
    branch: str = "",
    status: str = "",
    event: str = "",
    per_page: int = 20,
    page: int = 1,
    github_token: str = "",
) -> str:
    """列出指定儲存庫的 GitHub Actions Workflow 執行紀錄（支援 branch、status、event 篩選）。"""
    active_token = ""
    target = f"{owner}/{repo}"
    cmd_desc = f"GET actions/runs per_page={per_page} page={page}"
    try:
        clean_owner, clean_repo = security.check_repo_allowed(owner, repo)
        target = f"{clean_owner}/{clean_repo}"
        clean_per_page = validate_positive_int(per_page, "per_page", max_val=100)
        clean_page = validate_positive_int(page, "page")

        params: dict[str, str | int] = {"per_page": clean_per_page, "page": clean_page}
        if branch and branch.strip():
            params["branch"] = security.validate_git_ref(branch)
        if status and status.strip():
            params["status"] = security.validate_choice(status, security.ALLOWED_WORKFLOW_STATUSES, "status")
        if event and event.strip():
            params["event"] = security.validate_filter_text(event, "event")

        headers = gh_client.get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")
        url = helpers.build_repo_url(clean_owner, clean_repo, "/actions/runs")

        async with gh_client.create_http_client() as client:
            data = await gh_client.request_json(client, "GET", url, headers=headers, params=params)

        if not isinstance(data, dict):
            raise ValueError("GitHub API 回傳的 Workflow Runs 資料格式異常。")

        runs = data.get("workflow_runs")
        total_count = data.get("total_count", 0)
        if not isinstance(runs, list) or not runs:
            gh_client.AUDIT.log("github_list_workflow_runs", cmd_desc, target=target, status="OK")
            return f"目前 {target} 沒有符合條件的 Workflow Runs（第 {clean_page} 頁）。"

        lines: list[str] = [f"Workflow Runs（共 {total_count} 筆，目前顯示第 {clean_page} 頁）："]
        for run in runs:
            if not isinstance(run, dict):
                continue
            run_id = run.get("id", "?")
            run_num = run.get("run_number", "?")
            name = (run.get("display_title") or run.get("name") or "?").strip()
            run_status = run.get("status") or "unknown"
            conclusion = run.get("conclusion")
            state_str = f"{run_status}/{conclusion}" if conclusion else run_status
            head_branch = run.get("head_branch") or "?"
            head_sha = str(run.get("head_sha") or "")[:7]
            run_event = run.get("event") or "?"
            created_at = run.get("created_at") or ""
            lines.append(
                f"#{run_num} [{state_str}] {name} (id={run_id}) | {head_branch}@{head_sha} | {run_event} | {created_at}"
            )

        output, was_truncated = security.GITHUB_REDACTION.sanitize_and_truncate(
            "\n".join(lines),
            active_token,
            "請縮小 per_page 或搭配 page 分頁檢視。",
        )
        gh_client.AUDIT.log(
            "github_list_workflow_runs",
            cmd_desc,
            target=target,
            status="TRUNCATED" if was_truncated else "OK",
            detail=f"count={len(runs)}",
        )
        return output
    except Exception as exc:
        return gh_client.handle_tool_error(
            "github_list_workflow_runs",
            cmd_desc,
            target,
            exc,
            "無法取得 Workflow Runs 列表",
            active_token=active_token,
        )


async def github_get_workflow_run_jobs(
    owner: str,
    repo: str,
    run_id: int,
    filter_mode: str = "latest",
    per_page: int = 30,
    page: int = 1,
    github_token: str = "",
) -> str:
    """查詢指定 GitHub Actions Workflow Run 內的 Jobs 與 Steps 執行狀態。"""
    active_token = ""
    target = f"{owner}/{repo}#{run_id}"
    cmd_desc = f"GET actions/runs/{run_id}/jobs filter={filter_mode} per_page={per_page} page={page}"
    try:
        clean_owner, clean_repo = security.check_repo_allowed(owner, repo)
        clean_run_id = validate_positive_int(run_id, "run_id")
        clean_filter = security.validate_choice(filter_mode, security.ALLOWED_JOB_FILTERS, "filter_mode")
        clean_per_page = validate_positive_int(per_page, "per_page", max_val=100)
        clean_page = validate_positive_int(page, "page")
        target = f"{clean_owner}/{clean_repo}#{clean_run_id}"

        headers = gh_client.get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")
        url = helpers.build_repo_url(clean_owner, clean_repo, f"/actions/runs/{clean_run_id}/jobs")

        async with gh_client.create_http_client() as client:
            data = await gh_client.request_json(
                client,
                "GET",
                url,
                headers=headers,
                params={"filter": clean_filter, "per_page": clean_per_page, "page": clean_page},
            )

        if not isinstance(data, dict):
            raise ValueError("GitHub API 回傳的 Workflow Jobs 資料格式異常。")

        jobs = data.get("jobs")
        total_count = data.get("total_count", 0)
        if not isinstance(jobs, list) or not jobs:
            gh_client.AUDIT.log("github_get_workflow_run_jobs", cmd_desc, target=target, status="OK")
            return f"Workflow Run {target} 目前沒有任何 Jobs 紀錄（第 {clean_page} 頁）。"

        sections: list[str] = [f"Workflow Run Jobs（共 {total_count} 個，目前顯示第 {clean_page} 頁）："]
        for job in jobs:
            if not isinstance(job, dict):
                continue
            job_id = job.get("id", "?")
            job_name = job.get("name") or "?"
            job_status = job.get("status") or "unknown"
            job_conclusion = job.get("conclusion")
            state_str = f"{job_status}/{job_conclusion}" if job_conclusion else job_status
            job_lines = [f"Job: {job_name} [{state_str}] (id={job_id})"]
            steps = job.get("steps")
            if isinstance(steps, list):
                for step in steps:
                    if not isinstance(step, dict):
                        continue
                    s_num = step.get("number", "?")
                    s_name = step.get("name") or "?"
                    s_status = step.get("status") or "unknown"
                    s_concl = step.get("conclusion")
                    s_state = f"{s_status}/{s_concl}" if s_concl else s_status
                    job_lines.append(f"  {s_num}. {s_name} [{s_state}]")
            sections.append("\n".join(job_lines))

        output, was_truncated = security.GITHUB_REDACTION.sanitize_and_truncate(
            "\n\n".join(sections),
            active_token,
            "請縮小 per_page 或搭配 page 分頁檢視。",
        )
        gh_client.AUDIT.log(
            "github_get_workflow_run_jobs",
            cmd_desc,
            target=target,
            status="TRUNCATED" if was_truncated else "OK",
            detail=f"count={len(jobs)}",
        )
        return output
    except Exception as exc:
        return gh_client.handle_tool_error(
            "github_get_workflow_run_jobs",
            cmd_desc,
            target,
            exc,
            "無法取得 Workflow Jobs 狀態",
            active_token=active_token,
        )


def register(mcp: Any) -> None:
    """註冊 GitHub CI Checks 與 Actions Workflow 工具至 MCPServer。"""
    for fn in (
        github_get_pr_checks,
        github_get_check_annotations,
        github_list_workflow_runs,
        github_get_workflow_run_jobs,
    ):
        mcp.tool()(fn)
