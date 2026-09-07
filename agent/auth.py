"""OAuth 2.0 PKCE — 支援 OpenAI (ChatGPT Plus/Pro) 登入。

技術細節參考自 openclaw/openclaw 的 openai-codex provider 實作。
"""

import base64
import hashlib
import json
import logging
import os
import secrets
import sys
import threading
import time
import webbrowser
from base64 import urlsafe_b64encode
from pathlib import Path
from urllib.parse import urlencode

import certifi
import httpx
import keyring

# ── SSL：優先用 OS 系統憑證庫（支援公司 proxy CA）──
try:
    import truststore

    truststore.inject_into_ssl()
    _SSL_VERIFY = True  # truststore 已接管，用預設即可
except ImportError:
    _SSL_VERIFY = certifi.where()  # fallback：Mozilla CA bundle
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

# ── OAuth 設定 ────────────────────────────────────
DEFAULT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

OPENAI_AUTH_URL = "https://auth.openai.com/oauth/authorize"
OPENAI_TOKEN_URL = "https://auth.openai.com/oauth/token"
REDIRECT_URI = "http://localhost:1455/auth/callback"
OPENAI_SCOPE = "openid profile email offline_access"

CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"

ORIGINATOR = os.getenv("OPENAI_ORIGINATOR", "pocket-agent")
ADD_ORGANIZATIONS = os.getenv("OPENAI_ADD_ORGANIZATIONS", "1").strip().lower() not in ("0", "false", "no", "off")
USE_SIMPLIFIED_FLOW = os.getenv("OPENAI_CODEX_SIMPLIFIED_FLOW", "1").strip().lower() not in ("0", "false", "no", "off")

log = logging.getLogger("pocket-agent")

KEYCHAIN_SERVICE = "pocket-agent"
CONFIG_PATH = Path.home() / ".pocket-agent" / "config.json"
TOKEN_FILE = Path.home() / ".pocket-agent" / "token.json"


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _decode_jwt_payload(token: str) -> dict:
    try:
        part = token.split(".")[1]
        part += "=" * (4 - len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


def _extract_account_id(access_token: str) -> str | None:
    payload = _decode_jwt_payload(access_token)
    auth = payload.get("https://api.openai.com/auth", {})
    return auth.get("chatgpt_account_id")


def _save_token(provider: str, token: dict) -> None:
    if "expires_in" in token and "expires_at" not in token:
        token["expires_at"] = time.time() + token["expires_in"]
    data = json.dumps(token)
    key = f"{provider}-token"
    try:
        keyring.set_password(KEYCHAIN_SERVICE, key, data)
    except Exception:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        tokens = {}
        if TOKEN_FILE.exists():
            try:
                tokens = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        tokens[key] = token
        TOKEN_FILE.write_text(json.dumps(tokens), encoding="utf-8")


def _load_token(provider: str) -> dict | None:
    key = f"{provider}-token"
    try:
        raw = keyring.get_password(KEYCHAIN_SERVICE, key)
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    if TOKEN_FILE.exists():
        try:
            tokens = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
            return tokens.get(key)
        except Exception:
            pass
    return None


def _is_expired(token: dict) -> bool:
    return token.get("expires_at", 0) < time.time() + 60


def _try_refresh(provider: str, token: dict, client_id: str, client_secret: str = "") -> dict | None:
    refresh = token.get("refresh_token")
    if not refresh:
        return None
    token_url = OPENAI_TOKEN_URL
    data = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh,
    }
    if client_secret:
        data["client_secret"] = client_secret

    try:
        resp = httpx.post(token_url, data=data, timeout=10, verify=_SSL_VERIFY)
        if resp.status_code == 200:
            new_token = {**token, **resp.json()}
            new_token.pop("expires_at", None)
            _save_token(provider, new_token)
            return new_token
    except Exception:
        pass
    return None


