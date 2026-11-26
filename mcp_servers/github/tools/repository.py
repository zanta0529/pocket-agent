"""GitHub 儲存庫唯讀探索工具群組（Command / Registrar Pattern）。"""

import base64
from typing import Any

from mcp_servers._shared import validate_positive_int
from mcp_servers.github import client as gh_client
from mcp_servers.github import helpers, security

_MAX_RELEASE_BODY_CHARS = 600


async def github_get_repo(
    owner: str,
    repo: str,
    github_token: str = "",
) -> str:
    """查詢指定儲存庫的預設分支、可見性、語言、Topics 與基本狀態。"""
    active_token = ""
    target = f"{owner}/{repo}"
    cmd_desc = "GET repo"
    try:
        clean_owner, clean_repo = security.check_repo_allowed(owner, repo)
        target = f"{clean_owner}/{clean_repo}"

        headers = gh_client.get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")
        url = helpers.build_repo_url(clean_owner, clean_repo)

        async with gh_client.create_http_client() as client:
            data = await gh_client.request_json(client, "GET", url, headers=headers)

        if not isinstance(data, dict):
            raise ValueError("GitHub API 回傳的儲存庫資料格式異常。")

        full_name = data.get("full_name") or target
        visibility = data.get("visibility") or ("private" if data.get("private") else "public")
        default_branch = data.get("default_branch") or "main"
        language = data.get("language") or "未標記"
        archived = "是" if data.get("archived") else "否"
        open_issues = data.get("open_issues_count", 0)
        stars = data.get("stargazers_count", 0)
        forks = data.get("forks_count", 0)
        topics = data.get("topics") if isinstance(data.get("topics"), list) else []
        desc = (data.get("description") or "").strip()
        html_url = data.get("html_url") or ""

        lines = [
            f"儲存庫：{full_name} [{visibility}]",
            f"預設分支：{default_branch}",
            f"主要語言：{language}",
            f"已封存：{archived}",
            f"統計：Stars {stars} | Forks {forks} | Open Issues/PRs {open_issues}",
        ]
        if topics:
            lines.append(f"Topics：{', '.join(str(tp) for tp in topics)}")
        if html_url:
            lines.append(f"網址：{html_url}")
        if desc:
            lines.append(f"\n簡介：{desc}")

        output, was_truncated = security.GITHUB_REDACTION.sanitize_and_truncate(
            "\n".join(lines),
            active_token,
            "儲存庫資訊過長已截斷。",
        )
        gh_client.AUDIT.log(
            "github_get_repo",
            cmd_desc,
            target=target,
            status="TRUNCATED" if was_truncated else "OK",
        )
        return output
    except Exception as exc:
        return gh_client.handle_tool_error(
            "github_get_repo",
            cmd_desc,
            target,
            exc,
            "無法取得儲存庫資訊",
            active_token=active_token,
        )


async def github_list_branches(
    owner: str,
    repo: str,
    protected_only: bool = False,
    per_page: int = 30,
    page: int = 1,
    github_token: str = "",
) -> str:
    """列出指定儲存庫的遠端分支清單。"""
    active_token = ""
    target = f"{owner}/{repo}"
    cmd_desc = f"GET branches protected={protected_only} per_page={per_page} page={page}"
    try:
        clean_owner, clean_repo = security.check_repo_allowed(owner, repo)
        target = f"{clean_owner}/{clean_repo}"
        clean_per_page = validate_positive_int(per_page, "per_page", max_val=100)
        clean_page = validate_positive_int(page, "page")

        params: dict[str, str | int] = {"per_page": clean_per_page, "page": clean_page}
        if protected_only:
            params["protected"] = "true"

        headers = gh_client.get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")
        url = helpers.build_repo_url(clean_owner, clean_repo, "/branches")

        async with gh_client.create_http_client() as client:
            branches = await gh_client.request_json(client, "GET", url, headers=headers, params=params)

        if not isinstance(branches, list) or not branches:
            gh_client.AUDIT.log("github_list_branches", cmd_desc, target=target, status="OK")
            return f"目前 {target} 沒有符合條件的分支（第 {clean_page} 頁）。"

        lines: list[str] = []
        for br in branches:
            if not isinstance(br, dict):
                continue
            name = br.get("name") or "?"
            commit_obj = br.get("commit") if isinstance(br.get("commit"), dict) else {}
            sha = str(commit_obj.get("sha") or "")[:7]
            prot_tag = " [protected]" if br.get("protected") is True else ""
            lines.append(f"- {name} ({sha}){prot_tag}")

        output, was_truncated = security.GITHUB_REDACTION.sanitize_and_truncate(
            "\n".join(lines),
            active_token,
            "請縮小 per_page 或搭配 page 分頁檢視。",
        )
        gh_client.AUDIT.log(
            "github_list_branches",
            cmd_desc,
            target=target,
            status="TRUNCATED" if was_truncated else "OK",
            detail=f"count={len(lines)}",
        )
        return output
    except Exception as exc:
        return gh_client.handle_tool_error(
            "github_list_branches",
            cmd_desc,
            target,
            exc,
            "無法取得分支列表",
            active_token=active_token,
        )


