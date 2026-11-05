import ipaddress
import logging
import re
import socket
from urllib.parse import urlparse

import httpx

from pocket_agent.system.constants import (
    DEFAULT_MAX_REDIRECTS,
    DEFAULT_NETWORK_TIMEOUT_SEC,
    DEFAULT_WEB_CONTENT_MAX_CHARS,
    MAX_RESPONSE_BYTES,
)

log = logging.getLogger("pocket-agent")

WEB_TOOLS = [
    {
        "type": "function",
        "name": "search_web",
        "description": (
            "Perform a lightweight web search using DuckDuckGo to find information, documentation, or solutions."
        ),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "The search query."}},
            "required": ["query"],
        },
        "_is_safe_read": True,
    },
    {
        "type": "function",
        "name": "read_url_content",
        "description": (
            "Fetch the content of a specific URL and extract its text. Useful for reading documentation or articles."
        ),
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "The absolute URL to fetch."}},
            "required": ["url"],
        },
        "_is_safe_read": True,
    },
]


def is_web_tool(name: str) -> bool:
    return any(t["name"] == name for t in WEB_TOOLS)


def is_soft_web_error(result: object) -> bool:
    """判斷是否為外部網站回傳的軟性狀態錯誤（如 HTTP 404/403/410），避免觸發工具系統連續崩潰熔斷。"""
    if not isinstance(result, str):
        return False
    return any(code in result for code in ("HTTP 404", "HTTP 403", "HTTP 410"))


def _build_404_fallback_query(url: str) -> str:
    """從 404 網址萃取網域與路徑關鍵字，供自動退避搜尋使用。"""
    parsed = urlparse(url)
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return ""
    ignore_tokens = {"html", "htm", "php", "asp", "aspx", "jsp", "md", "txt", "xml", "json", "index", "docs", "doc"}
    raw_tokens = re.split(r"[/\-_.]+", parsed.path or "")
    keywords: list[str] = []
    for tok in raw_tokens:
        clean = tok.strip().lower()
        if len(clean) < 2 or clean.isdigit() or clean in ignore_tokens:
            continue
        if clean not in keywords:
            keywords.append(clean)
    if keywords:
        return f"site:{host} {' '.join(keywords[:6])}"
    return f"site:{host}"


def _search_ddg_snippets(query: str, headers: dict, max_results: int = 5) -> tuple[list[str], str | None]:
    """向 DuckDuckGo HTML 介面查詢並解析前 max_results 筆結果。回傳 (結果列表, 錯誤訊息或 None)。"""
    url = "https://html.duckduckgo.com/html/"
    try:
        from curl_cffi import requests as c_requests

        resp = c_requests.post(
            url,
            data={"q": query},
            headers=headers,
            impersonate="chrome",
            timeout=DEFAULT_NETWORK_TIMEOUT_SEC,
        )
        resp.raise_for_status()
    except Exception as curl_e:
        log.debug("search_web: curl_cffi 失敗，降級 httpx: %s", curl_e)
        try:
            resp = httpx.post(
                url,
                data={"q": query},
                headers=headers,
                follow_redirects=True,
                timeout=DEFAULT_NETWORK_TIMEOUT_SEC,
            )
            resp.raise_for_status()
        except httpx.HTTPError as httpx_e:
            err = (
                f"[ERROR] 網路操作失敗 (curl_cffi: {curl_e} | httpx: {httpx_e})\n\n"
                "(提示：如果您頻繁被擋，可以嘗試使用 Browser 等其他工具)"
            )
            return [], err

    html = resp.text
    results: list[str] = []
    snippets = re.findall(r'<a class="result__snippet[^>]*>(.*?)</a>', html, re.DOTALL)
    urls = re.findall(r'<a class="result__url" href="([^"]+)">', html)

    for i in range(min(len(snippets), max_results, len(urls))):
        text = _strip_html(snippets[i]).strip()
        link = urls[i].strip()
        if link.startswith("//duckduckgo.com/l/?"):
            import urllib.parse

            parsed = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
            if "uddg" in parsed:
                link = parsed["uddg"][0]
        results.append(f"{i + 1}. {text}\n   URL: {link}")

    return results, None


def _format_404_fallback(url: str, headers: dict) -> str:
    """當讀取網址遭遇 HTTP 404 時，自動以網址關鍵字檢索相近頁面並回傳導航建議。"""
    fallback_query = _build_404_fallback_query(url)
    hint = (
        "\n\n[提示] 目標網頁不存在或路徑已改版。請優先使用 `search_web` 取得真實存在的網址，"
        "切勿憑記憶猜測深層文件路徑。"
    )
    if not fallback_query:
        return hint
    try:
        results, err = _search_ddg_snippets(fallback_query, headers, max_results=3)
        if not err and results:
            joined = "\n\n".join(results)
            return f"\n\n[404 自動退避檢索] 系統已自動以「{fallback_query}」搜尋該站點相近頁面：\n\n{joined}{hint}"
    except Exception as e:
        log.debug("404 fallback search failed: %s", e)
    return hint