def _browser_oauth(provider: str, client_id: str, client_secret: str = "", on_auth_url=None, cancel_event=None) -> dict:
    verifier, challenge = _pkce_pair()
    expected_state = secrets.token_urlsafe(16)

    auth_url_base = OPENAI_AUTH_URL
    token_url = OPENAI_TOKEN_URL
    scope = OPENAI_SCOPE
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": scope,
        "state": expected_state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "originator": ORIGINATOR,
    }
    if ADD_ORGANIZATIONS:
        params["id_token_add_organizations"] = "true"
    if USE_SIMPLIFIED_FLOW:
        params["codex_cli_simplified_flow"] = "true"

    auth_url = auth_url_base + "?" + urlencode(params)
    bucket: dict = {}
    app = FastAPI()

    def _page(color: str, message: str) -> HTMLResponse:
        return HTMLResponse(
            f"<h1 style='font-family:sans-serif;text-align:center;padding-top:20vh;color:{color}'>{message}</h1>"
        )

    @app.get("/auth/callback")
    async def callback(code: str = "", state: str = "", error: str = "", error_description: str = ""):
        if error:
            log.error("OAuth 被拒絕：%s %s", error, error_description)
            bucket["error"] = error_description or error
            return _page("#c0392b", f"✗ 授權未完成：{error}")

        if state != expected_state:
            return _page("#c0392b", "✗ 驗證碼不符。<br>可能是舊的分頁，請關閉後重試。")

        if not code:
            return _page("#c0392b", "✗ 沒有收到授權碼。")

        bucket["code"] = code
        return _page("#10a37f", "✓ 認證成功！請關閉此視窗回到 Pocket Agent。")

    server = uvicorn.Server(uvicorn.Config(app, host="localhost", port=1455, log_level="error", log_config=None))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    for _ in range(30):
        if server.started:
            break
        if not thread.is_alive():
            raise RuntimeError("無法在 port 1455 啟動登入用的本機服務。請確認 port 未被占用。")
        time.sleep(0.1)
    else:
        raise RuntimeError("登入服務啟動逾時。")

    try:
        opened = webbrowser.open(auth_url)
    except Exception:
        opened = False

    if on_auth_url is not None:
        try:
            on_auth_url(auth_url, opened)
        except Exception:
            pass

    if opened:
        print("\n[Auth] 瀏覽器已開啟，請登入並授權…")
    else:
        print("\n[Auth] 無法自動開啟瀏覽器，請手動複製以下網址：")
    print(f"\n  {auth_url}\n")
    print("[Auth] 授權完成後會自動繼續，請勿關閉此視窗。")

    for i in range(300):
        if "code" in bucket:
            server.should_exit = True
            break
        if "error" in bucket:
            server.should_exit = True
            raise RuntimeError(f"授權未完成：{bucket['error']}")
        if cancel_event is not None and cancel_event.is_set():
            server.should_exit = True
            raise RuntimeError("登入已取消。")
        if i == 15 and not opened and sys.stdin is not None:
            redirect_url = input("\n[Auth] 登入後請貼上瀏覽器跳轉的完整 redirect URL：\n> ").strip()
            from urllib.parse import parse_qs, urlparse

            parsed = parse_qs(urlparse(redirect_url).query)
            if "code" in parsed:
                bucket["code"] = parsed["code"][0]
                server.should_exit = True
                break
        time.sleep(1)
    else:
        server.should_exit = True
        raise TimeoutError("OAuth 認證逾時（5 分鐘）")

    token_data = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": bucket["code"],
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
    }
    if client_secret:
        token_data["client_secret"] = client_secret

    resp = httpx.post(token_url, data=token_data, timeout=10, verify=_SSL_VERIFY)
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        error_details = e.response.text
        raise RuntimeError(f"Token 交換失敗 (HTTP {e.response.status_code}): {error_details}")
    return resp.json()


