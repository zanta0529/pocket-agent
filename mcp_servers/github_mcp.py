import os

import httpx
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("github")


def get_headers():
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise ValueError("缺少 GITHUB_TOKEN 環境變數。請在 mcp_config.yaml 中使用 inject 機制注入。")
    return {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


@mcp.tool()
async def github_list_prs(owner: str, repo: str, state: str = "open") -> str:
    """列出指定儲存庫的 Pull Requests"""
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/pulls", headers=get_headers(), params={"state": state}
            )
            resp.raise_for_status()
            prs = resp.json()
            if not prs:
                return f"目前沒有狀態為 {state} 的 Pull Requests。"
            return "\n".join(f"#{pr['number']} {pr['title']} ({pr['user']['login']})" for pr in prs)
        except Exception as e:
            return f"[ERROR] 無法取得 PR 列表: {e}"


@mcp.tool()
async def github_read_issue(owner: str, repo: str, issue_number: int) -> str:
    """讀取指定的 Issue 或 Pull Request 內容"""
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}", headers=get_headers()
            )
            resp.raise_for_status()
            data = resp.json()
            return f"標題：{data.get('title', '')}\n狀態：{data.get('state', '')}\n\n{data.get('body', '')}"
        except Exception as e:
            return f"[ERROR] 無法取得 Issue 內容: {e}"


@mcp.tool()
async def github_create_comment(owner: str, repo: str, issue_number: int, body: str) -> str:
    """在指定的 Issue 或 Pull Request 新增留言"""
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/comments",
                headers=get_headers(),
                json={"body": body},
            )
            resp.raise_for_status()
            return f"成功在 #{issue_number} 新增留言。"
        except Exception as e:
            return f"[ERROR] 無法新增留言: {e}"


if __name__ == "__main__":
    mcp.run()
