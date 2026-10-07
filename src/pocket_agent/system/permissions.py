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
    "ls ",
    "dir ",
    "cat ",
    "type ",
    "pwd",
    "echo ",
    "grep ",
    "find ",
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

    if cmd_lower in ("ls", "dir", "pwd", "echo"):
        return True

    if cmd_lower in ("cat", "type", "grep", "find"):
        # block writing flags if any
        for p in parts:
            if p.startswith("--out") or p.startswith("-o") or ">" in p:
                return False
        return True

    if cmd_lower == "git":
        if len(parts) < 2:
            return True
        subcmd = parts[1].lower()
        if subcmd in ("rev-parse", "status", "diff", "log", "show"):
            # Check for output flags in diff/log
            for p in parts:
                if p.startswith("--output") or p.startswith("-o"):
                    return False
            return True

    return False


def is_command_allowed(command: str) -> bool:
    """檢查指令是否符合白名單（包含嚴格內建與自訂完整比對）"""
    dangerous_chars = ["&", "|", ">", "<", ";", "$", "`"]
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

    cmd_lower = " ".join(parts).lower()

    for allowed_cmd in user_list:
        allowed_lower = allowed_cmd.lower()
        # Allow exact match or if it is a prefix of words (e.g. "npm run" matches "npm run dev")
        if cmd_lower == allowed_lower or cmd_lower.startswith(allowed_lower + " "):
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

    # 如果是常見的 sub-command，取前兩個，否則取第一個
    if parts[0].lower() in ("npm", "yarn", "pnpm", "pip", "docker", "docker-compose", "git", "python", "go", "cargo"):
        if len(parts) >= 2 and not parts[1].startswith("-"):
            prefix = f"{parts[0]} {parts[1]}"
        else:
            prefix = parts[0]
    else:
        prefix = parts[0]

    current_list = _load_allowlist()
    # 避免重複加入
    if not any(p.lower() == prefix.lower() for p in current_list):
        current_list.append(prefix)
        try:
            ALLOWLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
            output = {"allowed_prefixes": current_list}
            ALLOWLIST_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            log.warning("儲存 allowlist.json 失敗: %s", e)

    return prefix
