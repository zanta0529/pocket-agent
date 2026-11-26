"""GitHub Issue 與留言工具群組（Command / Registrar Pattern）。"""

from typing import Any

from mcp_servers._shared import validate_positive_int
from mcp_servers.github import client as gh_client
from mcp_servers.github import helpers, security


async def github_read_issue(
    owner: str,
    repo: str,
    issue_number: int,
    github_token: str = "",
) -> str:
    """讀取指定的 Issue 或 Pull Request 標題、狀態、標籤與內文。"""
    active_token = ""
    target = f"{owner}/{repo}#{issue_number}"
    cmd_desc = f"GET issues/{issue_number}"
    try:
        clean_owner, clean_repo = security.check_repo_allowed(owner, repo)
        clean_num = validate_positive_int(issue_number, "issue_number")
        target = f"{clean_owner}/{clean_repo}#{clean_num}"

        headers = gh_client.get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")
        url = helpers.build_repo_url(clean_owner, clean_repo, f"/issues/{clean_num}")

        async with gh_client.create_http_client() as client:
            data = await gh_client.request_json(client, "GET", url, headers=headers)

        if not isinstance(data, dict):
            raise ValueError("GitHub API 回傳的 Issue 資料格式異常。")

        title = data.get("title") or ""
        issue_state = data.get("state") or ""
        author = helpers.extract_login(data)
        issue_body = data.get("body") or ""
        header_lines = [
            f"標題：{title}",
            f"作者：{author}",
            f"狀態：{issue_state}",
        ]
        if "pull_request" in data:
            header_lines.append("類型：Pull Request")
        label_names = helpers.extract_label_names(data)
        if label_names:
            header_lines.append(f"標籤：{', '.join(label_names)}")
        assignees = helpers.extract_user_logins(data.get("assignees"))
        if assignees:
            header_lines.append(f"指派人：{', '.join(assignees)}")
        milestone_obj = data.get("milestone")
        if isinstance(milestone_obj, dict) and milestone_obj.get("title"):
            header_lines.append(f"里程碑：{milestone_obj['title']}")
        comments_count = data.get("comments")
        if isinstance(comments_count, int) and not isinstance(comments_count, bool):
            header_lines.append(f"留言數：{comments_count}")
        raw_output = "\n".join(header_lines) + f"\n\n{issue_body}"

        output, was_truncated = security.GITHUB_REDACTION.sanitize_and_truncate(
            raw_output,
            active_token,
            "內文過長已自動截斷前段。",
        )
        gh_client.AUDIT.log(
            "github_read_issue",
            cmd_desc,
            target=target,
            status="TRUNCATED" if was_truncated else "OK",
        )
        return output
    except Exception as exc:
        return gh_client.handle_tool_error(
            "github_read_issue",
            cmd_desc,
            target,
            exc,
            "無法取得 Issue 內容",
            active_token=active_token,
        )


