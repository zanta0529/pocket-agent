"""OpenAI OAuth 2.0 PKCE — 用 ChatGPT 帳號（Plus/Pro）登入，不需要 API Key。

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
# truststore 讓 Python 讀 Windows/macOS/Linux 的系統憑證庫，
# 跟瀏覽器用同一套 CA，公司 MITM proxy 就能過。
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
# 預設使用 openclaw 的 OAuth App（無需自行申請即可使用）。
# 若要換成自己的 App，在 .env 設定 OPENAI_CLIENT_ID。

DEFAULT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

AUTH_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
REDIRECT_URI = "http://localhost:1455/auth/callback"  # 必須跟 client_id 登記的一致
SCOPE = "openid profile email offline_access"

# ChatGPT 訂閱後端（走 Plus/Pro 配額，不需要 API 帳戶餘額）
CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"

# ── 授權流程參數（登入失敗時的調整旋鈕）──────────
# codex_cli_simplified_flow 會讓 OpenAI 在伺服器端多做一次 token
# 交換，失敗時瀏覽器會停在他們自己的錯誤頁（token_exchange_failed），
# 根本不會導回本機 callback，程式端偵測不到也無從重試。
# 關掉它就走標準 OAuth code flow，改由本程式自己拿 code 換 token。
#
# originator 則是 OpenAI 用來識別呼叫端的字串。這兩個都可能因為
# 對方調整白名單而突然不被接受，所以留成環境變數方便當場試。
ORIGINATOR = os.getenv("OPENAI_ORIGINATOR", "pocket-agent")

# id_token_add_organizations 要求 OpenAI 把組織資訊寫進 ID token。
# 公司 Business / Enterprise workspace 若對此有限制，這一步就是
# token_exchange_failed 的來源。設 0 可以不要求組織資訊。
ADD_ORGANIZATIONS = os.getenv("OPENAI_ADD_ORGANIZATIONS", "1").strip().lower() not in ("0", "false", "no", "off")
USE_SIMPLIFIED_FLOW = os.getenv("OPENAI_CODEX_SIMPLIFIED_FLOW", "1").strip().lower() not in ("0", "false", "no", "off")

log = logging.getLogger("pocket-agent")

KEYCHAIN_SERVICE = "pocket-agent"
KEYCHAIN_KEY = "openai-token"
CONFIG_PATH = Path.home() / ".pocket-agent" / "config.json"
TOKEN_FILE = Path.home() / ".pocket-agent" / "token.json"  # keyring fallback


# ── PKCE ─────────────────────────────────────────


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# ── JWT 解析（不驗簽，只讀 payload）─────────────


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


# ── Token 儲存（Keychain 優先，失敗 fallback 到檔案）──
# Windows Credential Manager 有 2500 bytes 限制，JWT token 容易超過；
# 公司 group policy 也可能禁止寫入。失敗時改存 ~/.pocket-agent/token.json。


def _save_token(token: dict) -> None:
    if "expires_in" in token and "expires_at" not in token:
        token["expires_at"] = time.time() + token["expires_in"]
    data = json.dumps(token)
    try:
        keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_KEY, data)
    except Exception:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(data, encoding="utf-8")


def _load_token() -> dict | None:
    # 1. Keychain
    try:
        raw = keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_KEY)
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    # 2. 檔案 fallback
    if TOKEN_FILE.exists():
        try:
            return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def _is_expired(token: dict) -> bool:
    return token.get("expires_at", 0) < time.time() + 60


def _try_refresh(token: dict, client_id: str) -> dict | None:
    refresh = token.get("refresh_token")
    if not refresh:
        return None
    try:
        resp = httpx.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": refresh,
            },
            timeout=10,
            verify=_SSL_VERIFY,
        )
        if resp.status_code == 200:
            new_token = {**token, **resp.json()}
            new_token.pop("expires_at", None)  # 移除舊值，讓 _save_token 依新的 expires_in 重算
            _save_token(new_token)
            return new_token
    except Exception:
        pass
    return None


# ── 瀏覽器 OAuth 流程 ─────────────────────────────


def _browser_oauth(client_id: str, on_auth_url=None, cancel_event=None) -> dict:
    """開瀏覽器讓用戶用 ChatGPT 帳號授權，回傳 token dict。

    on_auth_url：callable(url, opened) — 網址備妥後回報，讓 GUI 顯示
    真實狀態；瀏覽器開不起來時使用者才拿得到網址可以自己貼。

    cancel_event：threading.Event — 設起來就中止等待並關掉 callback
    server。使用者放棄登入時若不收掉，它會一直占著 port 1455，
    下一次登入綁不上而整個流程走不下去。
    """
    verifier, challenge = _pkce_pair()
    expected_state = secrets.token_urlsafe(16)

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "state": expected_state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        # OpenAI 專用參數（參考 openclaw 實作）
        "originator": ORIGINATOR,
    }
    if ADD_ORGANIZATIONS:
        params["id_token_add_organizations"] = "true"
    if USE_SIMPLIFIED_FLOW:
        params["codex_cli_simplified_flow"] = "true"

    log.info(
        "OAuth 授權流程：originator=%s simplified_flow=%s add_orgs=%s",
        ORIGINATOR,
        USE_SIMPLIFIED_FLOW,
        ADD_ORGANIZATIONS,
    )
    auth_url = AUTH_URL + "?" + urlencode(params)

    bucket: dict = {}
    app = FastAPI()

    def _page(color: str, message: str) -> HTMLResponse:
        return HTMLResponse(
            f"<h1 style='font-family:sans-serif;text-align:center;padding-top:20vh;color:{color}'>{message}</h1>"
        )

    @app.get("/auth/callback")
    async def callback(
        code: str = "",
        state: str = "",
        error: str = "",
        error_description: str = "",
    ):
        # error / code 都設成選填：授權被拒時 OpenAI 只帶 error 回來，
        # 若把 code 宣告成必填，FastAPI 會直接回 422 驗證錯誤頁，
        # 使用者只看到一堆 JSON，不知道發生什麼事。
        if error:
            log.error("OAuth 被拒絕：%s %s", error, error_description)
            bucket["error"] = error_description or error
            return _page("#c0392b", f"✗ 授權未完成：{error}")

        # state 必須是本次流程產生的那一組。沒有這個檢查的話，
        # 另一個視窗（或另一個 pocket-agent 實例）的授權碼也會被收下，
        # 拿去換 token 時因為 PKCE verifier 不同而失敗，
        # 錯誤訊息卻完全看不出真正的原因。
        if state != expected_state:
            log.error("OAuth state 不符（可能是舊分頁或另一個實例）")
            return _page(
                "#c0392b",
                "✗ 驗證碼不符。<br>"
                "這通常是因為此分頁是舊的授權頁，"
                "或同時開了兩個 My Agent。<br>"
                "請關閉所有相關分頁與程式，重新登入一次。",
            )

        if not code:
            return _page("#c0392b", "✗ 沒有收到授權碼。")

        bucket["code"] = code
        return _page("#10a37f", "✓ 認證成功！請關閉此視窗回到 My Agent。")

    # log_config=None 是必要的：uvicorn 預設的 log formatter 在初始化時
    # 執行 sys.stdout.isatty()，而可攜版用 pythonw.exe 啟動時 sys.stdout
    # 是 None，會拋 ValueError: Unable to configure formatter 'default'。
    # 這行在開瀏覽器之前，炸掉的話瀏覽器根本沒機會開。
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="localhost",
            port=1455,
            log_level="error",
            log_config=None,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # 確認 callback server 真的起來了。port 1455 被占用時（多半是前一次
    # 登入沒收乾淨的行程還掛在那），uvicorn 只會在 daemon thread 裡拋
    # WinError 10048 然後靜靜死掉——主流程毫不知情，照樣開瀏覽器，
    # 授權碼被舊行程接走，這邊傻等五分鐘才逾時。
    for _ in range(30):
        if server.started:
            break
        if not thread.is_alive():
            raise RuntimeError(
                "無法在 port 1455 啟動登入用的本機服務。"
                "多半是還有另一個 My Agent 正在執行（或前一次登入沒有正常結束）。"
                "請關閉所有 My Agent 視窗後再試一次。"
            )
        time.sleep(0.1)
    else:
        raise RuntimeError("登入用的本機服務啟動逾時，請重試。")

    # 嘗試開啟瀏覽器；在遠端/VPS 環境或瀏覽器關聯損壞時可能失敗
    try:
        opened = webbrowser.open(auth_url)
    except Exception:
        log.exception("開啟瀏覽器失敗")
        opened = False

    # 回報實際結果給呼叫端（GUI 用來顯示正確狀態與備用網址）。
    # 沒有這個的話，GUI 只能在按下按鈕當下先樂觀地說「瀏覽器已開啟」，
    # 開不起來時使用者完全不知道發生什麼事，也拿不到網址。
    if on_auth_url is not None:
        try:
            on_auth_url(auth_url, opened)
        except Exception:
            log.exception("on_auth_url callback 失敗")

    if opened:
        print("\n[Auth] 瀏覽器已開啟，請用你的 ChatGPT 帳號（Plus/Pro）登入並授權…")
    else:
        print("\n[Auth] 無法自動開啟瀏覽器，請手動複製以下網址：")
    print(f"\n  {auth_url}\n")
    print("[Auth] 授權完成後會自動繼續，請勿關閉此視窗。")

    # 等待 callback（15 秒後提示可手動貼 redirect URL，仿 openclaw 行為）
    for i in range(300):
        if "code" in bucket:
            server.should_exit = True
            break
        # 使用者在授權頁按了拒絕：立刻收工並說明，不要空等到逾時
        if "error" in bucket:
            server.should_exit = True
            raise RuntimeError(f"授權未完成：{bucket['error']}")

        # 使用者放棄登入（按了登出或重新登入）：收掉 server 釋放 port
        if cancel_event is not None and cancel_event.is_set():
            server.should_exit = True
            raise RuntimeError("登入已取消。")
        # GUI（尤其可攜版的 pythonw.exe）沒有 stdin，input() 會直接拋
        # 例外把整個登入流程炸掉。只有終端機模式才走手動貼上這條路；
        # GUI 端靠 on_auth_url 拿到網址自己顯示。
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

    resp = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": bucket["code"],
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
        },
        timeout=10,
        verify=_SSL_VERIFY,
    )
    resp.raise_for_status()
    return resp.json()


# ── 主要 API ──────────────────────────────────────


def get_access_token(on_auth_url=None, cancel_event=None) -> str:
    """取得有效 access token；必要時開瀏覽器重新登入。

    on_auth_url 會原樣傳給 _browser_oauth，只有真的要重新授權時才觸發。
    """
    client_id = os.getenv("OPENAI_CLIENT_ID") or DEFAULT_CLIENT_ID

    token = _load_token()

    if token and not _is_expired(token):
        return token["access_token"]

    if token:
        refreshed = _try_refresh(token, client_id)
        if refreshed:
            return refreshed["access_token"]

    token = _browser_oauth(client_id, on_auth_url, cancel_event)
    _save_token(token)
    print("[Auth] ✓ 登入成功！\n")
    return token["access_token"]


def get_openai_client():
    """建立 OpenAI client，指向 ChatGPT 訂閱後端（不需要 API 帳戶餘額）。"""
    from openai import AsyncOpenAI

    access_token = get_access_token()
    account_id = _extract_account_id(access_token)

    extra_headers = {
        "originator": "pocket-agent",
        "OpenAI-Beta": "responses=experimental",  # Codex 後端必要
    }
    if account_id:
        extra_headers["chatgpt-account-id"] = account_id

    return AsyncOpenAI(
        api_key=access_token,
        base_url=CODEX_BASE_URL,  # chatgpt.com/backend-api/codex
        default_headers=extra_headers,
    )


def fetch_available_models() -> list[str]:
    """從 codex 後端取得可用模型 slug 列表。

    未登入、token 過期且無法刷新、或請求失敗時回傳空 list（不觸發 OAuth 流程）。
    會過濾掉非對話用途的項目（如 codex-auto-review）。
    """
    token = _load_token()
    if token and _is_expired(token):
        client_id = os.getenv("OPENAI_CLIENT_ID") or DEFAULT_CLIENT_ID
        token = _try_refresh(token, client_id)
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


def logout() -> None:
    cleared = False
    try:
        keyring.delete_password(KEYCHAIN_SERVICE, KEYCHAIN_KEY)
        cleared = True
    except Exception:
        pass
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()
        cleared = True
    print("✓ 已登出。" if cleared else "（沒有找到已儲存的登入資訊）")


def has_valid_token() -> bool:
    """有沒有有效的 token（不觸發 OAuth）。"""
    token = _load_token()
    return bool(token and not _is_expired(token))


def get_user_info() -> dict:
    """從 JWT 提取用戶資訊，供 GUI 顯示。"""
    token = _load_token()
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