async def github_list_tags(
    owner: str,
    repo: str,
    per_page: int = 30,
    page: int = 1,
    github_token: str = "",
) -> str:
    """列出指定儲存庫的 Git Tags 清單。"""
    active_token = ""
    target = f"{owner}/{repo}"
    cmd_desc = f"GET tags per_page={per_page} page={page}"
    try:
        clean_owner, clean_repo = security.check_repo_allowed(owner, repo)
        target = f"{clean_owner}/{clean_repo}"
        clean_per_page = validate_positive_int(per_page, "per_page", max_val=100)
        clean_page = validate_positive_int(page, "page")

        headers = gh_client.get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")
        url = helpers.build_repo_url(clean_owner, clean_repo, "/tags")

        async with gh_client.create_http_client() as client:
            tags = await gh_client.request_json(
                client,
                "GET",
                url,
                headers=headers,
                params={"per_page": clean_per_page, "page": clean_page},
            )

        if not isinstance(tags, list) or not tags:
            gh_client.AUDIT.log("github_list_tags", cmd_desc, target=target, status="OK")
            return f"目前 {target} 沒有任何 Tags（第 {clean_page} 頁）。"

        lines: list[str] = []
        for tag in tags:
            if not isinstance(tag, dict):
                continue
            name = tag.get("name") or "?"
            commit_obj = tag.get("commit") if isinstance(tag.get("commit"), dict) else {}
            sha = str(commit_obj.get("sha") or "")[:7]
            lines.append(f"- {name} ({sha})")

        output, was_truncated = security.GITHUB_REDACTION.sanitize_and_truncate(
            "\n".join(lines),
            active_token,
            "請縮小 per_page 或搭配 page 分頁檢視。",
        )
        gh_client.AUDIT.log(
            "github_list_tags",
            cmd_desc,
            target=target,
            status="TRUNCATED" if was_truncated else "OK",
            detail=f"count={len(lines)}",
        )
        return output
    except Exception as exc:
        return gh_client.handle_tool_error(
            "github_list_tags",
            cmd_desc,
            target,
            exc,
            "無法取得 Tags 列表",
            active_token=active_token,
        )