async def github_read_comments(
    owner: str,
    repo: str,
    issue_number: int,
    per_page: int = 30,
    page: int = 1,
    github_token: str = "",
) -> str:
    """讀取指定 Issue 或 Pull Request 的留言討論串（支援分頁查詢）。"""
    active_token = ""
    target = f"{owner}/{repo}#{issue_number}"
    cmd_desc = f"GET issues/{issue_number}/comments per_page={per_page} page={page}"
    try:
        clean_owner, clean_repo = security.check_repo_allowed(owner, repo)
        clean_num = validate_positive_int(issue_number, "issue_number")
        clean_per_page = validate_positive_int(per_page, "per_page", max_val=100)
        clean_page = validate_positive_int(page, "page")
        target = f"{clean_owner}/{clean_repo}#{clean_num}"

        headers = gh_client.get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")
        url = helpers.build_repo_url(clean_owner, clean_repo, f"/issues/{clean_num}/comments")

        async with gh_client.create_http_client() as client:
            comments = await gh_client.request_json(
                client,
                "GET",
                url,
                headers=headers,
                params={"per_page": clean_per_page, "page": clean_page},
            )

        if not isinstance(comments, list) or not comments:
            gh_client.AUDIT.log("github_read_comments", cmd_desc, target=target, status="OK")
            return f"目前 {target} 沒有任何留言（第 {clean_page} 頁）。"

        blocks: list[str] = []
        for idx, comment in enumerate(comments, start=(clean_page - 1) * clean_per_page + 1):
            if not isinstance(comment, dict):
                continue
            author = helpers.extract_login(comment)
            created_at = comment.get("created_at") or ""
            body = (comment.get("body") or "").strip()
            blocks.append(f"--- 留言 #{idx} | 作者：{author} | 時間：{created_at} ---\n{body}")

        output, was_truncated = security.GITHUB_REDACTION.sanitize_and_truncate(
            "\n\n".join(blocks),
            active_token,
            "請縮小 per_page 或搭配 page 分頁檢視。",
        )
        gh_client.AUDIT.log(
            "github_read_comments",
            cmd_desc,
            target=target,
            status="TRUNCATED" if was_truncated else "OK",
            detail=f"count={len(blocks)}",
        )
        return output
    except Exception as exc:
        return gh_client.handle_tool_error(
            "github_read_comments",
            cmd_desc,
            target,
            exc,
            "無法取得留言列表",
            active_token=active_token,
        )


async def github_list_issues(
    owner: str,
    repo: str,
    state: str = "open",
    labels: str = "",
    assignee: str = "",
    creator: str = "",
    milestone: str = "",
    sort: str = "",
    direction: str = "",
    per_page: int = 30,
    page: int = 1,
    include_prs: bool = False,
    github_token: str = "",
) -> str:
    """列出指定儲存庫的 Issues（支援 state、labels、assignee、creator、milestone 與排序篩選，預設排除 PR）。"""
    active_token = ""
    target = f"{owner}/{repo}"
    cmd_desc = f"GET issues state={state} per_page={per_page} page={page}"
    try:
        clean_owner, clean_repo = security.check_repo_allowed(owner, repo)
        target = f"{clean_owner}/{clean_repo}"
        norm_state = security.validate_state(state)
        clean_per_page = validate_positive_int(per_page, "per_page", max_val=100)
        clean_page = validate_positive_int(page, "page")

        clean_labels = security.validate_filter_text(labels, "labels", max_len=200)
        params: dict[str, str | int] = {
            "state": norm_state,
            "per_page": clean_per_page,
            "page": clean_page,
        }
        if clean_labels:
            params["labels"] = clean_labels
        if assignee and assignee.strip():
            params["assignee"] = security.validate_filter_text(assignee, "assignee")
        if creator and creator.strip():
            params["creator"] = security.validate_filter_text(creator, "creator")
        if milestone and milestone.strip():
            params["milestone"] = security.validate_filter_text(milestone, "milestone")
        if sort and sort.strip():
            params["sort"] = security.validate_choice(sort, security.ALLOWED_ISSUE_SORTS, "sort")
        if direction and direction.strip():
            params["direction"] = security.validate_choice(direction, security.ALLOWED_DIRECTIONS, "direction")

        headers = gh_client.get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")
        url = helpers.build_repo_url(clean_owner, clean_repo, "/issues")

        async with gh_client.create_http_client() as client:
            items = await gh_client.request_json(client, "GET", url, headers=headers, params=params)

        if not isinstance(items, list):
            raise ValueError("GitHub API 回傳的 Issues 清單格式異常。")

        lines: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if not include_prs and "pull_request" in item:
                continue
            num = item.get("number", "?")
            title = (item.get("title") or "").strip()
            item_state = item.get("state") or norm_state
            label_names = helpers.extract_label_names(item)
            label_str = f" [{', '.join(label_names)}]" if label_names else ""
            lines.append(f"#{num} [{item_state}] {title} ({helpers.extract_login(item)}){label_str}")

        if not lines:
            gh_client.AUDIT.log("github_list_issues", cmd_desc, target=target, status="OK")
            return f"目前沒有符合條件（state={norm_state}）的 Issues（第 {clean_page} 頁）。"

        output, was_truncated = security.GITHUB_REDACTION.sanitize_and_truncate(
            "\n".join(lines),
            active_token,
            "請縮小 per_page 或搭配 page 分頁檢視。",
        )
        gh_client.AUDIT.log(
            "github_list_issues",
            cmd_desc,
            target=target,
            status="TRUNCATED" if was_truncated else "OK",
            detail=f"count={len(lines)}",
        )
        return output
    except Exception as exc:
        return gh_client.handle_tool_error(
            "github_list_issues",
            cmd_desc,
            target,
            exc,
            "無法取得 Issue 列表",
            active_token=active_token,
        )


