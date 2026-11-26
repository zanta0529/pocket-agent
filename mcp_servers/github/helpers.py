"""GitHub MCP 工具共用輔助函式（抽取跨模組重複邏輯）。"""

from typing import Any
from urllib.parse import quote

from mcp_servers.github import client as gh_client


def extract_login(item: dict[str, Any], field: str = "user") -> str:
    """從 GitHub API 物件萃取使用者帳號，缺失時回傳 'ghost'。"""
    user_obj = item.get(field)
    if isinstance(user_obj, dict) and user_obj.get("login"):
        return str(user_obj["login"])
    return "ghost"


def extract_label_names(item: dict[str, Any]) -> list[str]:
    """從 Issue 或 PR 物件萃取標籤名稱清單。"""
    raw_labels = item.get("labels")
    if not isinstance(raw_labels, list):
        return []
    names: list[str] = []
    for lb in raw_labels:
        if isinstance(lb, dict) and lb.get("name"):
            names.append(str(lb["name"]))
        elif isinstance(lb, str) and lb.strip():
            names.append(lb.strip())
    return names


def extract_user_logins(items: Any) -> list[str]:
    """從 GitHub 使用者物件陣列萃取帳號清單（如 assignees、requested_reviewers）。"""
    if not isinstance(items, list):
        return []
    logins: list[str] = []
    for user in items:
        if isinstance(user, dict) and user.get("login"):
            logins.append(str(user["login"]))
    return logins


def repo_api_path(owner: str, repo: str) -> str:
    """產生百分比編碼後的 owner/repo 路徑片段。"""
    return f"{quote(owner, safe='')}/{quote(repo, safe='')}"


def build_repo_url(owner: str, repo: str, suffix: str = "") -> str:
    """組合指定儲存庫的 GitHub REST API 完整請求網址。"""
    return f"{gh_client.get_api_base()}/repos/{repo_api_path(owner, repo)}{suffix}"


def encode_repo_path(clean_path: str) -> str:
    """將已驗證的儲存庫內部相對路徑逐段百分比編碼。"""
    if not clean_path:
        return ""
    return "/".join(quote(seg, safe="") for seg in clean_path.split("/"))


def clean_string_list(
    values: list[str] | None,
    field_name: str,
    *,
    max_items: int = 50,
) -> list[str]:
    """驗證並清理字串陣列參數，阻斷控制字元與過多項目。"""
    if values is None:
        return []
    if not isinstance(values, list):
        raise ValueError(f"{field_name} 參數必須為字串陣列。")
    cleaned: list[str] = []
    for item in values:
        if not isinstance(item, str) or not item.strip():
            continue
        item_clean = item.strip()
        if any(ch in item_clean for ch in ("\r", "\n", "\x00")):
            raise ValueError(f"{field_name} 包含無效的控制字元。")
        cleaned.append(item_clean)
    if len(cleaned) > max_items:
        raise ValueError(f"{field_name} 項目過多（上限 {max_items} 筆）。")
    return cleaned


def validate_single_line(
    value: str,
    field_name: str,
    *,
    empty_msg: str | None = None,
    invalid_msg: str | None = None,
) -> str:
    """驗證必填單行文字參數（不可為空，且不可含換行或空字元）。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(empty_msg or f"{field_name} 不可為空白。")
    cleaned = value.strip()
    if any(ch in cleaned for ch in ("\r", "\n", "\x00")):
        raise ValueError(invalid_msg or f"{field_name} 包含無效的換行或控制字元。")
    return cleaned
