"""GitHub Pull Request、Diff 與 Review 工具群組（Command / Registrar Pattern）。"""

from typing import Any
from urllib.parse import quote

from mcp_servers._shared import t, validate_positive_int
from mcp_servers.github import client as gh_client
from mcp_servers.github import helpers, security, templates

_MAX_SINGLE_PATCH_CHARS = 3000
_MAX_PR_CTX_PATCH_CHARS = 600
_MAX_REVIEW_COMMENTS = 50


async def github_list_prs(
    owner: str,
    repo: str,
    state: str = "open",
    head: str = "",
    base: str = "",
    sort: str = "",
    direction: str = "",
    per_page: int = 30,
    page: int = 1,
    github_token: str = "",
) -> str:
    """列出指定儲存庫的 Pull Requests（支援 state、head、base、sort、direction 與分頁查詢）。"""
    active_token = ""
    target = f"{owner}/{repo}"
    cmd_desc = f"GET pulls state={state} per_page={per_page} page={page}"
    try:
        clean_owner, clean_repo = security.check_repo_allowed(owner, repo)
        target = f"{clean_owner}/{clean_repo}"
        norm_state = security.validate_state(state)
        clean_per_page = validate_positive_int(per_page, "per_page", max_val=100)
        clean_page = validate_positive_int(page, "page")

        params: dict[str, str | int] = {
            "state": norm_state,
            "per_page": clean_per_page,
            "page": clean_page,
        }
        if head and head.strip():
            params["head"] = security.validate_head_ref(head)
        if base and base.strip():
            params["base"] = security.validate_git_ref(base)
        if sort and sort.strip():
            params["sort"] = security.validate_choice(sort, security.ALLOWED_PR_SORTS, "sort")
        if direction and direction.strip():
            params["direction"] = security.validate_choice(direction, security.ALLOWED_DIRECTIONS, "direction")

        headers = gh_client.get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")
        url = helpers.build_repo_url(clean_owner, clean_repo, "/pulls")

        async with gh_client.create_http_client() as client:
            prs = await gh_client.request_json(client, "GET", url, headers=headers, params=params)

        if not isinstance(prs, list) or not prs:
            gh_client.AUDIT.log("github_list_prs", cmd_desc, target=target, status="OK")
            return f"目前沒有狀態為 {norm_state} 的 Pull Requests（第 {clean_page} 頁）。"

        lines: list[str] = []
        for pr in prs:
            if not isinstance(pr, dict):
                continue
            num = pr.get("number", "?")
            title = (pr.get("title") or "").strip()
            line = f"#{num} {title} ({helpers.extract_login(pr)})"
            if pr.get("draft") is True:
                line += " [Draft]"
            head_obj = pr.get("head")
            base_obj = pr.get("base")
            if isinstance(head_obj, dict) and isinstance(base_obj, dict):
                head_ref = head_obj.get("ref")
                base_ref = base_obj.get("ref")
                if head_ref and base_ref:
                    line += f" [{head_ref} -> {base_ref}]"
            lines.append(line)

        output, was_truncated = security.GITHUB_REDACTION.sanitize_and_truncate(
            "\n".join(lines),
            active_token,
            "請縮小 per_page 或搭配 page 分頁檢視。",
        )
        gh_client.AUDIT.log(
            "github_list_prs",
            cmd_desc,
            target=target,
            status="TRUNCATED" if was_truncated else "OK",
            detail=f"count={len(lines)}",
        )
        return output
    except Exception as exc:
        return gh_client.handle_tool_error(
            "github_list_prs",
            cmd_desc,
            target,
            exc,
            "無法取得 PR 列表",
            active_token=active_token,
        )


