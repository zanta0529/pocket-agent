import json
import logging
import re
import shlex
import sys
from dataclasses import dataclass
from enum import StrEnum
from typing import List

from pocket_agent.system.paths import user_dir

log = logging.getLogger("pocket-agent")

ALLOWLIST_PATH = user_dir() / "allowlist.json"


class CommandVerdict(StrEnum):
    """指令安全分類結果。"""

    BLOCKED = "blocked"
    CAUTION = "caution"
    ALLOW = "allow"
    ASK = "ask"


@dataclass(frozen=True)
class CommandSafetyResult:
    """指令安全分類結果與原因說明。"""

    verdict: CommandVerdict
    reason: str = ""
    reason_key: str = ""


_HARD_BLOCK_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\brm\s+(?:-{1,2}[a-z-]+\s+)*-[a-z]*r[a-z]*[fFv]*\s+(?:/[a-z0-9_]*|~|\$HOME|\*|\s*$)",
            re.IGNORECASE,
        ),
        "recursive delete of root or home directory",
    ),
    (
        re.compile(r"\brm\b[^\n|;&]*--no-preserve-root\b", re.IGNORECASE),
        "rm --no-preserve-root",
    ),
    (
        re.compile(r"\bdel\s+(?:/[sqf]\s*)+\s*(?:[a-z]:\\|\*)", re.IGNORECASE),
        "recursive/quiet delete on drive root or wildcard",
    ),
    (
        re.compile(r"\b(?:rd|rmdir)\s+(?:/[sq]\s*)+\s*[a-z]:\\?(?:\s|$)", re.IGNORECASE),
        "recursive directory removal on drive root",
    ),
    (
        re.compile(
            r"\bRemove-Item\b[^\n]*(?:-Recurse|-r\b)[^\n]*(?:-Force|-fo\b)[^\n]*(?:[a-z]:\\|/|~|\$HOME)(?:\s|$|\*)",
            re.IGNORECASE,
        ),
        "PowerShell recursive force removal on root or home",
    ),
    (
        re.compile(r"\bformat\s+[a-z]:", re.IGNORECASE),
        "disk format command",
    ),
    (
        re.compile(r"\bmkfs(?:\.[a-z0-9]+)?\b", re.IGNORECASE),
        "filesystem format (mkfs)",
    ),
    (
        re.compile(r"\bdd\b[^\n]*\bof=\s*/dev/(?:sd|nvme|hd|disk|vd)[a-z0-9]*", re.IGNORECASE),
        "dd write to raw block device",
    ),
    (
        re.compile(r">\s*/dev/(?:sd|nvme|hd|disk|vd)[a-z0-9]*", re.IGNORECASE),
        "redirect output onto raw block device",
    ),
    (
        re.compile(r"\bshred\s+[^\n]*-[a-z]*[uzn][a-z]*\s+(?:/|[a-z]:\\|\*)", re.IGNORECASE),
        "shred on root or drive",
    ),
    (
        re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),
        "fork bomb",
    ),
    (
        re.compile(
            r"\b(?:curl|wget)\b[^\n]*\|\s*(?:sudo\s+)?(?:sh|bash|zsh|python3?|perl)\b",
            re.IGNORECASE,
        ),
        "pipe remote script directly into shell",
    ),
)

_CAUTION_GIT_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(r"\bgit\s+push\b[^\n|;&]*(?:\s--force\b|\s-f\b|\s--force-with-lease\b)", re.IGNORECASE),
        "git force-push may overwrite remote history",
        "confirmation.git_caution_force_push",
    ),
    (
        re.compile(r"\bgit\s+reset\s+--hard\b", re.IGNORECASE),
        "git reset --hard discards uncommitted local changes",
        "confirmation.git_caution_reset_hard",
    ),
    (
        re.compile(r"\bgit\s+clean\b[^\n|;&]*\s-[a-z]*f", re.IGNORECASE),
        "git clean -f permanently deletes untracked files",
        "confirmation.git_caution_clean_f",
    ),
    (
        re.compile(r"\bgit\s+branch\b[^\n|;&]*\s-D\b"),
        "git branch -D force-deletes a branch regardless of merge status",
        "confirmation.git_caution_branch_delete",
    ),
    (
        re.compile(r"\bgit\s+checkout\s+--\s", re.IGNORECASE),
        "git checkout -- discards working-tree modifications",
        "confirmation.git_caution_checkout_discard",
    ),
)


def _normalize_floor_variants(command: str) -> tuple[str, str]:
    """回傳 (空白正規化字串, 去引號並空白正規化字串)，防止引號拆字繞過底線檢查。"""
    collapsed = " ".join(command.split())
    quote_stripped = " ".join(command.replace('"', "").replace("'", "").split())
    return collapsed, quote_stripped


def _match_hard_block(command: str) -> str | None:
    for variant in _normalize_floor_variants(command):
        for pattern, reason in _HARD_BLOCK_PATTERNS:
            if pattern.search(variant):
                return reason
    return None


