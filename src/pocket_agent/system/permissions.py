import json
import logging
import shlex
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


def _is_builtin_allowed(command: str) -> bool:
    import shlex

    try:
        parts = shlex.split(command)
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
            # Check for output flags in diff/log
            for p in parts:
                if p.startswith("--output") or p.startswith("-o") or ">" in p:
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

    try:
        parts = shlex.split(command)
    except Exception:
        parts = command.strip().split()

    if not parts:
        return False

    cmd_normalized = " ".join(parts)

    for allowed_cmd in user_list:
        try:
            allowed_parts = shlex.split(allowed_cmd)
        except Exception:
            allowed_parts = allowed_cmd.strip().split()

        # Allow exact match preserving case
        if cmd_normalized == " ".join(allowed_parts):
            return True

    return False


def add_command_to_allowlist(command: str) -> str:
    """將指令加入自訂白名單，回傳被加入的字串"""
    import shlex

    try:
        parts = shlex.split(command)
    except Exception:
        parts = command.strip().split()

    if not parts:
        return ""

    prefix = command.strip()

    current_list = _load_allowlist()
    # 避免重複加入
    prefix_normalized = " ".join(parts)

    def normalize(cmd):
        try:
            p = shlex.split(cmd)
        except Exception:
            p = cmd.strip().split()
        return " ".join(p)

    if not any(normalize(p) == prefix_normalized for p in current_list):
        current_list.append(prefix)
        try:
            ALLOWLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
            output = {"allowed_prefixes": current_list}
            ALLOWLIST_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            log.warning("儲存 allowlist.json 失敗: %s", e)

    return prefix