async def github_get_pr(
    owner: str,
    repo: str,
    pull_number: int,
    github_token: str = "",
) -> str:
    """取得指定 Pull Request 的完整資訊（含 head SHA、分支方向、合併狀態、審查者與變更統計）。"""
    active_token = ""
    target = f"{owner}/{repo}#{pull_number}"
    cmd_desc = f"GET pulls/{pull_number}"
    try:
        clean_owner, clean_repo = security.check_repo_allowed(owner, repo)
        clean_num = validate_positive_int(pull_number, "pull_number")
        target = f"{clean_owner}/{clean_repo}#{clean_num}"

        headers = gh_client.get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")
        url = helpers.build_repo_url(clean_owner, clean_repo, f"/pulls/{clean_num}")

        async with gh_client.create_http_client() as client:
            data = await gh_client.request_json(client, "GET", url, headers=headers)

        if not isinstance(data, dict):
            raise ValueError("GitHub API 回傳的 Pull Request 資料格式異常。")

        title = data.get("title") or ""
        pr_state = "merged" if data.get("merged") is True else (data.get("state") or "")
        author = helpers.extract_login(data)
        head_obj = data.get("head") if isinstance(data.get("head"), dict) else {}
        base_obj = data.get("base") if isinstance(data.get("base"), dict) else {}
        head_label = head_obj.get("label") or head_obj.get("ref") or "?"
        base_label = base_obj.get("label") or base_obj.get("ref") or "?"
        head_sha = head_obj.get("sha") or ""

        header_lines = [
            f"標題：{title} (#{clean_num})",
            f"作者：{author}",
            f"狀態：{pr_state}" + (" [Draft]" if data.get("draft") is True else ""),
            f"分支：{head_label} -> {base_label}",
        ]
        if head_sha:
            header_lines.append(f"Head SHA：{head_sha}")

        mergeable = data.get("mergeable")
        mergeable_state = data.get("mergeable_state") or ""
        if mergeable is not None or mergeable_state:
            header_lines.append(f"可合併狀態：mergeable={mergeable}, state={mergeable_state or 'unknown'}")

        label_names = helpers.extract_label_names(data)
        if label_names:
            header_lines.append(f"標籤：{', '.join(label_names)}")

        assignees = helpers.extract_user_logins(data.get("assignees"))
        if assignees:
            header_lines.append(f"指派人：{', '.join(assignees)}")

        reviewers = helpers.extract_user_logins(data.get("requested_reviewers"))
        if reviewers:
            header_lines.append(f"待審查者：{', '.join(reviewers)}")

        commits_cnt = data.get("commits")
        changed_files = data.get("changed_files")
        additions = data.get("additions")
        deletions = data.get("deletions")
        if all(
            isinstance(v, int) and not isinstance(v, bool) for v in (commits_cnt, changed_files, additions, deletions)
        ):
            header_lines.append(f"變更統計：{commits_cnt} commits、{changed_files} 檔案 (+{additions} / -{deletions})")

        comments_cnt = data.get("comments", 0)
        review_comments_cnt = data.get("review_comments", 0)
        header_lines.append(f"留言統計：一般留言 {comments_cnt} 則、行內審查留言 {review_comments_cnt} 則")

        html_url = data.get("html_url") or ""
        if html_url:
            header_lines.append(f"網址：{html_url}")

        body = data.get("body") or ""
        raw_output = "\n".join(header_lines) + (f"\n\n{body}" if body else "")
        output, was_truncated = security.GITHUB_REDACTION.sanitize_and_truncate(
            raw_output,
            active_token,
            "內文過長已自動截斷前段。",
        )
        gh_client.AUDIT.log(
            "github_get_pr",
            cmd_desc,
            target=target,
            status="TRUNCATED" if was_truncated else "OK",
        )
        return output
    except Exception as exc:
        return gh_client.handle_tool_error(
            "github_get_pr",
            cmd_desc,
            target,
            exc,
            "無法取得 Pull Request 詳情",
            active_token=active_token,
        )


