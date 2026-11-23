"""跨 MCP 伺服器共用參數清理與輸入驗證輔助模組。"""

import ipaddress
import re

from mcp_servers._shared.bootstrap import t

_IDENT_RE = re.compile(r"^[a-zA-Z0-9_.@$\-]+$")
_HOST_RE = re.compile(r"^[a-zA-Z0-9_.\-]+$")
_HOST_WITH_INSTANCE_RE = re.compile(r"^[a-zA-Z0-9_.\-\\]+$")


def is_empty_or_null(val: str | None) -> bool:
    """檢查字串是否為空值、空白或 YAML/JSON 常見無效預留值（null / none / ~）。"""
    if val is None:
        return True
    cleaned = str(val).strip()
    return not cleaned or cleaned.lower() in ("null", "none", "~")


def clean_param(val: str | None) -> str:
    """清理並移除外層引號；若為空或無效預留值則回傳空字串。"""
    if is_empty_or_null(val):
        return ""
    return str(val).strip().strip("'\"")


def validate_port(value: str, env_name: str = "PORT") -> str:
    """驗證連接埠為 1~65535 整數並正規化為十進位字串。"""
    clean = value.strip()
    if not clean.isdigit() or not (1 <= int(clean) <= 65535):
        raise ValueError(t("mcp_shared.invalid_port", env_name=env_name))
    return str(int(clean))


def validate_identifier(
    value: str,
    field_name: str,
    *,
    pattern: re.Pattern[str] = _IDENT_RE,
) -> str:
    """驗證識別字格式且禁止以連字號開頭（防範 CLI 參數注入）。"""
    clean = value.strip()
    if not clean or clean.startswith("-") or not pattern.match(clean):
        raise ValueError(t("mcp_shared.invalid_identifier", field_name=field_name))
    return clean


def validate_host(
    value: str,
    field_name: str = "HOST",
    *,
    bracket_ipv6: bool = False,
    allow_backslash: bool = False,
) -> str:
    """驗證 IPv4、IPv6 或主機名稱（可選允許 SQL Server host\\instance 格式）。"""
    clean = value.strip()
    if not clean or clean.startswith("-"):
        raise ValueError(t("mcp_shared.invalid_host", field_name=field_name))
    raw_ip = clean.strip("[]")
    try:
        ip = ipaddress.ip_address(raw_ip)
        if ip.version == 6 and bracket_ipv6:
            return f"[{ip}]"
        return str(ip)
    except ValueError:
        pass
    host_re = _HOST_WITH_INSTANCE_RE if allow_backslash else _HOST_RE
    if not host_re.match(clean):
        raise ValueError(t("mcp_shared.invalid_host", field_name=field_name))
    return clean


def validate_positive_int(
    value: int,
    field_name: str,
    *,
    max_val: int | None = None,
) -> int:
    """驗證正整數參數（明確排除 bool 子類別）。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(t("mcp_shared.invalid_positive_int", field_name=field_name, value=repr(value)))
    if max_val is not None and value > max_val:
        raise ValueError(
            t(
                "mcp_shared.invalid_positive_int_range",
                field_name=field_name,
                value=repr(value),
                max_val=max_val,
            )
        )
    return value
