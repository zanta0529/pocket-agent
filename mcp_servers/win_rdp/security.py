"""Windows RDP MCP 安全驗證與輸出遮罩策略（Strategy Pattern）。"""

import ntpath
import re

from mcp_servers._shared import CONSTANTS, DEFAULT_REDACTION_POLICY

MAX_OUTPUT_CHARS = CONSTANTS.max_output_chars

_HOST_RE = re.compile(r"^[a-zA-Z0-9_.\-]+$")
_USER_RE = re.compile(r"^[a-zA-Z0-9_.@\\\-]+$")
_SERVICE_RE = re.compile(r"^[a-zA-Z0-9_.\-]+$")

VALID_EVENT_LOGS = {
    "system": "System",
    "application": "Application",
    "security": "Security",
    "setup": "Setup",
}

VALID_EVENT_LEVELS = {
    "critical": "1",
    "error": "2",
    "warning": "3",
    "info": "4",
    "information": "4",
    "1": "1",
    "2": "2",
    "3": "3",
    "4": "4",
}

_ALLOWED_WIN_CONFIG_PREFIXES = (
    "c:\\windows\\system32\\drivers\\etc\\",
    "c:\\windows\\win.ini",
    "c:\\windows\\system.ini",
    "c:\\inetpub\\wwwroot\\",
    "c:\\programdata\\",
)

_DENIED_WIN_PATH_KEYWORDS = (
    "sam",
    "security",
    "system32\\config",
    "ntds.dit",
    "private",
    "secret",
    "key",
    "token",
    "password",
    "passwd",
    ".ssh",
    ".env",
    ".pfx",
    ".p12",
    ".pem",
    "id_rsa",
    "id_ed25519",
    "unattend.xml",
    "web.config",
)

BLOCKED_HOTKEYS = frozenset(
    {
        "ctrl+alt+del",
        "ctrl+alt+delete",
        "alt+tab",
        "win+l",
    }
)


def validate_host(value: str) -> str:
    clean = value.strip()
    if not clean or clean.startswith("-") or not _HOST_RE.match(clean):
        raise ValueError(f"非法的 WIN_HOST 參數格式：{value!r}（僅允許英數字、點、底線與連字號）")
    return clean


def validate_user(value: str) -> str:
    clean = value.strip()
    if not clean or clean.startswith("-") or not _USER_RE.match(clean):
        raise ValueError(f"非法的 WIN_RDP_USER 參數格式：{value!r}（僅允許英數字、點、底線、連字號、@ 與 \\）")
    return clean


def validate_service_name(value: str) -> str:
    clean = value.strip()
    if not clean or clean.startswith("-") or not _SERVICE_RE.match(clean):
        raise ValueError(f"非法的 service_name 參數格式：{value!r}（僅允許英數字、點、底線與連字號）")
    return clean


def validate_mouse_bounds(x: int, y: int, width: int, height: int) -> None:
    if not (0 <= x < width and 0 <= y < height):
        raise ValueError(
            f"座標 ({x}, {y}) 超出遠端桌面視窗客戶區範圍 (0..{width - 1}, 0..{height - 1})，拒絕點擊視窗外區域。"
        )


def validate_win_config_path(file_path: str) -> str:
    raw = file_path.strip().replace("/", "\\")
    if not re.match(r"^[a-zA-Z]:\\", raw):
        raise ValueError("Windows 設定檔路徑必須為完整磁碟機絕對路徑（例如 C:\\Windows\\win.ini）。")
    if ".." in raw.split("\\"):
        raise ValueError("拒絕存取：路徑不得包含 '..' 跳躍符號。")

    forbidden_chars = (
        "'",
        '"',
        "`",
        ";",
        "$",
        "&",
        "|",
        "\r",
        "\n",
        "%",
        "^",
        "<",
        ">",
        "{",
        "}",
        "\u2018",
        "\u2019",
        "\u201a",
        "\u201b",
        "\u201c",
        "\u201d",
        "\u201e",
        "\u201f",
        "\u00ab",
        "\u00bb",
    )
    if any(ch in raw for ch in forbidden_chars):
        raise ValueError("拒絕存取：路徑包含非法的 Shell、引號或命令注入特殊字元。")

    norm = ntpath.normpath(raw)
    lower_path = norm.lower()
    is_allowed = any(
        lower_path.startswith(prefix) or lower_path == prefix.rstrip("\\") for prefix in _ALLOWED_WIN_CONFIG_PREFIXES
    )
    if not is_allowed:
        allowed_str = "、".join(_ALLOWED_WIN_CONFIG_PREFIXES)
        raise ValueError(f"拒絕存取：{norm} 不在允許的唯讀探查目錄白名單內（{allowed_str}）。")

    for kw in _DENIED_WIN_PATH_KEYWORDS:
        if kw in lower_path:
            raise ValueError(f"拒絕存取：路徑包含敏感保護關鍵字 {kw!r}，禁止讀取。")
    return norm


def redact_and_truncate(text: str) -> str:
    if not text:
        return "(沒有輸出)"
    redacted = DEFAULT_REDACTION_POLICY.redact(text)
    total_len = len(redacted)
    if total_len > MAX_OUTPUT_CHARS:
        return (
            redacted[:MAX_OUTPUT_CHARS]
            + f"\n\n... (內容過長，已達單次輸出上限 {MAX_OUTPUT_CHARS} 字元，原始輸出共 {total_len:,} 字元)"
        )
    return redacted
