"""OAuth 2.0 PKCE — 支援 OpenAI (ChatGPT Plus/Pro) 登入。"""

import base64
import hashlib
import json
import logging
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

# ── SSL：優先用 OS 系統憑證庫 ──
try:
    import truststore

    truststore.inject_into_ssl()
    _SSL_VERIFY = True  # truststore 已接管，用預設即可
except ImportError:
    import sys

    if sys.platform == "win32":
        raise RuntimeError("Windows 環境下必須安裝 truststore 以確保企業憑證安全。請在環境中安裝 truststore。")
    _SSL_VERIFY = certifi.where()  # fallback：Mozilla CA bundle

from pocket_agent.system.config import config

# ── OAuth 設定 ────────────────────────────────────
DEFAULT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

OPENAI_AUTH_URL = config.get("providers.openai.auth_url", "https://auth.openai.com/oauth/authorize")
OPENAI_TOKEN_URL = config.get("providers.openai.token_url", "https://auth.openai.com/oauth/token")
REDIRECT_URI = config.get("providers.openai.redirect_uri", "http://localhost:1455/auth/callback")
OPENAI_SCOPE = config.get("providers.openai.scope", "openid profile email offline_access")

CODEX_BASE_URL = config.get("providers.openai.codex_base_url", "https://chatgpt.com/backend-api/codex")

ORIGINATOR = config.get("providers.openai.originator") or "pocket-agent"
ADD_ORGANIZATIONS = str(config.get("providers.openai.add_organizations", "1")).strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)
USE_SIMPLIFIED_FLOW = str(config.get("providers.openai.simplified_flow", "1")).strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)

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

    chunk_size = 1024  # Windows Keyring (CredWrite) 限制，避免 WinError 1783

    # 先取得現有舊 meta 資訊，但不先刪除，以備寫入失敗時保留原憑證
    old_meta = None
    try:
        old_meta_raw = keyring.get_password(KEYCHAIN_SERVICE, f"{key}-meta")
        if old_meta_raw:
            old_meta = json.loads(old_meta_raw)
    except Exception:
        pass

    try:
        if len(data) > chunk_size:
            chunks = [data[i : i + chunk_size] for i in range(0, len(data), chunk_size)]
            slot = f"s{int(time.time() * 1000)}"
            written_chunk_keys = []
            try:
                for i, chunk in enumerate(chunks):
                    chunk_key = f"{key}-{slot}-{i}"
                    keyring.set_password(KEYCHAIN_SERVICE, chunk_key, chunk)
                    written_chunk_keys.append(chunk_key)
                new_meta = {"chunks": len(chunks), "prefix": f"{key}-{slot}"}
                keyring.set_password(KEYCHAIN_SERVICE, f"{key}-meta", json.dumps(new_meta))
            except Exception:
                # 寫入失敗時 rollback 剛剛寫入的切塊，不破壞舊資料
                for ck in written_chunk_keys:
                    try:
                        keyring.delete_password(KEYCHAIN_SERVICE, ck)
                    except Exception:
                        pass
                raise

            # 新切塊與 meta 成功生效後，再清理舊有的資料與單一 key
            if old_meta:
                old_prefix = old_meta.get("prefix", key)
                for i in range(old_meta.get("chunks", 0)):
                    try:
                        keyring.delete_password(KEYCHAIN_SERVICE, f"{old_prefix}-{i}")
                    except Exception:
                        pass
            try:
                keyring.delete_password(KEYCHAIN_SERVICE, key)
            except Exception:
                pass
        else:
            # 短 token：直接寫入 key，成功後再清理舊切塊與 meta
            keyring.set_password(KEYCHAIN_SERVICE, key, data)
            if old_meta:
                old_prefix = old_meta.get("prefix", key)
                for i in range(old_meta.get("chunks", 0)):
                    try:
                        keyring.delete_password(KEYCHAIN_SERVICE, f"{old_prefix}-{i}")
                    except Exception:
                        pass
                try:
                    keyring.delete_password(KEYCHAIN_SERVICE, f"{key}-meta")
                except Exception:
                    pass
    except Exception as e:
        raise RuntimeError(
            f"系統憑證庫 (Keyring) 寫入失敗，基於安全考量，拒絕將 Token 以不安全方式降級儲存。詳細錯誤：{e}"
        )


def _load_token(provider: str) -> dict | None:
    key = f"{provider}-token"
    try:
        meta_raw = keyring.get_password(KEYCHAIN_SERVICE, f"{key}-meta")
        if meta_raw:
            meta = json.loads(meta_raw)
            prefix = meta.get("prefix", key)
            chunks = []
            for i in range(meta.get("chunks", 0)):
                chunk = keyring.get_password(KEYCHAIN_SERVICE, f"{prefix}-{i}")
                if chunk is None:
                    return None
                chunks.append(chunk)
            return json.loads("".join(chunks))

        raw = keyring.get_password(KEYCHAIN_SERVICE, key)
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return None