async def github_list_commits(
    owner: str,
    repo: str,
    sha: str = "",
    path: str = "",
    author: str = "",
    per_page: int = 20,
    page: int = 1,
    github_token: str = "",
) -> str:
    """列出指定儲存庫的 Commit 歷史紀錄（支援 sha／分支、檔案路徑與作者篩選）。"""
    active_token = ""
    target = f"{owner}/{repo}"
    cmd_desc = f"GET commits per_page={per_page} page={page}"
    try:
        clean_owner, clean_repo = security.check_repo_allowed(owner, repo)
        target = f"{clean_owner}/{clean_repo}"
        clean_per_page = validate_positive_int(per_page, "per_page", max_val=100)
        clean_page = validate_positive_int(page, "page")

        params: dict[str, str | int] = {"per_page": clean_per_page, "page": clean_page}
        if sha and sha.strip():
            params["sha"] = security.validate_git_ref(sha)
        if path and path.strip():
            params["path"] = security.validate_repo_file_path(path, allow_empty=False)
        if author and author.strip():
            params["author"] = security.validate_filter_text(author, "author")

        headers = gh_client.get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")
        url = helpers.build_repo_url(clean_owner, clean_repo, "/commits")

        async with gh_client.create_http_client() as client:
            commits = await gh_client.request_json(client, "GET", url, headers=headers, params=params)

        if not isinstance(commits, list) or not commits:
            gh_client.AUDIT.log("github_list_commits", cmd_desc, target=target, status="OK")
            return f"目前 {target} 沒有符合條件的 Commit 紀錄（第 {clean_page} 頁）。"

        lines: list[str] = []
        for item in commits:
            if not isinstance(item, dict):
                continue
            short_sha = str(item.get("sha") or "")[:7]
            commit_obj = item.get("commit") if isinstance(item.get("commit"), dict) else {}
            msg_first = ""
            if commit_obj.get("message"):
                msg_first = str(commit_obj["message"]).splitlines()[0].strip()
            c_author = commit_obj.get("author") if isinstance(commit_obj.get("author"), dict) else {}
            author_name = c_author.get("name") or helpers.extract_login(item, field="author")
            commit_date = c_author.get("date") or ""
            lines.append(f"- {short_sha} {msg_first} ({author_name}, {commit_date})")

        output, was_truncated = security.GITHUB_REDACTION.sanitize_and_truncate(
            "\n".join(lines),
            active_token,
            "請縮小 per_page 或搭配 page 分頁檢視。",
        )
        gh_client.AUDIT.log(
            "github_list_commits",
            cmd_desc,
            target=target,
            status="TRUNCATED" if was_truncated else "OK",
            detail=f"count={len(lines)}",
        )
        return output
    except Exception as exc:
        return gh_client.handle_tool_error(
            "github_list_commits",
            cmd_desc,
            target,
            exc,
            "無法取得 Commit 列表",
            active_token=active_token,
        )