async def github_get_pr_files(
    owner: str,
    repo: str,
    pull_number: int,
    per_page: int = 30,
    page: int = 1,
    include_patch: bool = True,
    github_token: str = "",
) -> str:
    """取得指定 Pull Request 的變更檔案清單與差異內容（Diff Patch）。"""
    active_token = ""
    target = f"{owner}/{repo}#{pull_number}"
    cmd_desc = f"GET pulls/{pull_number}/files per_page={per_page} page={page}"
    try:
        clean_owner, clean_repo = security.check_repo_allowed(owner, repo)
        clean_num = validate_positive_int(pull_number, "pull_number")
        clean_per_page = validate_positive_int(per_page, "per_page", max_val=100)
        clean_page = validate_positive_int(page, "page")
        target = f"{clean_owner}/{clean_repo}#{clean_num}"

        headers = gh_client.get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")
        url = helpers.build_repo_url(clean_owner, clean_repo, f"/pulls/{clean_num}/files")

        async with gh_client.create_http_client() as client:
            files_data = await gh_client.request_json(
                client,
                "GET",
                url,
                headers=headers,
                params={"per_page": clean_per_page, "page": clean_page},
            )

        if not isinstance(files_data, list) or not files_data:
            gh_client.AUDIT.log("github_get_pr_files", cmd_desc, target=target, status="OK")
            return f"Pull Request {target} 目前沒有變更檔案（第 {clean_page} 頁）。"

        sections: list[str] = []
        for f_item in files_data:
            if not isinstance(f_item, dict):
                continue
            filename = f_item.get("filename") or "?"
            status = f_item.get("status") or "modified"
            additions = f_item.get("additions", 0)
            deletions = f_item.get("deletions", 0)
            header = f"檔案：{filename} [{status}] (+{additions} / -{deletions})"
            patch_text = f_item.get("patch")
            if include_patch and isinstance(patch_text, str) and patch_text.strip():
                clipped_patch = security.GITHUB_REDACTION.redact_and_clip_patch(
                    patch_text,
                    _MAX_SINGLE_PATCH_CHARS,
                    active_token,
                    "\n... [單一檔案 Diff 過長已截斷]",
                )
                sections.append(f"{header}\n```diff\n{clipped_patch}\n```")
            else:
                sections.append(header)

        output, was_truncated = security.GITHUB_REDACTION.sanitize_and_truncate(
            "\n\n".join(sections),
            active_token,
            "可設 include_patch=False 僅檢視檔案清單，或縮小 per_page 分頁檢視。",
        )
        gh_client.AUDIT.log(
            "github_get_pr_files",
            cmd_desc,
            target=target,
            status="TRUNCATED" if was_truncated else "OK",
            detail=f"files={len(sections)}",
        )
        return output
    except Exception as exc:
        return gh_client.handle_tool_error(
            "github_get_pr_files",
            cmd_desc,
            target,
            exc,
            "無法取得 PR 變更檔案",
            active_token=active_token,
        )


async def github_get_pr_reviews(
    owner: str,
    repo: str,
    pull_number: int,
    per_page: int = 30,
    page: int = 1,
    github_token: str = "",
) -> str:
    """取得指定 Pull Request 的 Code Review 審查紀錄與審查狀態。"""
    active_token = ""
    target = f"{owner}/{repo}#{pull_number}"
    cmd_desc = f"GET pulls/{pull_number}/reviews per_page={per_page} page={page}"
    try:
        clean_owner, clean_repo = security.check_repo_allowed(owner, repo)
        clean_num = validate_positive_int(pull_number, "pull_number")
        clean_per_page = validate_positive_int(per_page, "per_page", max_val=100)
        clean_page = validate_positive_int(page, "page")
        target = f"{clean_owner}/{clean_repo}#{clean_num}"

        headers = gh_client.get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")
        url = helpers.build_repo_url(clean_owner, clean_repo, f"/pulls/{clean_num}/reviews")

        async with gh_client.create_http_client() as client:
            reviews = await gh_client.request_json(
                client,
                "GET",
                url,
                headers=headers,
                params={"per_page": clean_per_page, "page": clean_page},
            )

        if not isinstance(reviews, list) or not reviews:
            gh_client.AUDIT.log("github_get_pr_reviews", cmd_desc, target=target, status="OK")
            return f"Pull Request {target} 目前沒有任何審查紀錄（第 {clean_page} 頁）。"

        blocks: list[str] = []
        for idx, rev in enumerate(reviews, start=(clean_page - 1) * clean_per_page + 1):
            if not isinstance(rev, dict):
                continue
            reviewer = helpers.extract_login(rev)
            rev_state = rev.get("state") or "UNKNOWN"
            submitted_at = rev.get("submitted_at") or ""
            body = (rev.get("body") or "").strip()
            header = f"--- Review #{idx} | 審查者：{reviewer} | 狀態：{rev_state} | 時間：{submitted_at} ---"
            blocks.append(f"{header}\n{body}" if body else header)

        output, was_truncated = security.GITHUB_REDACTION.sanitize_and_truncate(
            "\n\n".join(blocks),
            active_token,
            "請縮小 per_page 或搭配 page 分頁檢視。",
        )
        gh_client.AUDIT.log(
            "github_get_pr_reviews",
            cmd_desc,
            target=target,
            status="TRUNCATED" if was_truncated else "OK",
            detail=f"count={len(blocks)}",
        )
        return output
    except Exception as exc:
        return gh_client.handle_tool_error(
            "github_get_pr_reviews",
            cmd_desc,
            target,
            exc,
            "無法取得 PR 審查紀錄",
            active_token=active_token,
        )