def get_access_token(provider: str = "openai", on_auth_url=None, cancel_event=None) -> str:
    client_id = os.getenv("OPENAI_CLIENT_ID") or DEFAULT_CLIENT_ID
    client_secret = ""

    token = _load_token(provider)

    if token and not _is_expired(token):
        return token["access_token"]

    if token:
        refreshed = _try_refresh(provider, token, client_id, client_secret)
        if refreshed:
            return refreshed["access_token"]

    token = _browser_oauth(provider, client_id, client_secret, on_auth_url, cancel_event)
    _save_token(provider, token)
    print(f"[Auth] ✓ {provider.upper()} 登入成功！\n")
    return token["access_token"]


def get_openai_client(provider: str = None, model: str = None):
    """建立 OpenAI 相容的 client，根據 provider 切換 Base URL 與 Auth。"""
    from openai import AsyncOpenAI

    if provider is None:
        cfg = load_config()
        provider = cfg.get("provider", "openai")

    if provider == "ollama":
        base_url = "http://localhost:11434/v1"
        api_key = "ollama"
        extra_headers = {}
    else:
        # 預設 OpenAI (Codex 後端)
        base_url = CODEX_BASE_URL
        api_key = get_access_token(provider="openai")
        account_id = _extract_account_id(api_key)
        extra_headers = {
            "originator": "pocket-agent",
            "OpenAI-Beta": "responses=experimental",
        }
        if account_id:
            extra_headers["chatgpt-account-id"] = account_id

    return AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        default_headers=extra_headers,
    )


def fetch_available_models(provider: str = "openai") -> list[str]:
    """取得可用模型列表。"""
    if provider == "ollama":
        try:
            resp = httpx.get("http://localhost:11434/api/tags", timeout=5)
            if resp.status_code == 200:
                return [m["name"] for m in resp.json().get("models", [])]
        except Exception:
            return []
        return []

    # OpenAI 原有邏輯
    token = _load_token("openai")
    if token and _is_expired(token):
        client_id = os.getenv("OPENAI_CLIENT_ID") or DEFAULT_CLIENT_ID
        token = _try_refresh("openai", token, client_id)
    if not token:
        return []

    access_token = token["access_token"]
    headers = {
        "Authorization": f"Bearer {access_token}",
        "originator": "pocket-agent",
    }
    account_id = _extract_account_id(access_token)
    if account_id:
        headers["chatgpt-account-id"] = account_id

    try:
        resp = httpx.get(
            f"{CODEX_BASE_URL}/models",
            params={"client_version": "99.0.0"},
            headers=headers,
            timeout=10,
            verify=_SSL_VERIFY,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []

    items = data.get("models", data.get("data", [])) if isinstance(data, dict) else data
    slugs: list[str] = []
    for item in items or []:
        if isinstance(item, str):
            slug = item
        elif isinstance(item, dict):
            slug = item.get("slug") or item.get("id") or item.get("model") or ""
        else:
            continue
        if slug and "review" not in slug and slug not in slugs:
            slugs.append(slug)
    return slugs


def logout(provider: str = "openai") -> None:
    cleared = False
    key = f"{provider}-token"
    try:
        keyring.delete_password(KEYCHAIN_SERVICE, key)
        cleared = True
    except Exception:
        pass
    if TOKEN_FILE.exists():
        try:
            tokens = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
            if key in tokens:
                del tokens[key]
                TOKEN_FILE.write_text(json.dumps(tokens), encoding="utf-8")
                cleared = True
        except Exception:
            pass
    print("✓ 已登出。" if cleared else "（沒有找到已儲存的登入資訊）")


def has_valid_token(provider: str = "openai") -> bool:
    token = _load_token(provider)
    return bool(token and not _is_expired(token))


def get_user_info(provider: str = "openai") -> dict:
    token = _load_token(provider)
    if not token:
        return {}

    payload = _decode_jwt_payload(token.get("access_token", ""))
    auth = payload.get("https://api.openai.com/auth", {})
    profile = payload.get("https://api.openai.com/profile", {})
    return {
        "email": profile.get("email", ""),
        "plan": auth.get("chatgpt_plan_type", ""),
    }


def get_model() -> str:
    cfg = load_config()
    return cfg.get("model") or os.getenv("OPENAI_MODEL", "gpt-6-astra")


def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
