"""對話 session 持久化：~/.pocket-agent/sessions/*.json，一個檔案一個對話。"""

import json
import secrets
import time
from pathlib import Path

SESSIONS_DIR = Path.home() / ".pocket-agent" / "sessions"


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
    data = {
        "id": session_id,
        "title": title or make_title(history),
        "updated_at": time.time(),
        "history": history,
    }
    _path(session_id).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def load_session(session_id: str) -> dict | None:
    p = _path(session_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def list_sessions() -> list[dict]:
    """回傳 [{id, title, updated_at}]，新的在前。"""
    if not SESSIONS_DIR.exists():
        return []
    metas = []
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
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