async def github_get_file_content(
    owner: str,
    repo: str,
    path: str = "",
    ref: str = "",
    github_token: str = "",
) -> str:
    """唯讀取得指定儲存庫與 ref 下的單一檔案內容或目錄清單。"""
    active_token = ""
    target = f"{owner}/{repo}"
    cmd_desc = f"GET contents path={path or '/'} ref={ref or 'default'}"
    try:
        clean_owner, clean_repo = security.check_repo_allowed(owner, repo)
        clean_path = security.validate_repo_file_path(path, allow_empty=True)
        clean_ref = security.validate_git_ref(ref) if ref and ref.strip() else ""
        target = f"{clean_owner}/{clean_repo}:{clean_path or '/'}"

        headers = gh_client.get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")
        encoded_path = helpers.encode_repo_path(clean_path)
        suffix = f"/contents/{encoded_path}" if encoded_path else "/contents"
        url = helpers.build_repo_url(clean_owner, clean_repo, suffix)
        params: dict[str, str | int] | None = {"ref": clean_ref} if clean_ref else None

        async with gh_client.create_http_client() as client:
            data = await gh_client.request_json(client, "GET", url, headers=headers, params=params)

        if isinstance(data, list):
            sorted_entries = sorted(
                (e for e in data if isinstance(e, dict)),
                key=lambda e: (0 if e.get("type") == "dir" else 1, str(e.get("name") or "")),
            )
            if not sorted_entries:
                gh_client.AUDIT.log("github_get_file_content", cmd_desc, target=target, status="OK")
                return f"目錄 {clean_path or '/'} 為空。"
            lines = [f"目錄：{clean_path or '/'} (ref={clean_ref or 'default'})"]
            for entry in sorted_entries:
                etype = entry.get("type") or "file"
                ename = entry.get("name") or "?"
                esize = entry.get("size", 0)
                if etype == "dir":
                    lines.append(f"- [dir] {ename}/")
                else:
                    lines.append(f"- [{etype}] {ename} ({esize} bytes)")
            output, was_truncated = security.GITHUB_REDACTION.sanitize_and_truncate(
                "\n".join(lines),
                active_token,
                "目錄項目過多已截斷。",
            )
            gh_client.AUDIT.log(
                "github_get_file_content",
                cmd_desc,
                target=target,
                status="TRUNCATED" if was_truncated else "OK",
                detail=f"entries={len(sorted_entries)}",
            )
            return output

        if not isinstance(data, dict):
            raise ValueError("GitHub API 回傳的檔案內容格式異常。")

        item_type = data.get("type") or "file"
        file_size = data.get("size", 0)
        file_sha = data.get("sha") or ""
        if item_type != "file":
            gh_client.AUDIT.log("github_get_file_content", cmd_desc, target=target, status="OK")
            return f"目標 {clean_path} 類型為 {item_type}（非一般檔案，sha={file_sha}）。"

        if isinstance(file_size, int) and file_size > security.MAX_RESPONSE_BYTES:
            raise ValueError(f"檔案過大（{file_size} bytes，超過 {security.MAX_RESPONSE_BYTES} 位元組上限）。")

        raw_content = data.get("content")
        if not isinstance(raw_content, str) or not raw_content.strip():
            gh_client.AUDIT.log("github_get_file_content", cmd_desc, target=target, status="OK")
            return f"檔案：{clean_path} ({file_size} bytes, sha={file_sha}) 目前無可解碼的內文資料。"

        raw_bytes = base64.b64decode(raw_content)
        if b"\x00" in raw_bytes:
            gh_client.AUDIT.log("github_get_file_content", cmd_desc, target=target, status="OK")
            return f"檔案：{clean_path} 為二進位檔案（{len(raw_bytes)} bytes, sha={file_sha}），不予顯示純文字內容。"

        decoded_text = raw_bytes.decode("utf-8", errors="replace")
        header = f"檔案：{clean_path} (ref={clean_ref or 'default'}, {file_size} bytes)"
        output, was_truncated = security.GITHUB_REDACTION.sanitize_and_truncate(
            f"{header}\n\n{decoded_text}",
            active_token,
            "檔案內容過長已截斷。",
        )
        gh_client.AUDIT.log(
            "github_get_file_content",
            cmd_desc,
            target=target,
            status="TRUNCATED" if was_truncated else "OK",
        )
        return output
    except Exception as exc:
        return gh_client.handle_tool_error(
            "github_get_file_content",
            cmd_desc,
            target,
            exc,
            "無法取得檔案或目錄內容",
            active_token=active_token,
        )