def _is_expired(token: dict) -> bool:
    return token.get("expires_at", 0) < time.time() + 60


def _try_refresh(provider: str, token: dict, client_id: str, client_secret: str = "") -> dict | None:
    refresh = token.get("refresh_token")
    if not refresh:
        return None

    from pocket_agent.system.constants import OAUTH_TOKEN_URLS

    token_url = OAUTH_TOKEN_URLS.get(provider, OPENAI_TOKEN_URL)

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

    if provider == "gemini-oauth":
        auth_url_base = "https://accounts.google.com/o/oauth2/v2/auth"
        token_url = "https://oauth2.googleapis.com/token"
        scope = config.get("providers.gemini.scope", "https://www.googleapis.com/auth/generative-language")
    else:
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
    }

    if provider != "gemini-oauth":
        params["originator"] = ORIGINATOR
        if ADD_ORGANIZATIONS:
            params["id_token_add_organizations"] = "true"
        if USE_SIMPLIFIED_FLOW:
            params["codex_cli_simplified_flow"] = "true"

    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse

    auth_url = auth_url_base + "?" + urlencode(params)
    bucket: dict = {}
    app = FastAPI()

    from urllib.parse import urlparse

    parsed_uri = urlparse(REDIRECT_URI)
    listen_port = parsed_uri.port or 1455
    listen_path = parsed_uri.path or "/auth/callback"

    def _page(color: str, message: str) -> HTMLResponse:
        return HTMLResponse(
            f"<h1 style='font-family:sans-serif;text-align:center;padding-top:20vh;color:{color}'>{message}</h1>"
        )

    @app.get(listen_path)
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

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=listen_port, log_level="error", log_config=None))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    for _ in range(30):
        if server.started:
            break
        if not thread.is_alive():
            raise RuntimeError(f"無法在 port {listen_port} 啟動登入用的本機服務。請確認 port 未被占用。")
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
                got_state = parsed.get("state", [""])[0]
                if got_state != expected_state:
                    bucket["error"] = "驗證碼不符 (State mismatch)。"
                    server.should_exit = True
                    break
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
    if provider == "gemini-oauth":
        client_id = config.get("providers.gemini.client_id")
        client_secret = config.get("providers.gemini.client_secret", "")
        if not client_id:
            raise ValueError("請先在設定檔設定 `providers.gemini.client_id` (Google OAuth Client ID)。")
    else:
        client_id = config.get("openai_client_id") or DEFAULT_CLIENT_ID
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


def get_provider_client(provider: str = None, model: str = None):
    """建立 OpenAI 相容的 client，根據 provider 透過 Provider Strategy 切換 Base URL 與 Auth。"""
    from dotenv import load_dotenv

    from pocket_agent.system.config import config
    from pocket_agent.system.paths import env_path
    from pocket_agent.system.providers import get_provider_strategy

    # 在此讀取 .env 以確保 os.getenv 能拿到最新的環境變數，配合 ConfigManager
    load_dotenv(env_path(), override=True)

    if not provider:
        provider = config.get("active_provider", "openai")

    strategy = get_provider_strategy(provider)
    return strategy.get_client()


def fetch_available_models(provider: str = "openai") -> list[str]:
    """取得可用模型列表。"""
    from dotenv import load_dotenv

    from pocket_agent.system.paths import env_path
    from pocket_agent.system.providers import get_provider_strategy

    load_dotenv(env_path(), override=True)

    strategy = get_provider_strategy(provider)
    return strategy.fetch_models()


def logout(provider: str = "openai") -> None:
    cleared = False
    key = f"{provider}-token"
    try:
        keyring.delete_password(KEYCHAIN_SERVICE, key)
        cleared = True
    except Exception:
        pass

    try:
        meta_raw = keyring.get_password(KEYCHAIN_SERVICE, f"{key}-meta")
        if meta_raw:
            meta = json.loads(meta_raw)
            prefix = meta.get("prefix", key)
            for i in range(meta.get("chunks", 0)):
                try:
                    keyring.delete_password(KEYCHAIN_SERVICE, f"{prefix}-{i}")
                except Exception:
                    pass
            keyring.delete_password(KEYCHAIN_SERVICE, f"{key}-meta")
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
    from pocket_agent.system.config import config

    return config.get("active_model", "gpt-6-astra")


def load_config() -> dict:
    from pocket_agent.system.config import config

    return dict(config.json_provider._data)


def save_config(cfg: dict) -> None:
    from pocket_agent.system.config import config

    config.json_provider._data = cfg
    config.json_provider.save()