async def github_get_pr_review_comments(
    owner: str,
    repo: str,
    pull_number: int,
    per_page: int = 30,
    page: int = 1,
    github_token: str = "",
) -> str:
    """讀取指定 Pull Request 掛載於程式碼檔案與行號上的行內審查留言（Inline Review Comments）。"""
    active_token = ""
    target = f"{owner}/{repo}#{pull_number}"
    cmd_desc = f"GET pulls/{pull_number}/comments per_page={per_page} page={page}"
    try:
        clean_owner, clean_repo = security.check_repo_allowed(owner, repo)
        clean_num = validate_positive_int(pull_number, "pull_number")
        clean_per_page = validate_positive_int(per_page, "per_page", max_val=100)
        clean_page = validate_positive_int(page, "page")
        target = f"{clean_owner}/{clean_repo}#{clean_num}"

        headers = gh_client.get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")
        url = helpers.build_repo_url(clean_owner, clean_repo, f"/pulls/{clean_num}/comments")

        async with gh_client.create_http_client() as client:
            comments = await gh_client.request_json(
                client,
                "GET",
                url,
                headers=headers,
                params={"per_page": clean_per_page, "page": clean_page},
            )

        if not isinstance(comments, list) or not comments:
            gh_client.AUDIT.log("github_get_pr_review_comments", cmd_desc, target=target, status="OK")
            return f"Pull Request {target} 目前沒有任何行內審查留言（第 {clean_page} 頁）。"

        blocks: list[str] = []
        for idx, item in enumerate(comments, start=(clean_page - 1) * clean_per_page + 1):
            if not isinstance(item, dict):
                continue
            cid = item.get("id", "?")
            path = item.get("path") or "?"
            line_no = item.get("line") or item.get("original_line") or "?"
            author = helpers.extract_login(item)
            created_at = item.get("created_at") or ""
            reply_to = item.get("in_reply_to_id")
            reply_suffix = f" | 回覆 #{reply_to}" if reply_to else ""
            header = (
                f"--- 行內留言 #{idx} (id={cid}) | 檔案：{path}:{line_no} | "
                f"作者：{author} | 時間：{created_at}{reply_suffix} ---"
            )
            parts = [header]
            diff_hunk = item.get("diff_hunk")
            if isinstance(diff_hunk, str) and diff_hunk.strip():
                clipped_hunk = security.GITHUB_REDACTION.redact_and_clip_patch(
                    diff_hunk.strip(),
                    _MAX_PR_CTX_PATCH_CHARS,
                    active_token,
                    "\n... [Diff 片段已截斷]",
                )
                parts.append(f"```diff\n{clipped_hunk}\n```")
            body = (item.get("body") or "").strip()
            if body:
                parts.append(body)
            blocks.append("\n".join(parts))

        output, was_truncated = security.GITHUB_REDACTION.sanitize_and_truncate(
            "\n\n".join(blocks),
            active_token,
            "請縮小 per_page 或搭配 page 分頁檢視。",
        )
        gh_client.AUDIT.log(
            "github_get_pr_review_comments",
            cmd_desc,
            target=target,
            status="TRUNCATED" if was_truncated else "OK",
            detail=f"count={len(blocks)}",
        )
        return output
    except Exception as exc:
        return gh_client.handle_tool_error(
            "github_get_pr_review_comments",
            cmd_desc,
            target,
            exc,
            "無法取得 PR 行內審查留言",
            active_token=active_token,
        )


