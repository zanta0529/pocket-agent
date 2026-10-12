import json
import logging
import shlex
import sys
from typing import List

from pocket_agent.system.paths import user_dir

log = logging.getLogger("pocket-agent")

ALLOWLIST_PATH = user_dir() / "allowlist.json"

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
    """分詞後以單一空白重組，分詞失敗時退回簡單空白切分。"""
    try:
        parts = _split_command(command)
    except Exception:
        parts = command.strip().split()
    return " ".join(parts)


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


def is_command_allowed(command: str) -> bool:
    """檢查指令是否符合白名單（包含嚴格內建與自訂完整比對）"""
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
        # Allow exact match preserving case
        if cmd_normalized == _normalize_command(str(allowed_cmd)):
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