async def github_search_issues(
    owner: str,
    repo: str,
    query: str,
    state: str = "all",
    per_page: int = 20,
    page: int = 1,
    github_token: str = "",
) -> str:
    """在指定儲存庫內以關鍵字搜尋 Issues 與 Pull Requests（強制鎖定儲存庫範圍）。"""
    active_token = ""
    target = f"{owner}/{repo}"
    cmd_desc = f"GET search/issues state={state} per_page={per_page} page={page}"
    try:
        clean_owner, clean_repo = security.check_repo_allowed(owner, repo)
        target = f"{clean_owner}/{clean_repo}"
        norm_state = security.validate_state(state)
        clean_per_page = validate_positive_int(per_page, "per_page", max_val=100)
        clean_page = validate_positive_int(page, "page")
        clean_query = security.check_search_query_scope(query)

        scoped_q = f"repo:{clean_owner}/{clean_repo} {clean_query}"
        if norm_state in ("open", "closed"):
            scoped_q += f" state:{norm_state}"

        headers = gh_client.get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")
        url = f"{gh_client.get_api_base()}/search/issues"

        async with gh_client.create_http_client() as client:
            data = await gh_client.request_json(
                client,
                "GET",
                url,
                headers=headers,
                params={"q": scoped_q, "per_page": clean_per_page, "page": clean_page},
            )

        if not isinstance(data, dict):
            raise ValueError("GitHub API 回傳的搜尋結果格式異常。")

        items = data.get("items")
        total_count = data.get("total_count", 0)
        if not isinstance(items, list) or not items:
            gh_client.AUDIT.log("github_search_issues", cmd_desc, target=target, status="OK")
            return f"在 {target} 中找不到符合查詢條件的 Issue 或 PR（第 {clean_page} 頁）。"

        lines: list[str] = [f"搜尋結果（共 {total_count} 筆，目前顯示第 {clean_page} 頁）："]
        for item in items:
            if not isinstance(item, dict):
                continue
            num = item.get("number", "?")
            title = (item.get("title") or "").strip()
            item_state = item.get("state") or "?"
            kind = "PR" if "pull_request" in item else "Issue"
            lines.append(f"#{num} [{kind}:{item_state}] {title} ({helpers.extract_login(item)})")

        output, was_truncated = security.GITHUB_REDACTION.sanitize_and_truncate(
            "\n".join(lines),
            active_token,
            "請縮小 per_page 或調整搜尋關鍵字。",
        )
        gh_client.AUDIT.log(
            "github_search_issues",
            cmd_desc,
            target=target,
            status="TRUNCATED" if was_truncated else "OK",
            detail=f"count={len(items)}",
        )
        return output
    except Exception as exc:
        return gh_client.handle_tool_error(
            "github_search_issues",
            cmd_desc,
            target,
            exc,
            "無法搜尋 Issue/PR",
            active_token=active_token,
        )


