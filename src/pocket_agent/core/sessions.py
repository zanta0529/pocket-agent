"""對話 session 持久化：~/.pocket-agent/sessions/*.json，一個檔案一個對話。"""

import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path

SESSIONS_DIR = Path.home() / ".pocket-agent" / "sessions"


def _get_hmac_key() -> bytes:
    import getpass
    import platform

    salt_file = Path.home() / ".pocket-agent" / "session_salt"
    if salt_file.exists():
        salt = salt_file.read_bytes()
    else:
        salt = secrets.token_bytes(32)
        salt_file.parent.mkdir(parents=True, exist_ok=True)
        salt_file.write_bytes(salt)

    try:
        login = getpass.getuser()
    except Exception:
        login = "unknown_user"
    material = f"{platform.node()}-{login}-pocket-agent-session".encode("utf-8")

    # 使用 PBKDF2 進行 Key Stretching (100,000 次)
    return hashlib.pbkdf2_hmac("sha256", material, salt, 100000)


def _sign_data(data_str: str) -> str:
    return hmac.new(_get_hmac_key(), data_str.encode("utf-8"), hashlib.sha256).hexdigest()


def new_session_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def _path(session_id: str) -> Path:
    return SESSIONS_DIR / f"{session_id}.json"


def make_title(history: list[dict]) -> str:
    """用第一則使用者訊息當標題。"""
    for msg in history:
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            content = next(
                (p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") in ("input_text", "text")),
                "",
            )
        text = str(content).strip().replace("\n", " ")
        if text:
            return text[:40]
    return "（無標題）"


def save_session(session_id: str, history: list[dict], title: str | None = None) -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": session_id,
        "title": title or make_title(history),
        "updated_at": time.time(),
        "history": history,
    }
    payload_str = json.dumps(payload, ensure_ascii=False)
    signature = _sign_data(payload_str)

    data = {"signature": signature, "payload": payload_str}
    _path(session_id).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def load_session(session_id: str) -> dict | None:
    p = _path(session_id)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if "signature" in data and "payload" in data:
            expected_sig = _sign_data(data["payload"])
            if not hmac.compare_digest(expected_sig, data["signature"]):
                import logging

                logging.getLogger("pocket-agent").warning(
                    f"Session {session_id} signature mismatch. Data may be tampered."
                )
                return None
            return json.loads(data["payload"])
        else:
            # Backwards compatibility for old un-signed sessions
            return data
    except Exception:
        return None


def list_sessions() -> list[dict]:
    """回傳 [{id, title, updated_at}]，新的在前。"""
    if not SESSIONS_DIR.exists():
        return []
    metas = []
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            raw_data = json.loads(f.read_text(encoding="utf-8"))
            if "signature" in raw_data and "payload" in raw_data:
                d = json.loads(raw_data["payload"])
            else:
                d = raw_data

            metas.append(
                {
                    "id": d["id"],
                    "title": d.get("title", "（無標題）"),
                    "updated_at": d.get("updated_at", 0),
                }
            )
        except Exception:
            continue
    return sorted(metas, key=lambda m: m["updated_at"], reverse=True)


def delete_session(session_id: str) -> None:
    _path(session_id).unlink(missing_ok=True)