def _strip_html(html_str: str) -> str:
    """簡易移除 HTML 標籤並抽取純文字 (避免 bs4 依賴)"""
    from html.parser import HTMLParser

    class MLStripper(HTMLParser):
        def __init__(self):
            super().__init__()
            self.reset()
            self.strict = False
            self.convert_charrefs = True
            self.text = []
            self.in_style_script = False

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style"):
                self.in_style_script = True

        def handle_endtag(self, tag):
            if tag in ("script", "style"):
                self.in_style_script = False

        def handle_data(self, d):
            if not self.in_style_script:
                self.text.append(d)

        def get_data(self):
            return "".join(self.text)

    try:
        s = MLStripper()
        s.feed(html_str)
        text = s.get_data()
        # 整理連續空白
        return re.sub(r"\n\s*\n", "\n\n", text).strip()
    except Exception:
        # Fallback regex fallback
        return re.sub(r"<[^>]+>", " ", html_str)


# 向後相容別名 (對齊 constants.py)
MAX_REDIRECTS = DEFAULT_MAX_REDIRECTS
_REDIRECT_CODES = (301, 302, 303, 307, 308)
_EXTERNAL_TAG_RE = re.compile(r"<\s*/?\s*external_content\s*>", re.IGNORECASE)


class _BlockedAddressError(Exception):
    """目標位址屬於內部或保留網段。"""


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """判斷位址是否非公開（含私有、loopback、link-local、保留、未指定、CGNAT、IPv4-mapped 內網）。"""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (not ip.is_global) or ip.is_multicast


DEFAULT_ALLOWED_SSRF_DOMAINS = ("github.com", "raw.githubusercontent.com")


def _is_allowed_domain(hostname: str) -> bool:
    """檢查主機名稱是否屬於允許內部/代理解析的受信任白名單網域。"""
    if not hostname:
        return False
    host_lower = hostname.lower().strip(".")
    allowed_list = list(DEFAULT_ALLOWED_SSRF_DOMAINS)
    try:
        from pocket_agent.system.config import config

        custom = config.get("web.ssrf_allowed_domains")
        if custom:
            if isinstance(custom, str):
                allowed_list.extend(d.strip().lower() for d in custom.split(",") if d.strip())
            elif isinstance(custom, (list, tuple, set)):
                allowed_list.extend(str(d).strip().lower() for d in custom if str(d).strip())
    except Exception:
        pass

    for domain in allowed_list:
        if host_lower == domain or host_lower.endswith(f".{domain}"):
            return True
    return False


def _resolve_public_ip(hostname: str, port: int) -> str:
    """解析主機名稱（IPv4 與 IPv6），所有結果皆須為公開位址，回傳第一個供固定連線使用。

    若環境解析為內部/保留位址，但主機名稱符合受信任白名單，且非本機 loopback / link-local，則予以放行。
    """
    if not hostname:
        raise ValueError("缺少主機名稱")
    infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    addrs: list[str] = []
    is_allowed = _is_allowed_domain(hostname)
    for info in infos:
        ip_str = str(info[4][0]).split("%")[0]
        ip_obj = ipaddress.ip_address(ip_str)
        if _is_blocked_ip(ip_obj):
            # 高風險本機 loopback、link-local (雲端 metadata) 絕不放行
            if ip_obj.is_loopback or ip_obj.is_link_local:
                raise _BlockedAddressError(f"{hostname} -> {ip_str}")
            if is_allowed:
                log.info("SSRF 豁免白名單網域: %s -> %s (企業代理/內網解析)", hostname, ip_str)
            else:
                raise _BlockedAddressError(f"{hostname} -> {ip_str}")
        addrs.append(ip_str)
    if not addrs:
        raise ValueError("DNS 無解析結果")
    return addrs[0]


def _read_limited(chunks) -> tuple[bytes, bool]:
    """從區塊迭代器讀取資料，超過 MAX_RESPONSE_BYTES 即截斷。回傳 (內容, 是否被截斷)。"""
    buf = bytearray()
    for chunk in chunks:
        buf.extend(chunk)
        if len(buf) > MAX_RESPONSE_BYTES:
            return bytes(buf[:MAX_RESPONSE_BYTES]), True
    return bytes(buf), False


def _decode_body(body: bytes, content_type: str) -> str:
    """依 Content-Type 的 charset 解碼，失敗時退回 UTF-8（替代字元）。"""
    match = re.search(r"charset=([\w-]+)", content_type, re.IGNORECASE)
    for enc in ([match.group(1)] if match else []) + ["utf-8"]:
        try:
            return body.decode(enc)
        except UnicodeDecodeError, LookupError:
            continue
    return body.decode("utf-8", errors="replace")