async def github_create_issue(
    owner: str,
    repo: str,
    title: str,
    body: str = "",
    labels: list[str] | None = None,
    assignees: list[str] | None = None,
    milestone: int | None = None,
    github_token: str = "",
) -> str:
    """在指定儲存庫建立新的 Issue（可選設定標籤、指派人與里程碑，並自動掃描機密）。"""
    active_token = ""
    target = f"{owner}/{repo}"
    cmd_desc = "POST issues"
    try:
        clean_owner, clean_repo = security.check_repo_allowed(owner, repo)
        target = f"{clean_owner}/{clean_repo}"

        clean_title = helpers.validate_single_line(
            title,
            "Issue 標題 (title)",
            empty_msg="Issue 標題 (title) 不可為空白。",
            invalid_msg="Issue 標題 (title) 包含無效的換行或控制字元。",
        )
        clean_body = body if isinstance(body, str) else ""
        clean_labels = helpers.clean_string_list(labels, "labels")
        clean_assignees = helpers.clean_string_list(assignees, "assignees")
        clean_milestone = validate_positive_int(milestone, "milestone") if milestone is not None else None

        headers = gh_client.get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")

        combined_outbound = "\n".join([clean_title, clean_body, " ".join(clean_labels), " ".join(clean_assignees)])
        block_reason = security.GITHUB_REDACTION.check_outbound_content(combined_outbound, active_token)
        if block_reason:
            gh_client.AUDIT.log(
                "github_create_issue",
                cmd_desc,
                target=target,
                status="BLOCKED",
                detail="outbound_policy_blocked",
            )
            return f"[ERROR] 安全攔截：Issue 內容疑似包含機密（{block_reason}），已阻斷外發以防外洩。"

        url = helpers.build_repo_url(clean_owner, clean_repo, "/issues")
        payload: dict[str, Any] = {"title": clean_title, "body": clean_body}
        if clean_labels:
            payload["labels"] = clean_labels
        if clean_assignees:
            payload["assignees"] = clean_assignees
        if clean_milestone is not None:
            payload["milestone"] = clean_milestone

        async with gh_client.create_http_client() as client:
            created = await gh_client.request_json(client, "POST", url, headers=headers, json_body=payload)

        issue_num = created.get("number", "?") if isinstance(created, dict) else "?"
        gh_client.AUDIT.log(
            "github_create_issue",
            cmd_desc,
            target=f"{target}#{issue_num}",
            status="OK",
            detail=f"chars={len(clean_title) + len(clean_body)}",
        )
        return f"成功在 {clean_owner}/{clean_repo} 建立 Issue #{issue_num}：{clean_title}"
    except Exception as exc:
        return gh_client.handle_tool_error(
            "github_create_issue",
            cmd_desc,
            target,
            exc,
            "無法建立 Issue",
            active_token=active_token,
        )