async def github_prepare_pr_context(
    owner: str,
    repo: str,
    head: str,
    base: str = "",
    github_token: str = "",
) -> str:
    """讀取專案 PR 範本並比對 base...head 分支變更（除非使用者明確指定語言，否則預設以英文撰寫 PR 內容）。"""
    active_token = ""
    target = f"{owner}/{repo}"
    cmd_desc = f"GET compare head={head} base={base or 'default'}"
    try:
        clean_owner, clean_repo = security.check_repo_allowed(owner, repo)
        clean_head = security.validate_head_ref(head)
        target = f"{clean_owner}/{clean_repo}"

        headers = gh_client.get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")
        safe_path = helpers.repo_api_path(clean_owner, clean_repo)

        async with gh_client.create_http_client() as client:
            clean_base = await templates.resolve_base_branch(client, safe_path, headers, base)
            target = f"{clean_owner}/{clean_repo} ({clean_head}->{clean_base})"
            template_text, template_source = await templates.load_pr_template(client, safe_path, headers)

            safe_base = quote(clean_base, safe="")
            if ":" in clean_head:
                fk_owner, fk_branch = clean_head.split(":", 1)
                safe_head = f"{quote(fk_owner, safe='')}:{quote(fk_branch, safe='')}"
            else:
                safe_head = quote(clean_head, safe="")

            compare_url = f"{gh_client.get_api_base()}/repos/{safe_path}/compare/{safe_base}...{safe_head}"
            compare_data = await gh_client.request_json(client, "GET", compare_url, headers=headers)

        if not isinstance(compare_data, dict):
            raise ValueError("GitHub API 回傳的分支比較資料格式異常。")

        raw_commits = compare_data.get("commits")
        commits_list = raw_commits if isinstance(raw_commits, list) else []
        commit_lines: list[str] = []
        for c_item in commits_list[:30]:
            if not isinstance(c_item, dict):
                continue
            sha = str(c_item.get("sha") or "")[:7]
            c_obj = c_item.get("commit")
            msg_first = ""
            if isinstance(c_obj, dict) and c_obj.get("message"):
                msg_first = str(c_obj["message"]).splitlines()[0].strip()
            commit_lines.append(f"- {sha} {msg_first}".strip())

        raw_files = compare_data.get("files")
        files_list = raw_files if isinstance(raw_files, list) else []
        file_lines: list[str] = []
        for f_item in files_list[:50]:
            if not isinstance(f_item, dict):
                continue
            fname = f_item.get("filename") or "?"
            fstatus = f_item.get("status") or "modified"
            fadd = f_item.get("additions", 0)
            fdel = f_item.get("deletions", 0)
            line = f"- {fname} [{fstatus}] (+{fadd} / -{fdel})"
            patch_str = f_item.get("patch")
            if isinstance(patch_str, str) and patch_str.strip():
                snippet = security.GITHUB_REDACTION.redact_and_clip_patch(
                    patch_str.strip(),
                    _MAX_PR_CTX_PATCH_CHARS,
                    active_token,
                    "\n... [Diff 摘要已截斷]",
                )
                line += f"\n```diff\n{snippet}\n```"
            file_lines.append(line)

        commits_block = "\n".join(commit_lines) if commit_lines else "（無 Commit 紀錄）"
        files_block = "\n".join(file_lines) if file_lines else "（無檔案變更）"
        raw_output = (
            f"目標儲存庫：{clean_owner}/{clean_repo}\n"
            f"合併方向：{clean_head} -> {clean_base}\n"
            f"範本來源：{template_source}\n\n"
            f"=== PR 範本內容 ===\n{template_text}\n\n"
            f"=== 分支 Commit 列表 (共 {len(commits_list)} 筆) ===\n{commits_block}\n\n"
            f"=== 變更檔案摘要 (共 {len(files_list)} 檔) ===\n{files_block}\n\n"
            "=== 執行規範 ===\n"
            "1. 語言規範（Language Policy）：除非使用者明確指定使用的語言，"
            "否則產生的 PR Title 與 PR Body 內容**一律預設使用英文（English）**撰寫。\n"
            "2. 請依據上述範本與變更內容，撰寫結構完整的英文 PR Title（建議採 Conventional Commits 格式）與 PR Body。\n"
            f"3. 完成後請呼叫 github_create_pr（head={clean_head!r}, base={clean_base!r}）；"
            "系統會在終端機畫面完整列出預計執行的動作與 PR 內容，待使用者同意後才會送出，且絕不自動執行 Merge。\n"
            "4. 證據誠信（Evidence Integrity）：僅可將本次對話已實際確認完成的項目勾選為 `[x]`；\n"
            "   未在本機實測之項目請保留 `[ ]`，並於 Testing 區段誠實標註 `Pending CI` 或待人工驗證項目，"
            "不得預先宣稱通過。"
        )

        output, was_truncated = security.GITHUB_REDACTION.sanitize_and_truncate(
            raw_output,
            active_token,
            "變更內容過長已截斷尾端，請直接依據現有摘要撰寫 PR。",
        )
        gh_client.AUDIT.log(
            "github_prepare_pr_context",
            cmd_desc,
            target=target,
            status="TRUNCATED" if was_truncated else "OK",
            detail=f"commits={len(commits_list)},files={len(files_list)}",
        )
        return output
    except Exception as exc:
        return gh_client.handle_tool_error(
            "github_prepare_pr_context",
            cmd_desc,
            target,
            exc,
            t("github_mcp.pr_ctx_failed"),
            active_token=active_token,
        )


