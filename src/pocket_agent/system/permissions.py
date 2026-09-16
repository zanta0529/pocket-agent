import json
import logging
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


def is_command_allowed(command: str) -> bool:
    """檢查指令是否符合白名單前綴（包含內建與自訂）"""
    cmd_lower = command.strip().lower()

    # 檢查內建
    for prefix in BUILTIN_ALLOWLIST:
        if cmd_lower.startswith(prefix):
            return True

    # 檢查使用者自訂
    user_list = _load_allowlist()
    for prefix in user_list:
        if cmd_lower.startswith(prefix.lower()):
            return True

    return False


def add_command_to_allowlist(command: str) -> str:
    """將指令提取為前綴並加入自訂白名單，回傳被加入的前綴字串"""
    parts = command.strip().split()
    if not parts:
        return ""

    # 擷取前兩個單字作為 prefix (例如 npm run xxx -> npm run)
    if len(parts) >= 2:
        prefix = " ".join(parts[:2])
    else:
        prefix = parts[0]

    current_list = _load_allowlist()
    # 避免重複加入
    if not any(p.lower() == prefix.lower() for p in current_list):
        current_list.append(prefix)
        try:
            ALLOWLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
            # 使用字典格式，方便未來擴充其他設定
            output = {"allowed_prefixes": current_list}
            ALLOWLIST_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            log.warning("儲存 allowlist.json 失敗: %s", e)

    return prefix
