"""網站與網路診斷專用機密遮罩與防注入策略（Strategy Pattern）。"""

import re
from collections.abc import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from mcp_servers._shared import CONSTANTS, DefaultRedactionPolicy

_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "api_key",
        "apikey",
        "key",
        "secret",
        "client_secret",
        "password",
        "passwd",
        "pwd",
        "sig",
        "signature",
        "auth",
        "session",
        "sessionid",
        "sid",
        "jwt",
        "code",
        "x-amz-signature",
        "x-amz-security-token",
    }
)

_COOKIE_SAFE_FLAGS = frozenset({"httponly", "secure", "samesite", "path", "domain", "max-age", "expires"})

ALLOWED_RESPONSE_HEADERS = frozenset(
    {
        "server",
        "via",
        "location",
        "content-type",
        "content-length",
        "content-encoding",
        "cache-control",
        "expires",
        "age",
        "etag",
        "vary",
        "alt-svc",
        "retry-after",
        "strict-transport-security",
        "content-security-policy",
        "x-frame-options",
        "x-content-type-options",
        "referrer-policy",
        "permissions-policy",
        "cross-origin-opener-policy",
        "cross-origin-embedder-policy",
        "access-control-allow-origin",
        "access-control-allow-methods",
        "access-control-allow-headers",
        "cf-ray",
        "cf-cache-status",
        "x-cache",
        "x-cache-hits",
        "x-amz-cf-id",
        "x-amz-cf-pop",
        "x-served-by",
        "x-vercel-id",
        "x-azure-ref",
        "x-request-id",
        "x-correlation-id",
        "x-powered-by",
    }
)

_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_SHIELD_REDACTED_AMP = "=[REDACTED] \ue000&"


class NetdiagRedactionPolicy(DefaultRedactionPolicy):
    """繼承通用遮罩策略，額外保護 URL 查詢機密、Set-Cookie 值與回應標頭防注入。"""

    def redact(self, text: str, *, extra_secret: str = "") -> str:
        """保護已遮罩之 URL Query 參數（=[REDACTED]&）不被父類 _KV_RE 跨參數吞併。"""
        if not text:
            return text
        shielded = text.replace("=[REDACTED]&", _SHIELD_REDACTED_AMP)
        redacted = super().redact(shielded, extra_secret=extra_secret)
        return redacted.replace(_SHIELD_REDACTED_AMP, "=[REDACTED]&")

    def redact_url(self, url: str) -> str:
        """移除 URL 內嵌帳密並遮罩敏感查詢參數值。"""
        if not url:
            return url
        cleaned = _CTRL_RE.sub(" ", url).strip()
        try:
            parts = urlsplit(cleaned)
        except ValueError:
            return self.redact(cleaned)

        netloc = parts.netloc
        if "@" in netloc:
            _, host_part = netloc.rsplit("@", 1)
            netloc = f"[REDACTED]@{host_part}"

        safe_path = self.redact(parts.path)
        query = parts.query
        if query:
            pairs = parse_qsl(query, keep_blank_values=True)
            encoded_parts: list[str] = []
            for k, v in pairs:
                if k.lower() in _SENSITIVE_QUERY_KEYS:
                    encoded_k = urlencode([(k, "")]).rstrip("=")
                    encoded_parts.append(f"{encoded_k}=[REDACTED]")
                else:
                    encoded_parts.append(urlencode([(k, self.redact(v))]))
            query = "&".join(encoded_parts)

        rebuilt = urlunsplit((parts.scheme, netloc, safe_path, query, ""))
        return self.redact(rebuilt)

    def sanitize_header_value(self, value: str) -> str:
        """清除控制字元、套用機密遮罩並裁切至安全長度上限。"""
        cleaned = _CTRL_RE.sub(" ", value or "").strip()
        redacted = self.redact(cleaned)
        return self.clip_text(redacted, CONSTANTS.netdiag_max_header_value_chars)

    def summarize_set_cookie(self, raw_cookie: str) -> str:
        """僅抽取 Set-Cookie 名稱與安全旗標，絕對不輸出 Cookie 實際值。"""
        cleaned = _CTRL_RE.sub(" ", raw_cookie or "").strip()
        if not cleaned:
            return ""
        segments = [seg.strip() for seg in cleaned.split(";") if seg.strip()]
        if not segments:
            return ""
        name_part = segments[0].split("=", 1)[0].strip() or "<unnamed>"
        flags: list[str] = []
        for seg in segments[1:]:
            key = seg.split("=", 1)[0].strip().lower()
            if key in _COOKIE_SAFE_FLAGS:
                flags.append(self.clip_text(seg, 64))
        flags_str = "; ".join(flags) if flags else "no-flags"
        return f"{name_part}=[REDACTED] ({flags_str})"

    def filter_response_headers(self, headers: Iterable[tuple[str, str]]) -> tuple[dict[str, str], tuple[str, ...]]:
        """篩選白名單回應標頭並摘要 Set-Cookie 旗標。"""
        filtered: dict[str, str] = {}
        cookies: list[str] = []
        for raw_key, raw_val in headers:
            key_lower = raw_key.strip().lower()
            if key_lower == "set-cookie":
                summary = self.summarize_set_cookie(raw_val)
                if summary:
                    cookies.append(summary)
                continue
            if key_lower in ALLOWED_RESPONSE_HEADERS:
                safe_val = self.sanitize_header_value(raw_val)
                if key_lower in filtered:
                    merged = f"{filtered[key_lower]}, {safe_val}"
                    filtered[key_lower] = self.clip_text(merged, CONSTANTS.netdiag_max_header_value_chars)
                else:
                    filtered[key_lower] = safe_val
        return filtered, tuple(cookies)


NETDIAG_REDACTION = NetdiagRedactionPolicy()