async def github_create_pr(
    owner: str,
    repo: str,
    title: str,
    head: str,
    base: str = "",
    body: str = "",
    draft: bool = False,
    labels: list[str] | None = None,
    reviewers: list[str] | None = None,
    github_token: str = "",
) -> str:
    """在指定儲存庫建立 Pull Request（可選附加標籤與審查者；絕不自動執行 Merge）。"""
    active_token = ""
    target = f"{owner}/{repo}"
    cmd_desc = f"POST pulls head={head} base={base or 'default'} draft={draft}"
    try:
        clean_owner, clean_repo = security.check_repo_allowed(owner, repo)
        clean_head = security.validate_head_ref(head)
        target = f"{clean_owner}/{clean_repo}"

        clean_title = helpers.validate_single_line(
            title,
            "title",
            empty_msg=t("github_mcp.pr_title_empty"),
            invalid_msg=t("github_mcp.pr_title_invalid_chars"),
        )
        clean_body = body if isinstance(body, str) else ""
        clean_labels = helpers.clean_string_list(labels, "labels")
        clean_reviewers = helpers.clean_string_list(reviewers, "reviewers")

        headers = gh_client.get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")

        combined_outbound = "\n".join([clean_title, clean_body, " ".join(clean_labels), " ".join(clean_reviewers)])
        block_reason = security.GITHUB_REDACTION.check_outbound_content(combined_outbound, active_token)
        if block_reason:
            gh_client.AUDIT.log(
                "github_create_pr",
                cmd_desc,
                target=target,
                status="BLOCKED",
                detail="outbound_policy_blocked",
            )
            return t("github_mcp.pr_outbound_blocked", reason=block_reason)

        safe_path = helpers.repo_api_path(clean_owner, clean_repo)
        api_base = gh_client.get_api_base()
        followup_notes: list[str] = []
        async with gh_client.create_http_client() as client:
            clean_base = await templates.resolve_base_branch(client, safe_path, headers, base)
            if clean_head == clean_base:
                raise ValueError(f"來源分支 (head={clean_head!r}) 與目標分支 (base={clean_base!r}) 不可相同。")
            url = f"{api_base}/repos/{safe_path}/pulls"
            payload: dict[str, Any] = {
                "title": clean_title,
                "head": clean_head,
                "base": clean_base,
                "body": clean_body,
                "draft": bool(draft),
            }
            created = await gh_client.request_json(client, "POST", url, headers=headers, json_body=payload)

            pr_num = created.get("number", "?") if isinstance(created, dict) else "?"
            if isinstance(pr_num, int) and not isinstance(pr_num, bool):
                if clean_labels:
                    try:
                        await gh_client.request_json(
                            client,
                            "POST",
                            f"{api_base}/repos/{safe_path}/issues/{pr_num}/labels",
                            headers=headers,
                            json_body={"labels": clean_labels},
                        )
                    except Exception as sub_exc:
                        safe_sub = security.GITHUB_REDACTION.clip_error_text(str(sub_exc), active_token, limit=160)
                        followup_notes.append(f"標籤套用失敗：{safe_sub}")
                if clean_reviewers:
                    try:
                        await gh_client.request_json(
                            client,
                            "POST",
                            f"{api_base}/repos/{safe_path}/pulls/{pr_num}/requested_reviewers",
                            headers=headers,
                            json_body={"reviewers": clean_reviewers},
                        )
                    except Exception as sub_exc:
                        safe_sub = security.GITHUB_REDACTION.clip_error_text(str(sub_exc), active_token, limit=160)
                        followup_notes.append(f"審查者指派失敗：{safe_sub}")

        html_url = created.get("html_url", "") if isinstance(created, dict) else ""
        url_line = f"\nPR 網址：{html_url}" if html_url else ""
        note_line = f"\n⚠ 附註：{'；'.join(followup_notes)}" if followup_notes else ""
        gh_client.AUDIT.log(
            "github_create_pr",
            cmd_desc,
            target=f"{target}#{pr_num}",
            status="OK",
            detail=f"chars={len(clean_title) + len(clean_body)}",
        )
        return (
            f"成功在 {clean_owner}/{clean_repo} 建立 Pull Request #{pr_num}"
            f"（{clean_head} -> {clean_base}）：{clean_title}{url_line}{note_line}\n"
            "（本工具僅建立 PR 單，不會自動執行 Merge；請前往 GitHub 網站確認並執行合併。）"
        )
    except Exception as exc:
        return gh_client.handle_tool_error(
            "github_create_pr",
            cmd_desc,
            target,
            exc,
            t("github_mcp.pr_create_failed"),
            active_token=active_token,
        )


