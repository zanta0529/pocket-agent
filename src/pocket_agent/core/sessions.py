"""對話 session 持久化：~/.pocket-agent/sessions/*.json，一個檔案一個對話。"""

import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path

SESSIONS_DIR = Path.home() / ".pocket-agent" / "sessions"
PBKDF2_ITERATIONS = 600_000
LEGACY_PBKDF2_ITERATIONS = 100_000

_CACHED_HMAC_KEY: bytes | None = None
_CACHED_LEGACY_HMAC_KEY: bytes | None = None


def _get_hmac_key(iterations: int = PBKDF2_ITERATIONS) -> bytes:
    global _CACHED_HMAC_KEY, _CACHED_LEGACY_HMAC_KEY
    if iterations == PBKDF2_ITERATIONS and _CACHED_HMAC_KEY is not None:
        return _CACHED_HMAC_KEY
    if iterations == LEGACY_PBKDF2_ITERATIONS and _CACHED_LEGACY_HMAC_KEY is not None:
        return _CACHED_LEGACY_HMAC_KEY

    import getpass
    import platform

    salt_file = Path.home() / ".pocket-agent" / "session_salt"
    salt = None
    if salt_file.exists():
        try:
            salt = salt_file.read_bytes()
        except Exception:
            salt = None

    if not salt:
        salt_file.parent.mkdir(parents=True, exist_ok=True)
        new_salt = secrets.token_bytes(32)
        try:
            with salt_file.open("xb") as f:
                f.write(new_salt)
            salt = new_salt
        except FileExistsError:
            # 另一個行程搶先建立，讀取已初始化的 salt
            for _ in range(10):
                try:
                    salt = salt_file.read_bytes()
                    if len(salt) == 32:
                        break
                except Exception:
                    pass
                time.sleep(0.01)
            if not salt:
                salt = salt_file.read_bytes()
        except Exception:
            if salt_file.exists():
                salt = salt_file.read_bytes()
            else:
                salt_file.write_bytes(new_salt)
                salt = new_salt

    try:
        login = getpass.getuser()
    except Exception:
        login = "unknown_user"
    material = f"{platform.node()}-{login}-pocket-agent-session".encode("utf-8")

    # 使用 PBKDF2 進行 Key Stretching (預設 600,000 次)
    key = hashlib.pbkdf2_hmac("sha256", material, salt, iterations)
    if iterations == PBKDF2_ITERATIONS:
        _CACHED_HMAC_KEY = key
    elif iterations == LEGACY_PBKDF2_ITERATIONS:
        _CACHED_LEGACY_HMAC_KEY = key
    return key


def _sign_data(data_str: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    return hmac.new(_get_hmac_key(iterations), data_str.encode("utf-8"), hashlib.sha256).hexdigest()


def new_session_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def _path(session_id: str) -> Path:
    import re

    if not re.match(r"^[a-zA-Z0-9\-_]+$", session_id):
        raise ValueError(f"無效的 Session ID 格式: {session_id}")
    path = (SESSIONS_DIR / f"{session_id}.json").resolve()
    try:
        path.relative_to(SESSIONS_DIR.resolve())
    except ValueError:
        raise ValueError(f"Session ID 超出安全路徑範圍: {session_id}")
    return path


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


def save_session(
    session_id: str,
    history: list[dict],
    title: str | None = None,
    is_sensitive: bool = False,
) -> None:
    if is_sensitive:
        return
    for msg in history:
        if isinstance(msg, dict) and msg.get("is_sensitive"):
            return
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    resolved_title = title or make_title(history)
    updated_at = time.time()
    payload = {
        "id": session_id,
        "title": resolved_title,
        "updated_at": updated_at,
        "history": history,
    }
    payload_str = json.dumps(payload, ensure_ascii=False)
    signature = _sign_data(payload_str)

    data = {
        "signature": signature,
        "meta": {"id": session_id, "title": resolved_title, "updated_at": updated_at},
        "payload": payload_str,
    }

    final_path = _path(session_id)
    tmp_path = final_path.with_suffix(".tmp")

    try:
        tmp_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(final_path)
    except Exception as e:
        import logging

        logging.getLogger("pocket-agent").error(f"Failed to atomic save session {session_id}: {e}")
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def load_session(session_id: str) -> dict | None:
    p = _path(session_id)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if "signature" in data and "payload" in data:
            expected_sig = _sign_data(data["payload"])
            is_valid = hmac.compare_digest(expected_sig, data["signature"])
            if not is_valid:
                # 嘗試以舊版迭代次數驗證（向下相容舊 Session）
                legacy_sig = _sign_data(data["payload"], iterations=LEGACY_PBKDF2_ITERATIONS)
                is_valid = hmac.compare_digest(legacy_sig, data["signature"])

            if not is_valid:
                import logging

                logging.getLogger("pocket-agent").warning(
                    f"Session {session_id} signature mismatch. Data may be tampered."
                )
                return None
            return json.loads(data["payload"])
        else:
            import logging

            logging.getLogger("pocket-agent").warning(
                f"Session {session_id} is missing signature and is rejected for security."
            )
            return None
    except Exception:
        return None


def list_sessions() -> list[dict]:
    """回傳 [{id, title, updated_at}]，新的在前，並驗證簽章完整性。"""
    if not SESSIONS_DIR.exists():
        return []
    metas = []
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            raw_data = json.loads(f.read_text(encoding="utf-8"))
            if "signature" not in raw_data or "payload" not in raw_data:
                continue
            expected_sig = _sign_data(raw_data["payload"])
            is_valid = hmac.compare_digest(expected_sig, raw_data["signature"])
            if not is_valid:
                legacy_sig = _sign_data(raw_data["payload"], iterations=LEGACY_PBKDF2_ITERATIONS)
                is_valid = hmac.compare_digest(legacy_sig, raw_data["signature"])
            if not is_valid:
                continue

            meta = raw_data.get("meta")
            if isinstance(meta, dict) and "id" in meta:
                metas.append(
                    {
                        "id": meta["id"],
                        "title": meta.get("title", "（無標題）"),
                        "updated_at": meta.get("updated_at", 0),
                    }
                )
            else:
                d = json.loads(raw_data["payload"])
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


def rename_session(session_id: str, new_title: str) -> bool:
    """重新命名指定的 session 標題。若該 session 檔案存在則更新並重新簽章回寫。"""
    clean_title = new_title.strip().replace("\n", " ")[:60]
    if not clean_title:
        raise ValueError("標題不得為空。")
    data = load_session(session_id)
    if data is None:
        return False
    save_session(session_id, data.get("history", []), title=clean_title)
    return True
