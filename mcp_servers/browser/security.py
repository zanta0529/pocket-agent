"""瀏覽器自動化安全策略、URL 白名單、機密遮罩與集中稽核模組（Strategy Pattern）。"""

import ipaddress
import os
import re
import urllib.parse
from abc import ABC, abstractmethod
from dataclasses import dataclass

from mcp_servers._shared import (
    CONSTANTS,
    DEFAULT_REDACTION_POLICY,
    SENSITIVE_COL_RE,
    AuditLogger,
    t,
)

_CTRL_OR_WS_RE = re.compile(r"[\x00-\x20\x7f]")
_SCHEME_PREFIX_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*):")
_SENSITIVE_FIELD_RE = re.compile(
    r"(?i)(?:password|passwd|pwd|secret|token|passphrase|api[_-]?key|apikey|"
    r"private[_-]?key|credential|one[-_]?time[-_]?code|otp|totp|cvv|cvc|cc-)"
)

_METADATA_V6 = ipaddress.ip_address("fd00:ec2::254")
_BROADCAST_V4 = ipaddress.ip_address("255.255.255.255")

BROWSER_REDACTION = DEFAULT_REDACTION_POLICY
AUDIT = AuditLogger("browser", redaction_policy=BROWSER_REDACTION)


def parse_bool_env(name: str, default: bool = False) -> bool:
    """解析布林環境變數（大小寫不敏感，去除前後空白）。"""
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _parse_csv_env(name: str) -> tuple[str, ...]:
    raw = os.getenv(name) or ""
    return tuple(item.strip().lower().rstrip(".") for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class BrowserConfig:
    """瀏覽器 MCP 執行期安全與行為設定。"""

    allowed_domains: tuple[str, ...]
    allow_private_networks: bool
    allow_loopback: bool
    timeout_ms: int
    headless: bool
    cdp_url: str


def load_browser_config() -> BrowserConfig:
    """從環境變數載入瀏覽器設定並套用系統常數邊界。"""
    raw_timeout = (os.getenv("BROWSER_TIMEOUT_MS") or "").strip()
    timeout_ms = CONSTANTS.browser_default_timeout_ms
    if raw_timeout:
        try:
            parsed = int(float(raw_timeout))
            timeout_ms = max(
                CONSTANTS.browser_min_timeout_ms,
                min(parsed, CONSTANTS.browser_max_timeout_ms),
            )
        except ValueError:
            pass
    return BrowserConfig(
        allowed_domains=_parse_csv_env("BROWSER_ALLOWED_DOMAINS"),
        allow_private_networks=parse_bool_env("BROWSER_ALLOW_PRIVATE_NETWORKS", False),
        allow_loopback=parse_bool_env("BROWSER_ALLOW_LOOPBACK", False),
        timeout_ms=timeout_ms,
        headless=parse_bool_env("BROWSER_HEADLESS", False),
        cdp_url=(os.getenv("BROWSER_CDP_URL") or "").strip(),
    )


def _matches_allowed_domains(hostname: str, allowed_domains: tuple[str, ...]) -> bool:
    """比對網域白名單（支援精確網域、子網域與 *.domain 萬用字元）。"""
    host = hostname.lower().rstrip(".")
    for raw_entry in allowed_domains:
        entry = raw_entry.strip().lower().rstrip(".")
        if not entry:
            continue
        if entry.startswith("*."):
            base = entry[2:]
            if base and host.endswith("." + base) and len(host) > len(base) + 1:
                return True
        else:
            base = entry.lstrip(".")
            if base and (host == base or host.endswith("." + base)):
                return True
    return False


def _is_cdp_endpoint_target(hostname: str, port: int | None, cdp_url: str) -> bool:
    """判斷目標是否指向目前設定的本機 CDP 除錯端點，防止自我接管。"""
    if not cdp_url:
        return False
    try:
        cdp_parts = urllib.parse.urlsplit(cdp_url)
    except ValueError:
        return False
    cdp_host = (cdp_parts.hostname or "").strip().strip("[]").lower().rstrip(".")
    cdp_port = cdp_parts.port or (443 if cdp_parts.scheme.lower() == "https" else 80)
    target_port = port or 80
    if target_port != cdp_port:
        return False
    loopback_hosts = {"localhost", "127.0.0.1", "::1"}
    if hostname in loopback_hosts and cdp_host in loopback_hosts:
        return True
    return bool(cdp_host and hostname == cdp_host)


class BrowserUrlPolicy(ABC):
    """瀏覽器導覽與請求安全驗證抽象策略。"""

    @abstractmethod
    def normalize_navigation_url(self, raw_url: str) -> str:
        """正規化導覽網址並阻擋不安全協定。"""

    @abstractmethod
    def check_url_allowed(self, url: str, *, is_subresource: bool = False) -> None:
        """驗證網址是否符合協定、Userinfo、保留網段與網域白名單規範。"""


class DefaultBrowserUrlPolicy(BrowserUrlPolicy):
    """預設瀏覽器 URL 安全策略：限定 HTTP(S)、阻擋 Userinfo 與 Metadata、分層控管內網與白名單。"""

    def __init__(self, config: BrowserConfig | None = None) -> None:
        self._fixed_config = config

    def _get_config(self) -> BrowserConfig:
        return self._fixed_config if self._fixed_config is not None else load_browser_config()

    def normalize_navigation_url(self, raw_url: str) -> str:
        cleaned = (raw_url or "").strip()
        if not cleaned or _CTRL_OR_WS_RE.search(cleaned):
            raise ValueError(t("browser_mcp.invalid_url"))

        if "://" in cleaned:
            scheme, rest = cleaned.split("://", 1)
            norm_scheme = scheme.lower()
            if norm_scheme not in ("http", "https"):
                raise ValueError(t("browser_mcp.unsupported_scheme", scheme=norm_scheme or "<empty>"))
            return f"{norm_scheme}://{rest}"

        scheme_match = _SCHEME_PREFIX_RE.match(cleaned)
        if scheme_match:
            scheme = scheme_match.group(1).lower()
            raise ValueError(t("browser_mcp.unsupported_scheme", scheme=scheme))

        return f"https://{cleaned}"

    def check_url_allowed(self, url: str, *, is_subresource: bool = False) -> None:
        raw = (url or "").strip()
        if is_subresource and raw.lower().startswith(("data:", "blob:", "about:")):
            return
        if not raw or _CTRL_OR_WS_RE.search(raw):
            raise ValueError(t("browser_mcp.invalid_url"))

        try:
            parsed = urllib.parse.urlsplit(raw)
        except ValueError as exc:
            raise ValueError(t("browser_mcp.invalid_url")) from exc

        scheme = parsed.scheme.lower()
        if scheme not in ("http", "https"):
            raise ValueError(t("browser_mcp.unsupported_scheme", scheme=scheme or "<empty>"))

        if parsed.username is not None or parsed.password is not None:
            raise ValueError(t("browser_mcp.userinfo_blocked"))

        hostname = (parsed.hostname or "").strip().strip("[]").lower().rstrip(".")
        if not hostname:
            raise ValueError(t("browser_mcp.domain_parse_failed", url=BROWSER_REDACTION.redact(raw)))

        try:
            port = parsed.port or (443 if scheme == "https" else 80)
        except ValueError as exc:
            raise ValueError(t("browser_mcp.invalid_url")) from exc

        cfg = self._get_config()
        explicitly_allowed = bool(cfg.allowed_domains and _matches_allowed_domains(hostname, cfg.allowed_domains))

        ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
        try:
            ip_obj = ipaddress.ip_address(hostname.split("%", 1)[0])
            if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
                ip_obj = ip_obj.ipv4_mapped
        except ValueError:
            ip_obj = None

        if ip_obj is not None:
            if (
                ip_obj.is_link_local
                or ip_obj.is_multicast
                or ip_obj.is_unspecified
                or ip_obj == _METADATA_V6
                or ip_obj == _BROADCAST_V4
            ):
                raise ValueError(t("browser_mcp.hard_blocked_ip", ip=str(ip_obj)))

            if _is_cdp_endpoint_target(hostname, port, cfg.cdp_url):
                raise ValueError(t("browser_mcp.cdp_endpoint_blocked"))

            if ip_obj.is_loopback:
                if not (cfg.allow_loopback or explicitly_allowed):
                    raise ValueError(t("browser_mcp.loopback_blocked", host=hostname))
            elif not ip_obj.is_global:
                if not (cfg.allow_private_networks or explicitly_allowed):
                    raise ValueError(t("browser_mcp.private_ip_blocked", ip=str(ip_obj)))
        else:
            if _is_cdp_endpoint_target(hostname, port, cfg.cdp_url):
                raise ValueError(t("browser_mcp.cdp_endpoint_blocked"))
            if hostname == "localhost" or hostname.endswith(".localhost"):
                if not (cfg.allow_loopback or explicitly_allowed):
                    raise ValueError(t("browser_mcp.loopback_blocked", host=hostname))

        if cfg.allowed_domains and not explicitly_allowed:
            raise ValueError(t("browser_mcp.domain_not_allowed", host=hostname))


def check_url_allowed(url: str) -> None:
    """驗證網址是否符合目前瀏覽器安全政策（維持向後相容公開函式）。"""
    DefaultBrowserUrlPolicy().check_url_allowed(url)


def is_sensitive_field(
    el_type: str,
    el_name: str = "",
    el_id: str = "",
    el_autocomplete: str = "",
) -> bool:
    """判斷表單輸入欄位是否屬於密碼、一次性驗證碼或機密憑證欄位。"""
    if (el_type or "").strip().lower() == "password":
        return True
    for attr in (el_name, el_id, el_autocomplete):
        val = (attr or "").strip()
        if not val:
            continue
        if SENSITIVE_COL_RE.search(val) or _SENSITIVE_FIELD_RE.search(val):
            return True
    return False


def redact_and_truncate(
    text: str,
    *,
    max_chars: int = CONSTANTS.max_output_chars,
    hint: str = "",
) -> tuple[str, bool]:
    """先遮罩機密資訊再安全裁切輸出，確保不截斷 [REDACTED...] 標記。"""
    redacted = BROWSER_REDACTION.redact(text)
    return BROWSER_REDACTION.truncate_output(redacted, max_chars=max_chars, hint=hint)