async def github_update_pr(
    owner: str,
    repo: str,
    pull_number: int,
    title: str | None = None,
    body: str | None = None,
    state: str | None = None,
    base: str | None = None,
    github_token: str = "",
) -> str:
    """更新既有 Pull Request 的標題、內文、狀態（open/closed）或目標分支（base）。"""
    active_token = ""
    target = f"{owner}/{repo}#{pull_number}"
    cmd_desc = f"PATCH pulls/{pull_number}"
    try:
        clean_owner, clean_repo = security.check_repo_allowed(owner, repo)
        clean_num = validate_positive_int(pull_number, "pull_number")
        target = f"{clean_owner}/{clean_repo}#{clean_num}"

        payload: dict[str, Any] = {}
        outbound_parts: list[str] = []
        if title is not None:
            clean_title = helpers.validate_single_line(
                title,
                "title",
                empty_msg=t("github_mcp.pr_title_empty"),
                invalid_msg=t("github_mcp.pr_title_invalid_chars"),
            )
            payload["title"] = clean_title
            outbound_parts.append(clean_title)
        if body is not None:
            if not isinstance(body, str):
                raise ValueError("PR 內文 (body) 必須為字串。")
            payload["body"] = body
            outbound_parts.append(body)
        if state is not None:
            payload["state"] = security.validate_update_state(state)
        if base is not None:
            payload["base"] = security.validate_git_ref(base)

        if not payload:
            raise ValueError("至少須指定一項要更新的欄位（title、body、state 或 base）。")

        headers = gh_client.get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")

        if outbound_parts:
            block_reason = security.GITHUB_REDACTION.check_outbound_content("\n".join(outbound_parts), active_token)
            if block_reason:
                gh_client.AUDIT.log(
                    "github_update_pr",
                    cmd_desc,
                    target=target,
                    status="BLOCKED",
                    detail="outbound_policy_blocked",
                )
                return t("github_mcp.pr_outbound_blocked", reason=block_reason)

        url = helpers.build_repo_url(clean_owner, clean_repo, f"/pulls/{clean_num}")
        async with gh_client.create_http_client() as client:
            updated = await gh_client.request_json(client, "PATCH", url, headers=headers, json_body=payload)

        html_url = updated.get("html_url", "") if isinstance(updated, dict) else ""
        url_line = f"\nPR 網址：{html_url}" if html_url else ""
        fields_str = "、".join(payload.keys())
        gh_client.AUDIT.log(
            "github_update_pr",
            cmd_desc,
            target=target,
            status="OK",
            detail=f"fields={','.join(payload.keys())}",
        )
        return f"成功更新 {target}（欄位：{fields_str}）。{url_line}"
    except Exception as exc:
        return gh_client.handle_tool_error(
            "github_update_pr",
            cmd_desc,
            target,
            exc,
            "無法更新 Pull Request",
            active_token=active_token,
        )