async def github_list_releases(
    owner: str,
    repo: str,
    per_page: int = 10,
    page: int = 1,
    github_token: str = "",
) -> str:
    """列出指定儲存庫的 Releases 發行版本清單與摘要。"""
    active_token = ""
    target = f"{owner}/{repo}"
    cmd_desc = f"GET releases per_page={per_page} page={page}"
    try:
        clean_owner, clean_repo = security.check_repo_allowed(owner, repo)
        target = f"{clean_owner}/{clean_repo}"
        clean_per_page = validate_positive_int(per_page, "per_page", max_val=100)
        clean_page = validate_positive_int(page, "page")

        headers = gh_client.get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")
        url = helpers.build_repo_url(clean_owner, clean_repo, "/releases")

        async with gh_client.create_http_client() as client:
            releases = await gh_client.request_json(
                client,
                "GET",
                url,
                headers=headers,
                params={"per_page": clean_per_page, "page": clean_page},
            )

        if not isinstance(releases, list) or not releases:
            gh_client.AUDIT.log("github_list_releases", cmd_desc, target=target, status="OK")
            return f"目前 {target} 沒有任何 Releases（第 {clean_page} 頁）。"

        blocks: list[str] = []
        for rel in releases:
            if not isinstance(rel, dict):
                continue
            tag_name = rel.get("tag_name") or "?"
            rel_name = (rel.get("name") or tag_name).strip()
            flags: list[str] = []
            if rel.get("draft") is True:
                flags.append("Draft")
            if rel.get("prerelease") is True:
                flags.append("Pre-release")
            flag_str = f" [{', '.join(flags)}]" if flags else ""
            published_at = rel.get("published_at") or rel.get("created_at") or ""
            header = f"--- {rel_name} ({tag_name}){flag_str} | 時間：{published_at} ---"
            rel_body = (rel.get("body") or "").strip()
            if rel_body:
                clipped_body = security.GITHUB_REDACTION.redact_and_clip_patch(
                    rel_body,
                    _MAX_RELEASE_BODY_CHARS,
                    active_token,
                    "\n... [Release 說明已截斷]",
                )
                blocks.append(f"{header}\n{clipped_body}")
            else:
                blocks.append(header)

        output, was_truncated = security.GITHUB_REDACTION.sanitize_and_truncate(
            "\n\n".join(blocks),
            active_token,
            "請縮小 per_page 或搭配 page 分頁檢視。",
        )
        gh_client.AUDIT.log(
            "github_list_releases",
            cmd_desc,
            target=target,
            status="TRUNCATED" if was_truncated else "OK",
            detail=f"count={len(blocks)}",
        )
        return output
    except Exception as exc:
        return gh_client.handle_tool_error(
            "github_list_releases",
            cmd_desc,
            target,
            exc,
            "無法取得 Releases 列表",
            active_token=active_token,
        )


async def github_list_labels(
    owner: str,
    repo: str,
    per_page: int = 50,
    page: int = 1,
    github_token: str = "",
) -> str:
    """列出指定儲存庫目前可用的標籤（Labels）與說明。"""
    active_token = ""
    target = f"{owner}/{repo}"
    cmd_desc = f"GET labels per_page={per_page} page={page}"
    try:
        clean_owner, clean_repo = security.check_repo_allowed(owner, repo)
        target = f"{clean_owner}/{clean_repo}"
        clean_per_page = validate_positive_int(per_page, "per_page", max_val=100)
        clean_page = validate_positive_int(page, "page")

        headers = gh_client.get_headers(github_token)
        active_token = headers["Authorization"].removeprefix("Bearer ")
        url = helpers.build_repo_url(clean_owner, clean_repo, "/labels")

        async with gh_client.create_http_client() as client:
            labels = await gh_client.request_json(
                client,
                "GET",
                url,
                headers=headers,
                params={"per_page": clean_per_page, "page": clean_page},
            )

        if not isinstance(labels, list) or not labels:
            gh_client.AUDIT.log("github_list_labels", cmd_desc, target=target, status="OK")
            return f"目前 {target} 沒有任何標籤（第 {clean_page} 頁）。"

        lines: list[str] = []
        for lb in labels:
            if not isinstance(lb, dict):
                continue
            name = lb.get("name") or "?"
            color = lb.get("color") or ""
            desc = (lb.get("description") or "").strip()
            color_str = f" (#{color})" if color else ""
            desc_str = f" — {desc}" if desc else ""
            lines.append(f"- {name}{color_str}{desc_str}")

        output, was_truncated = security.GITHUB_REDACTION.sanitize_and_truncate(
            "\n".join(lines),
            active_token,
            "請縮小 per_page 或搭配 page 分頁檢視。",
        )
        gh_client.AUDIT.log(
            "github_list_labels",
            cmd_desc,
            target=target,
            status="TRUNCATED" if was_truncated else "OK",
            detail=f"count={len(lines)}",
        )
        return output
    except Exception as exc:
        return gh_client.handle_tool_error(
            "github_list_labels",
            cmd_desc,
            target,
            exc,
            "無法取得標籤列表",
            active_token=active_token,
        )


def register(mcp: Any) -> None:
    """註冊 GitHub 儲存庫唯讀探索工具至 MCPServer。"""
    for fn in (
        github_get_repo,
        github_list_branches,
        github_list_tags,
        github_list_commits,
        github_get_file_content,
        github_list_releases,
        github_list_labels,
    ):
        mcp.tool()(fn)