def _fetch_pinned(url: str, ip: str, headers: dict) -> tuple[int, dict, bytes, bool, bool]:
    """以已驗證的 IP 連線（避免 DNS rebinding），串流讀取並限制大小。

    回傳 (狀態碼, 小寫鍵標頭, 內容, 是否截斷, curl_cffi 是否缺套件)。
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    curl_missing = False

    try:
        from curl_cffi import requests as c_requests
    except ImportError:
        c_requests = None
        curl_missing = True

    if c_requests is not None:
        try:
            resp = c_requests.get(
                url,
                headers=headers,
                impersonate="chrome",
                timeout=DEFAULT_NETWORK_TIMEOUT_SEC,
                allow_redirects=False,
                stream=True,
                resolve=[f"{host}:{port}:{ip}"],
            )
            try:
                body, truncated = _read_limited(resp.iter_content(chunk_size=8192))
            finally:
                resp.close()
            return resp.status_code, {k.lower(): v for k, v in resp.headers.items()}, body, truncated, False
        except Exception as e:
            log.debug("curl_cffi 請求失敗，改用 httpx: %s", e)

    ip_host = f"[{ip}]" if ":" in ip else ip
    pinned_url = parsed._replace(netloc=f"{ip_host}:{port}").geturl()
    pinned_headers = dict(headers)
    pinned_headers["Host"] = parsed.netloc.rsplit("@", 1)[-1]
    extensions = {"sni_hostname": host} if parsed.scheme == "https" else {}
    # trust_env=False：避免環境 proxy 重新解析主機名稱而繞過固定 IP
    with httpx.Client(timeout=DEFAULT_NETWORK_TIMEOUT_SEC, follow_redirects=False, trust_env=False) as client:
        with client.stream("GET", pinned_url, headers=pinned_headers, extensions=extensions) as r:
            body, truncated = _read_limited(r.iter_bytes())
            return r.status_code, {k.lower(): v for k, v in r.headers.items()}, body, truncated, curl_missing


def _read_url(url: str, headers: dict) -> str:
    """讀取網址內容：逐跳驗證公開位址、固定 IP 連線、限制大小，並跳脫隔離標籤。"""
    warning_msg = ""
    current_url = url
    status = 0
    resp_headers: dict = {}
    body = b""
    truncated = False

    for _ in range(MAX_REDIRECTS):
        parsed = urlparse(current_url)
        if parsed.scheme not in ("http", "https"):
            return "[ERROR] 僅支援 http / https 網址"

        # SSRF 防護：解析一次並驗證，之後以該 IP 連線
        hostname = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            ip = _resolve_public_ip(hostname, port)
        except _BlockedAddressError as e:
            return f"[ERROR] 安全限制：禁止存取內部或保留網段 ({e})"
        except Exception as e:
            return f"[ERROR] DNS 解析失敗或位址無效: {e}"

        try:
            status, resp_headers, body, truncated, curl_missing = _fetch_pinned(current_url, ip, headers)
        except httpx.HTTPError as e:
            return f"[ERROR] 網路操作失敗: {e}{warning_msg}"
        if curl_missing and not warning_msg:
            warning_msg = (
                "\n\n(提示：強烈建議使用 run_command 執行 pip install curl_cffi，"
                "以模擬真實瀏覽器指紋，避免遭遇 403 Forbidden)"
            )

        if status in _REDIRECT_CODES:
            location = resp_headers.get("location")
            if not location:
                break
            from urllib.parse import urljoin

            current_url = urljoin(current_url, location)
            continue

        if status == 404:
            fallback_block = _format_404_fallback(current_url, headers)
            return f"[ERROR] 網路操作失敗: HTTP 404{fallback_block}{warning_msg}"

        if status >= 400:
            return f"[ERROR] 網路操作失敗: HTTP {status}{warning_msg}"
        break
    else:
        return f"[ERROR] 重新導向次數過多{warning_msg}"

    content_type = resp_headers.get("content-type", "")
    text = _decode_body(body, content_type)
    if "text/html" in content_type:
        text = _strip_html(text)

    # 限制長度避免爆 Context
    max_len = DEFAULT_WEB_CONTENT_MAX_CHARS
    inner = text
    if len(text) > max_len:
        inner = text[:max_len] + f"\n\n... (內容過長，已截斷前 {max_len} 字元)"
    elif truncated:
        inner = text + f"\n\n... (回應超過 {MAX_RESPONSE_BYTES} 位元組，已截斷)"

    # 跳脫內容中的隔離標籤，避免外部內容提早結束 <external_content> 區塊
    inner = _EXTERNAL_TAG_RE.sub("[external_content tag removed]", inner)
    inner += warning_msg

    return f"<external_content>\n{inner}\n</external_content>"


def call_web_tool(name: str, args: dict) -> str:
    """執行輕量級網路工具。"""
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        }

        if name == "search_web":
            query = args.get("query", "")
            if not query:
                return "[ERROR] 缺少必填參數 'query'"

            results, err = _search_ddg_snippets(query, headers, max_results=5)
            if err:
                return err

            if not results:
                return "搜尋無結果，或遭到搜尋引擎阻擋（亦可能是搜尋頁面結構已改版，導致解析失敗）。"
            return "搜尋結果 (DuckDuckGo)：\n\n" + "\n\n".join(results)

        elif name == "read_url_content":
            url = args.get("url", "")
            if not url:
                return "[ERROR] 缺少必填參數 'url'"
            return _read_url(url, headers)

        return f"[ERROR] 未知的工具名稱: {name}"

    except Exception as e:
        log.info(f"Web Tool {name} 執行失敗: {e}")
        return f"[ERROR] 網路操作失敗: {e}"