async def github_update_issue(
    owner: str,
    repo: str,
    issue_number: int,
    title: str | None = None,
    body: str | None = None,
    state: str | None = None,
    state_reason: str | None = None,
    labels: list[str] | None = None,
    assignees: list[str] | None = None,
    milestone: int | None = None,
    github_token: str = "",
) -> str:
    """更新既有 Issue 的標題、內文、狀態、關閉原因、標籤、指派人或里程碑。"""
    active_token = ""
    target = f"{owner}/{repo}#{issue_number}"
    cmd_desc = f"PATCH issues/{issue_number}"
    try:
        clean_owner, clean_repo = security.check_repo_allowed(owner, repo)
        clean_num = validate_positive_int(issue_number, "issue_number")
        target = f"{clean_owner}/{clean_repo}#{clean_num}"

        payload: dict[str, Any] = {}
        outbound_parts: list[str] = []
        if title is not None:
            clean_title = helpers.validate_single_line(
                title,
                "Issue 標題 (title)",
                empty_msg="Issue 標題 (title) 不可為空白。",
                invalid_msg="Issue 標題 (title) 包含無效的換行或控制字元。",
            )
            payload["title"] = clean_title
            outbound_parts.append(clean_title)
        if body is not None:
            if not isinstance(body, str):
                raise ValueError("Issue 內文 (body) 必須為字串。")
            payload["body"] = body
            outbound_parts.append(body)
        if state is not None:
            payload["state"] = security.validate_update_state(state)
        if state_reason is not None:
            payload["state_reason"] = security.validate_state_reason(state_reason)
        if labels is not None:
            clean_labels = helpers.clean_string_list(labels, "labels")
            payload["labels"] = clean_labels
            outbound_parts.extend(clean_labels)
        if assignees is not None:
            clean_assignees = helpers.clean_string_list(assignees, "assignees")
            payload["assignees"] = clean_assignees
            outbound_parts.extend(clean_assignees)
        if milestone is not None:
            payload["milestone"] = validate_positive_int(milestone, "milestone")

        if not payload:
            raise ValueError(
                "至少須指定一項要更新的欄位（title、body、state、state_reason、labels、assignees 或 milestone）。"
            )

        headers = gh_client.get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")

        if outbound_parts:
            block_reason = security.GITHUB_REDACTION.check_outbound_content("\n".join(outbound_parts), active_token)
            if block_reason:
                gh_client.AUDIT.log(
                    "github_update_issue",
                    cmd_desc,
                    target=target,
                    status="BLOCKED",
                    detail="outbound_policy_blocked",
                )
                return f"[ERROR] 安全攔截：Issue 更新內容疑似包含機密（{block_reason}），已阻斷外發以防外洩。"

        url = helpers.build_repo_url(clean_owner, clean_repo, f"/issues/{clean_num}")
        async with gh_client.create_http_client() as client:
            updated = await gh_client.request_json(client, "PATCH", url, headers=headers, json_body=payload)

        html_url = updated.get("html_url", "") if isinstance(updated, dict) else ""
        url_line = f"\nIssue 網址：{html_url}" if html_url else ""
        fields_str = "、".join(payload.keys())
        gh_client.AUDIT.log(
            "github_update_issue",
            cmd_desc,
            target=target,
            status="OK",
            detail=f"fields={','.join(payload.keys())}",
        )
        return f"成功更新 {target}（欄位：{fields_str}）。{url_line}"
    except Exception as exc:
        return gh_client.handle_tool_error(
            "github_update_issue",
            cmd_desc,
            target,
            exc,
            "無法更新 Issue",
            active_token=active_token,
        )


async def github_create_comment(
    owner: str,
    repo: str,
    issue_number: int,
    body: str,
    github_token: str = "",
) -> str:
    """在指定的 Issue 或 Pull Request 新增留言。"""
    active_token = ""
    target = f"{owner}/{repo}#{issue_number}"
    cmd_desc = f"POST issues/{issue_number}/comments"
    try:
        clean_owner, clean_repo = security.check_repo_allowed(owner, repo)
        clean_num = validate_positive_int(issue_number, "issue_number")
        target = f"{clean_owner}/{clean_repo}#{clean_num}"

        if not isinstance(body, str) or not body.strip():
            raise ValueError("留言內容 (body) 不可為空白。")

        headers = gh_client.get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")

        block_reason = security.GITHUB_REDACTION.check_outbound_content(body, active_token)
        if block_reason:
            gh_client.AUDIT.log(
                "github_create_comment",
                cmd_desc,
                target=target,
                status="BLOCKED",
                detail="outbound_policy_blocked",
            )
            return f"[ERROR] 安全攔截：留言內容疑似包含機密（{block_reason}），已阻斷外發以防外洩。"

        url = helpers.build_repo_url(clean_owner, clean_repo, f"/issues/{clean_num}/comments")
        async with gh_client.create_http_client() as client:
            await gh_client.request_json(client, "POST", url, headers=headers, json_body={"body": body})

        gh_client.AUDIT.log(
            "github_create_comment",
            cmd_desc,
            target=target,
            status="OK",
            detail=f"chars={len(body)}",
        )
        return f"成功在 {clean_owner}/{clean_repo}#{clean_num} 新增留言。"
    except Exception as exc:
        return gh_client.handle_tool_error(
            "github_create_comment",
            cmd_desc,
            target,
            exc,
            "無法新增留言",
            active_token=active_token,
        )


def register(mcp: Any) -> None:
    """註冊 GitHub Issue 與留言工具至 MCPServer。"""
    for fn in (
        github_read_issue,
        github_read_comments,
        github_list_issues,
        github_search_issues,
        github_create_issue,
        github_update_issue,
        github_create_comment,
    ):
        mcp.tool()(fn)