def _match_caution_git(command: str) -> tuple[str, str] | None:
    for variant in _normalize_floor_variants(command):
        for pattern, reason, reason_key in _CAUTION_GIT_PATTERNS:
            if pattern.search(variant):
                return reason, reason_key
    return None


# 內建唯讀白名單 (基於前綴比對，請使用小寫)
BUILTIN_ALLOWLIST = [
    "git rev-parse",
    "git status",
    "git diff",
    "git log",
    "git show",
]


def _load_allowlist() -> List[str]:
    """讀取使用者的 allowlist.json"""
    if not ALLOWLIST_PATH.exists():
        return []
    try:
        data = json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("allowed_prefixes"), list):
            return data["allowed_prefixes"]
        elif isinstance(data, list):
            return data
    except Exception as e:
        log.warning("讀取 allowlist.json 失敗: %s", e)
    return []


def _split_command(command: str) -> List[str]:
    """分詞指令。Windows 使用非 POSIX 模式，避免反斜線（路徑分隔）被當成跳脫字元吃掉。"""
    posix = sys.platform != "win32"
    parts = shlex.split(command, posix=posix)
    if not posix:
        # 非 POSIX 模式會保留引號，這裡移除成對的外層引號以便比對
        parts = [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in ("'", '"') else p for p in parts]
    return parts


def _normalize_command(command: str) -> str:
    """取得自訂白名單之嚴格原始命令鍵（保留引號、大小寫與參數邊界；語法異常時拒絕）。"""
    raw = command.strip()
    if not raw:
        return ""
    try:
        parts = _split_command(raw)
    except Exception:
        return ""
    if not parts:
        return ""
    in_quote: str | None = None
    for ch in raw:
        if in_quote is None:
            if ch in ('"', "'"):
                in_quote = ch
        elif ch == in_quote:
            in_quote = None
    if in_quote is not None:
        return ""
    return raw


# 這些旗標會寫出檔案、讀取任意檔案，或啟動外部程式（pager、外部 diff、textconv），不符合「唯讀」
_FORBIDDEN_GIT_FLAGS = ("--no-index", "--ext-diff", "--textconv", "--open-files-in-pager")


def _is_builtin_allowed(command: str) -> bool:
    try:
        parts = _split_command(command)
    except Exception:
        return False
    if not parts:
        return False

    cmd_lower = parts[0].lower()

    if cmd_lower == "git":
        if len(parts) < 2:
            return True
        subcmd = parts[1].lower()
        if subcmd in ("rev-parse", "status", "diff", "log", "show"):
            for p in parts[2:]:
                # 精確比對輸出旗標；-O<orderfile> 與 -O 亦拒絕
                if p == "-o" or p.startswith("--output") or p.startswith("-O") or ">" in p:
                    return False
                if p.startswith(_FORBIDDEN_GIT_FLAGS):
                    return False
            return True

    return False


def classify_command_safety(command: str) -> CommandSafetyResult:
    """依序執行硬性底線、高風險 Git 警示、白名單與一般確認分類。"""
    block_reason = _match_hard_block(command)
    if block_reason:
        return CommandSafetyResult(CommandVerdict.BLOCKED, block_reason)

    caution_match = _match_caution_git(command)
    if caution_match:
        reason, reason_key = caution_match
        return CommandSafetyResult(CommandVerdict.CAUTION, reason, reason_key)

    if is_command_allowed(command):
        return CommandSafetyResult(CommandVerdict.ALLOW)

    return CommandSafetyResult(CommandVerdict.ASK)


def is_command_allowed(command: str) -> bool:
    """檢查指令是否符合白名單（包含嚴格內建與自訂完整比對）"""
    if _match_hard_block(command) or _match_caution_git(command):
        return False

    dangerous_chars = ["&", "|", ">", "<", ";", "$", "`", "\n", "\r", "%", "^"]
    if any(char in command for char in dangerous_chars):
        return False

    if _is_builtin_allowed(command):
        return True

    # 檢查使用者自訂
    user_list = _load_allowlist()

    cmd_normalized = _normalize_command(command)
    if not cmd_normalized:
        return False

    for allowed_cmd in user_list:
        # 嚴格原始命令比對（保留大小寫、引號與內部空白邊界）
        allowed_norm = _normalize_command(str(allowed_cmd))
        if allowed_norm and cmd_normalized == allowed_norm:
            return True

    return False


def add_command_to_allowlist(command: str) -> str:
    """將指令加入自訂白名單（完整指令比對，非前綴），回傳被加入的字串"""
    prefix_normalized = _normalize_command(command)

    if not prefix_normalized:
        return ""

    prefix = command.strip()

    current_list = _load_allowlist()
    # 避免重複加入

    def normalize(cmd):
        return _normalize_command(str(cmd))

    if not any(normalize(p) == prefix_normalized for p in current_list):
        current_list.append(prefix)
        try:
            ALLOWLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
            output = {"allowed_prefixes": current_list}
            ALLOWLIST_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            log.warning("儲存 allowlist.json 失敗: %s", e)

    return prefix
