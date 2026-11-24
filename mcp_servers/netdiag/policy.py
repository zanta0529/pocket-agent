"""網站與網路診斷安全授權與網段策略（Strategy Pattern：授權先於 DNS）。"""

import ipaddress
import os
import re
from abc import ABC, abstractmethod
from urllib.parse import urlsplit

from mcp_servers._shared import CONSTANTS, t
from mcp_servers.netdiag.models import NetdiagConfig, ProxyInfo, ValidatedTarget

_HOST_LABEL_RE = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$")
_CTRL_OR_WS_RE = re.compile(r"[\x00-\x20\x7f]")

_RFC1918_NETS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)
_ULA_NET = ipaddress.ip_network("fc00::/7")
_METADATA_V6 = ipaddress.ip_address("fd00:ec2::254")
_BROADCAST_V4 = ipaddress.ip_address("255.255.255.255")


class PolicyError(PermissionError):
    """違反網路診斷安全政策時拋出。"""


def _parse_bool_env(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _parse_csv_env(name: str) -> tuple[str, ...]:
    raw = os.getenv(name) or ""
    return tuple(item.strip().lower() for item in raw.split(",") if item.strip())


def _parse_ports_env(name: str, defaults: tuple[int, ...]) -> tuple[int, ...]:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return defaults
    ports: list[int] = []
    for token in raw.split(","):
        tok = token.strip()
        if not tok or not tok.isdigit():
            continue
        p = int(tok)
        if 1 <= p <= 65535 and p not in ports:
            ports.append(p)
    return tuple(ports) if ports else defaults


def load_netdiag_config() -> NetdiagConfig:
    """從環境變數讀取執行期安全設定。"""
    raw_timeout = (os.getenv("NETDIAG_TIMEOUT") or "").strip()
    timeout_val = CONSTANTS.netdiag_default_timeout_sec
    if raw_timeout:
        try:
            parsed_t = float(raw_timeout)
            timeout_val = max(CONSTANTS.netdiag_min_timeout_sec, min(parsed_t, CONSTANTS.netdiag_max_timeout_sec))
        except ValueError:
            pass
    return NetdiagConfig(
        allow_private_networks=_parse_bool_env("NETDIAG_ALLOW_PRIVATE_NETWORKS", False),
        allow_loopback=_parse_bool_env("NETDIAG_ALLOW_LOOPBACK", False),
        allow_proxy=_parse_bool_env("NETDIAG_ALLOW_PROXY", False),
        allowed_cidrs=_parse_csv_env("NETDIAG_ALLOWED_CIDRS"),
        allowed_domains=_parse_csv_env("NETDIAG_ALLOWED_DOMAINS"),
        allowed_ports=_parse_ports_env("NETDIAG_ALLOWED_PORTS", CONSTANTS.netdiag_default_allowed_ports),
        default_timeout_sec=timeout_val,
    )


def _is_valid_hostname(hostname: str) -> bool:
    clean = hostname.rstrip(".")
    if not clean or len(clean) > 253:
        return False
    return all(_HOST_LABEL_RE.match(label) for label in clean.split("."))


def _matches_domain_allowlist(hostname: str, allowlist: tuple[str, ...]) -> bool:
    host = hostname.lower().rstrip(".")
    for entry in allowlist:
        pattern = entry.strip().lower().rstrip(".")
        if not pattern:
            continue
        if pattern.startswith("*."):
            suffix = pattern[1:]  # ".example.com"
            if host.endswith(suffix) and len(host) > len(suffix):
                return True
        elif host == pattern or host.endswith(f".{pattern}"):
            return True
    return False


class NetworkAccessPolicy(ABC):
    """網路存取授權與網段隔離抽象策略。"""

    @abstractmethod
    def validate_target_preflight(self, target: str, *, host_header: str = "") -> ValidatedTarget:
        """在任何 DNS 解析前驗證目標 URL、埠口、host_header 與網域白名單。"""

    @abstractmethod
    def validate_domain_preflight(self, domain: str) -> str:
        """在任何 DNS 解析前驗證純網域名稱。"""

    @abstractmethod
    def validate_ip(self, ip_str: str) -> str:
        """驗證解析後或字面值 IP 是否符合硬封鎖與公私網政策。"""

    @abstractmethod
    def validate_nameservers(self, nameservers_raw: str) -> tuple[str, ...]:
        """解析並驗證自訂 DNS 解析器 IP 清單。"""

    @abstractmethod
    def detect_proxy(self, url: str) -> ProxyInfo:
        """偵測環境代理並執行 fail-closed 協定檢查。"""


class DefaultNetdiagPolicy(NetworkAccessPolicy):
    """預設安全策略：授權先於 DNS、硬封鎖保留網段、公私網分層控管。"""

    def __init__(self, config: NetdiagConfig | None = None) -> None:
        self.config = config or load_netdiag_config()

    def validate_target_preflight(self, target: str, *, host_header: str = "") -> ValidatedTarget:
        raw = (target or "").strip()
        if not raw or _CTRL_OR_WS_RE.search(raw):
            raise PolicyError(t("netdiag_mcp.invalid_url"))

        try:
            parts = urlsplit(raw)
        except ValueError as exc:
            raise PolicyError(t("netdiag_mcp.invalid_url")) from exc

        scheme = parts.scheme.lower()
        if scheme not in ("http", "https"):
            raise PolicyError(t("netdiag_mcp.unsupported_scheme", scheme=scheme or "<empty>"))
        if parts.username is not None or parts.password is not None:
            raise PolicyError(t("netdiag_mcp.userinfo_blocked"))

        hostname = (parts.hostname or "").strip().strip("[]").lower()
        if not hostname:
            raise PolicyError(t("netdiag_mcp.invalid_url"))

        try:
            port = parts.port or (443 if scheme == "https" else 80)
        except ValueError as exc:
            raise PolicyError(t("netdiag_mcp.invalid_port")) from exc

        if port not in self.config.allowed_ports:
            raise PolicyError(t("netdiag_mcp.port_not_allowed", port=port))

        clean_host_header = (host_header or "").strip().rstrip(".")
        if clean_host_header:
            if _CTRL_OR_WS_RE.search(clean_host_header) or not _is_valid_hostname(clean_host_header):
                raise PolicyError(t("netdiag_mcp.invalid_host_header"))
            if self.config.allowed_domains and not _matches_domain_allowlist(
                clean_host_header, self.config.allowed_domains
            ):
                raise PolicyError(t("netdiag_mcp.domain_not_allowed", host=clean_host_header))

        is_ip_literal = False
        scope_label = "domain"
        try:
            ipaddress.ip_address(hostname)
            is_ip_literal = True
        except ValueError:
            is_ip_literal = False

        if is_ip_literal:
            norm_ip = self.validate_ip(hostname)
            scope_label = self._classify_ip_scope(norm_ip)
        else:
            if not _is_valid_hostname(hostname):
                raise PolicyError(t("netdiag_mcp.invalid_url"))
            if (hostname == "localhost" or hostname.endswith(".localhost")) and not self.config.allow_loopback:
                raise PolicyError(t("netdiag_mcp.loopback_blocked", ip=hostname))
            if self.config.allowed_domains and not _matches_domain_allowlist(hostname, self.config.allowed_domains):
                raise PolicyError(t("netdiag_mcp.domain_not_allowed", host=hostname))

        return ValidatedTarget(
            url=raw,
            scheme=scheme,
            hostname=hostname,
            port=port,
            is_ip_literal=is_ip_literal,
            scope_label=scope_label,
            host_header=clean_host_header,
        )

    def validate_domain_preflight(self, domain: str) -> str:
        clean = (domain or "").strip().lower().rstrip(".")
        if not clean or _CTRL_OR_WS_RE.search(clean) or clean.startswith("-"):
            raise PolicyError(t("netdiag_mcp.invalid_url"))
        try:
            ipaddress.ip_address(clean.strip("[]"))
        except ValueError:
            pass
        else:
            raise PolicyError(t("netdiag_mcp.invalid_url"))
        if not _is_valid_hostname(clean):
            raise PolicyError(t("netdiag_mcp.invalid_url"))
        if (clean == "localhost" or clean.endswith(".localhost")) and not self.config.allow_loopback:
            raise PolicyError(t("netdiag_mcp.loopback_blocked", ip=clean))
        if self.config.allowed_domains and not _matches_domain_allowlist(clean, self.config.allowed_domains):
            raise PolicyError(t("netdiag_mcp.domain_not_allowed", host=clean))
        return clean

    def validate_ip(self, ip_str: str) -> str:
        clean = ip_str.strip().strip("[]").split("%", 1)[0]
        ip = ipaddress.ip_address(clean)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped

        if ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip == _METADATA_V6 or ip == _BROADCAST_V4:
            raise PolicyError(t("netdiag_mcp.hard_blocked_ip", ip=str(ip)))

        if ip.is_loopback:
            if not self.config.allow_loopback:
                raise PolicyError(t("netdiag_mcp.loopback_blocked", ip=str(ip)))
            return str(ip)

        if ip.is_global:
            return str(ip)

        if not self.config.allow_private_networks:
            raise PolicyError(t("netdiag_mcp.private_ip_blocked", ip=str(ip)))

        if self.config.allowed_cidrs:
            for cidr_raw in self.config.allowed_cidrs:
                try:
                    net = ipaddress.ip_network(cidr_raw, strict=False)
                except ValueError:
                    continue
                if ip.version == net.version and ip in net:
                    return str(ip)
            raise PolicyError(t("netdiag_mcp.cidr_not_allowed", ip=str(ip)))

        is_rfc1918 = isinstance(ip, ipaddress.IPv4Address) and any(ip in net for net in _RFC1918_NETS)
        is_ula = isinstance(ip, ipaddress.IPv6Address) and ip in _ULA_NET
        if is_rfc1918 or is_ula:
            return str(ip)
        raise PolicyError(t("netdiag_mcp.hard_blocked_ip", ip=str(ip)))

    def validate_nameservers(self, nameservers_raw: str) -> tuple[str, ...]:
        raw = (nameservers_raw or "").strip()
        if not raw:
            return ()
        items = [s.strip() for s in raw.split(",") if s.strip()]
        if len(items) > CONSTANTS.netdiag_dns_max_nameservers:
            raise PolicyError(t("netdiag_mcp.too_many_nameservers", max_count=CONSTANTS.netdiag_dns_max_nameservers))
        validated: list[str] = []
        for item in items:
            try:
                norm_ip = self.validate_ip(item)
            except ValueError as exc:
                raise PolicyError(t("netdiag_mcp.invalid_nameservers", nameserver=item)) from exc
            if norm_ip not in validated:
                validated.append(norm_ip)
        return tuple(validated)

    def detect_proxy(self, url: str) -> ProxyInfo:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        hostname = (parts.hostname or "").lower()
        no_proxy = (os.getenv("NO_PROXY") or os.getenv("no_proxy") or "").strip()
        if no_proxy and self._matches_no_proxy(hostname, no_proxy):
            return ProxyInfo(used=False)

        candidates = (
            ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")
            if scheme == "https"
            else ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
        )
        proxy_url = ""
        for env_key in candidates:
            val = (os.getenv(env_key) or "").strip()
            if val:
                proxy_url = val
                break
        if not proxy_url:
            return ProxyInfo(used=False)

        p_parts = urlsplit(proxy_url)
        p_scheme = p_parts.scheme.lower()
        if p_scheme not in ("http", "https"):
            raise PolicyError(t("netdiag_mcp.unsupported_proxy", scheme=p_scheme or "<empty>"))
        if not self.config.allow_proxy:
            raise PolicyError(t("netdiag_mcp.proxy_blocked_by_policy"))
        p_host = p_parts.hostname or ""
        p_port = p_parts.port or (443 if p_scheme == "https" else 80)
        return ProxyInfo(used=True, display_proxy=f"{p_scheme}://{p_host}:{p_port}", raw_url=proxy_url)

    @staticmethod
    def _matches_no_proxy(hostname: str, no_proxy: str) -> bool:
        for entry in no_proxy.split(","):
            rule = entry.strip().lower().lstrip(".")
            if not rule:
                continue
            if rule == "*" or hostname == rule or hostname.endswith(f".{rule}"):
                return True
        return False

    @staticmethod
    def _classify_ip_scope(ip_str: str) -> str:
        ip = ipaddress.ip_address(ip_str)
        if ip.is_loopback:
            return "loopback"
        if not ip.is_global:
            return "private"
        return "public"