async def github_create_pr_review(
    owner: str,
    repo: str,
    pull_number: int,
    event: str = "COMMENT",
    body: str = "",
    comments: list[dict[str, Any]] | None = None,
    commit_id: str = "",
    github_token: str = "",
) -> str:
    """對指定 Pull Request 提交審查意見（僅允許 COMMENT 或 REQUEST_CHANGES，支援行內留言）。"""
    active_token = ""
    target = f"{owner}/{repo}#{pull_number}"
    cmd_desc = f"POST pulls/{pull_number}/reviews event={event}"
    try:
        clean_owner, clean_repo = security.check_repo_allowed(owner, repo)
        clean_num = validate_positive_int(pull_number, "pull_number")
        target = f"{clean_owner}/{clean_repo}#{clean_num}"

        clean_event = security.validate_review_event(event)
        clean_body = body.strip() if isinstance(body, str) else ""
        clean_commit = security.validate_git_ref(commit_id) if commit_id and commit_id.strip() else ""

        clean_comments: list[dict[str, Any]] = []
        outbound_parts: list[str] = [clean_body]
        if comments is not None:
            if not isinstance(comments, list):
                raise ValueError("comments 參數必須為物件陣列。")
            if len(comments) > _MAX_REVIEW_COMMENTS:
                raise ValueError(f"單次審查行內留言數量過多（上限 {_MAX_REVIEW_COMMENTS} 筆）。")
            for idx, item in enumerate(comments, start=1):
                if not isinstance(item, dict):
                    raise ValueError(f"第 {idx} 筆行內留言格式錯誤，必須為物件。")
                c_path = security.validate_repo_file_path(str(item.get("path") or ""), allow_empty=False)
                c_line = validate_positive_int(item.get("line"), f"comments[{idx}].line")  # type: ignore[arg-type]
                raw_c_body = item.get("body")
                if not isinstance(raw_c_body, str) or not raw_c_body.strip():
                    raise ValueError(f"第 {idx} 筆行內留言內容 (body) 不可為空白。")
                c_body = raw_c_body.strip()
                c_side_raw = str(item.get("side") or "RIGHT").strip().upper()
                if c_side_raw not in security.ALLOWED_DIFF_SIDES:
                    raise ValueError(f"第 {idx} 筆行內留言 side 無效：{c_side_raw!r}（僅允許 LEFT 或 RIGHT）。")
                clean_comments.append({"path": c_path, "line": c_line, "side": c_side_raw, "body": c_body})
                outbound_parts.extend([c_path, c_body])

        if clean_event == "REQUEST_CHANGES" and not clean_body:
            raise ValueError("提交 REQUEST_CHANGES 審查時，body 不可為空白。")
        if not clean_body and not clean_comments:
            raise ValueError("請提供審查總結 (body) 或至少一筆行內留言 (comments)。")

        headers = gh_client.get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")

        block_reason = security.GITHUB_REDACTION.check_outbound_content("\n".join(outbound_parts), active_token)
        if block_reason:
            gh_client.AUDIT.log(
                "github_create_pr_review",
                cmd_desc,
                target=target,
                status="BLOCKED",
                detail="outbound_policy_blocked",
            )
            return f"[ERROR] 安全攔截：PR 審查內容疑似包含機密（{block_reason}），已阻斷外發以防外洩。"

        payload: dict[str, Any] = {"event": clean_event}
        if clean_body:
            payload["body"] = clean_body
        if clean_comments:
            payload["comments"] = clean_comments
        if clean_commit:
            payload["commit_id"] = clean_commit

        url = helpers.build_repo_url(clean_owner, clean_repo, f"/pulls/{clean_num}/reviews")
        async with gh_client.create_http_client() as client:
            created = await gh_client.request_json(client, "POST", url, headers=headers, json_body=payload)

        review_id = created.get("id", "?") if isinstance(created, dict) else "?"
        gh_client.AUDIT.log(
            "github_create_pr_review",
            cmd_desc,
            target=target,
            status="OK",
            detail=f"event={clean_event},inline={len(clean_comments)}",
        )
        return (
            f"成功在 {target} 提交 PR Review #{review_id}（事件：{clean_event}，行內留言：{len(clean_comments)} 筆）。"
        )
    except Exception as exc:
        return gh_client.handle_tool_error(
            "github_create_pr_review",
            cmd_desc,
            target,
            exc,
            "無法提交 PR 審查意見",
            active_token=active_token,
        )


def register(mcp: Any) -> None:
    """註冊 GitHub Pull Request 與審查工具至 MCPServer。"""
    for fn in (
        github_list_prs,
        github_get_pr,
        github_get_pr_files,
        github_get_pr_reviews,
        github_get_pr_review_comments,
        github_prepare_pr_context,
        github_create_pr,
        github_update_pr,
        github_create_pr_review,
    ):
        mcp.tool()(fn)
