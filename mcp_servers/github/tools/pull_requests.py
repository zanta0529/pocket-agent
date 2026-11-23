"""GitHub Pull Request、Diff、Review 與 CI Checks 工具群組（Command / Registrar Pattern）。"""

from typing import Any
from urllib.parse import quote

from mcp_servers._shared import t, validate_positive_int
from mcp_servers.github import client as gh_client
from mcp_servers.github import security, templates

_MAX_SINGLE_PATCH_CHARS = 3000
_MAX_PR_CTX_PATCH_CHARS = 600


def _extract_login(item: dict[str, Any]) -> str:
    user_obj = item.get("user")
    if isinstance(user_obj, dict) and user_obj.get("login"):
        return str(user_obj["login"])
    return "ghost"


async def github_list_prs(
    owner: str,
    repo: str,
    state: str = "open",
    per_page: int = 30,
    page: int = 1,
    github_token: str = "",
) -> str:
    """列出指定儲存庫的 Pull Requests（支援 state 與分頁查詢）。"""
    active_token = ""
    target = f"{owner}/{repo}"
    cmd_desc = f"GET pulls state={state} per_page={per_page} page={page}"
    try:
        clean_owner, clean_repo = security.check_repo_allowed(owner, repo)
        target = f"{clean_owner}/{clean_repo}"
        norm_state = security.validate_state(state)
        clean_per_page = validate_positive_int(per_page, "per_page", max_val=100)
        clean_page = validate_positive_int(page, "page")

        headers = gh_client.get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")
        safe_path = f"{quote(clean_owner, safe='')}/{quote(clean_repo, safe='')}"
        url = f"{gh_client.GITHUB_API_BASE}/repos/{safe_path}/pulls"

        async with gh_client.create_http_client() as client:
            prs = await gh_client.request_json(
                client,
                "GET",
                url,
                headers=headers,
                params={"state": norm_state, "per_page": clean_per_page, "page": clean_page},
            )

        if not isinstance(prs, list) or not prs:
            gh_client.AUDIT.log("github_list_prs", cmd_desc, target=target, status="OK")
            return f"目前沒有狀態為 {norm_state} 的 Pull Requests（第 {clean_page} 頁）。"

        lines: list[str] = []
        for pr in prs:
            if not isinstance(pr, dict):
                continue
            num = pr.get("number", "?")
            title = (pr.get("title") or "").strip()
            line = f"#{num} {title} ({_extract_login(pr)})"
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
        safe_path = f"{quote(clean_owner, safe='')}/{quote(clean_repo, safe='')}"
        url = f"{gh_client.GITHUB_API_BASE}/repos/{safe_path}/pulls/{clean_num}/files"

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
        safe_path = f"{quote(clean_owner, safe='')}/{quote(clean_repo, safe='')}"
        url = f"{gh_client.GITHUB_API_BASE}/repos/{safe_path}/pulls/{clean_num}/reviews"

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
            reviewer = _extract_login(rev)
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
        safe_path = f"{quote(clean_owner, safe='')}/{quote(clean_repo, safe='')}"
        safe_ref = quote(clean_ref, safe="")
        url = f"{gh_client.GITHUB_API_BASE}/repos/{safe_path}/commits/{safe_ref}/check-runs"

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
            line = f"- {name}: [{state_str}]"
            out_obj = run.get("output")
            if isinstance(out_obj, dict) and out_obj.get("title"):
                line += f" — {str(out_obj['title']).strip()}"
            lines.append(line)

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
        safe_path = f"{quote(clean_owner, safe='')}/{quote(clean_repo, safe='')}"

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

            compare_url = f"{gh_client.GITHUB_API_BASE}/repos/{safe_path}/compare/{safe_base}...{safe_head}"
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
    github_token: str = "",
) -> str:
    """在指定儲存庫建立 Pull Request（預設應以英文撰寫 title 與 body，除非使用者指定其他語言；絕不自動執行 Merge）。"""
    active_token = ""
    target = f"{owner}/{repo}"
    cmd_desc = f"POST pulls head={head} base={base or 'default'} draft={draft}"
    try:
        clean_owner, clean_repo = security.check_repo_allowed(owner, repo)
        clean_head = security.validate_head_ref(head)
        target = f"{clean_owner}/{clean_repo}"

        if not isinstance(title, str) or not title.strip():
            raise ValueError(t("github_mcp.pr_title_empty"))
        clean_title = title.strip()
        if any(ch in clean_title for ch in ("\r", "\n", "\x00")):
            raise ValueError(t("github_mcp.pr_title_invalid_chars"))

        clean_body = body if isinstance(body, str) else ""
        headers = gh_client.get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")

        combined_outbound = f"{clean_title}\n{clean_body}"
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

        safe_path = f"{quote(clean_owner, safe='')}/{quote(clean_repo, safe='')}"
        async with gh_client.create_http_client() as client:
            clean_base = await templates.resolve_base_branch(client, safe_path, headers, base)
            if clean_head == clean_base:
                raise ValueError(f"來源分支 (head={clean_head!r}) 與目標分支 (base={clean_base!r}) 不可相同。")
            url = f"{gh_client.GITHUB_API_BASE}/repos/{safe_path}/pulls"
            payload: dict[str, Any] = {
                "title": clean_title,
                "head": clean_head,
                "base": clean_base,
                "body": clean_body,
                "draft": bool(draft),
            }
            created = await gh_client.request_json(client, "POST", url, headers=headers, json_body=payload)

        pr_num = created.get("number", "?") if isinstance(created, dict) else "?"
        html_url = created.get("html_url", "") if isinstance(created, dict) else ""
        url_line = f"\nPR 網址：{html_url}" if html_url else ""
        gh_client.AUDIT.log(
            "github_create_pr",
            cmd_desc,
            target=f"{target}#{pr_num}",
            status="OK",
            detail=f"chars={len(clean_title) + len(clean_body)}",
        )
        return (
            f"成功在 {clean_owner}/{clean_repo} 建立 Pull Request #{pr_num}"
            f"（{clean_head} -> {clean_base}）：{clean_title}{url_line}\n"
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


def register(mcp: Any) -> None:
    """註冊 GitHub Pull Request 與 CI 工具至 MCPServer。"""
    for fn in (
        github_list_prs,
        github_get_pr_files,
        github_get_pr_reviews,
        github_get_pr_checks,
        github_prepare_pr_context,
        github_create_pr,
    ):
        mcp.tool()(fn)
