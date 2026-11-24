"""MCP 機密儲存轉接器（Adapter Pattern）與保留註解之原子 YAML 修補器。"""

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml

from pocket_agent.system.constants import (
    SECRET_PLACEHOLDER_ENCRYPTED,
    SECRET_PLACEHOLDER_KEYRING,
)
from pocket_agent.system.paths import mcp_example_path

_SERVER_KEY_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9_-]+")
_SERVER_KEY_MULTI_US_RE = re.compile(r"_+")
_YAML_KV_RE = re.compile(r"^([ \t]*)(.+?):(.*)$")


def normalize_secret_placeholder(value: str | None) -> str | None:
    """正規化並辨識憑證佔位符號（USE_KEYRING / USE_ENCRYPTED）。"""
    cleaned = (value or "").strip()
    if cleaned == SECRET_PLACEHOLDER_KEYRING:
        return SECRET_PLACEHOLDER_KEYRING
    if cleaned == SECRET_PLACEHOLDER_ENCRYPTED:
        return SECRET_PLACEHOLDER_ENCRYPTED
    return None


def is_secret_placeholder(value: str | None) -> bool:
    """判斷字串是否為系統憑證佔位符號。"""
    return normalize_secret_placeholder(value) is not None


def build_mcp_secret_key(server_name: str, env_key: str) -> str:
    """產生正規化且符合 Keyring 篩選條件的小寫憑證鍵名。"""
    cleaned = _SERVER_KEY_SANITIZE_RE.sub("_", server_name.strip())
    cleaned = _SERVER_KEY_MULTI_US_RE.sub("_", cleaned).strip("_").lower() or "mcp_server"
    return f"mcp.{cleaned}.{env_key.strip().lower()}"


@dataclass(frozen=True)
class ConnPromptResult:
    """MCP 連線參數互動詢問結果，支援指定需持久化至 Keyring／JWE 的鍵名集合。"""

    values: dict[str, str]
    save_to_keyring: frozenset[str] = frozenset()


@dataclass(frozen=True)
class SecretPersistResult:
    """MCP 憑證持久化與設定檔回寫結果，供 UI 層呈現回饋訊息。"""

    server_name: str
    env_key: str
    saved: bool
    placeholder: str | None = None
    yaml_updated: bool = False
    reason: str = ""


class MCPSecretStore(Protocol):
    """MCP 憑證儲存後端抽象介面（便於依賴注入與單元測試隔離）。"""

    def get(self, server_name: str, env_key: str, placeholder: str) -> str | None: ...

    def set(self, server_name: str, env_key: str, value: str) -> str: ...

    def delete(self, server_name: str, env_key: str) -> bool: ...

    def unlock(self, password: str) -> bool: ...

    def create_vault_and_set(self, server_name: str, env_key: str, value: str, password: str) -> str: ...


class ConfigManagerSecretStore:
    """將 MCPSecretStore 轉接至系統 ConfigManager（Keyring 優先＋JWE 加密庫降級）。"""

    def __init__(self, config_manager: Any | None = None) -> None:
        self._config_manager = config_manager

    @property
    def _cfg(self) -> Any:
        if self._config_manager is None:
            from pocket_agent.system.config import config

            self._config_manager = config
        return self._config_manager

    def get(self, server_name: str, env_key: str, placeholder: str) -> str | None:
        secret_key = build_mcp_secret_key(server_name, env_key)
        return self._cfg.get_secret(secret_key, placeholder=placeholder)

    def set(self, server_name: str, env_key: str, value: str) -> str:
        secret_key = build_mcp_secret_key(server_name, env_key)
        return self._cfg.set_secret(secret_key, value)

    def delete(self, server_name: str, env_key: str) -> bool:
        secret_key = build_mcp_secret_key(server_name, env_key)
        return self._cfg.delete_secret(secret_key)

    def unlock(self, password: str) -> bool:
        return bool(self._cfg.unlock(password))

    def create_vault_and_set(self, server_name: str, env_key: str, value: str, password: str) -> str:
        secret_key = build_mcp_secret_key(server_name, env_key)
        self._cfg.encrypted_provider.set(secret_key, str(value))
        self._cfg.encrypted_provider.save_vault(password)
        return SECRET_PLACEHOLDER_ENCRYPTED


def _unquote_yaml_key(raw_key: str) -> str | None:
    s = raw_key.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    if not s or any(ch in s for ch in (" ", "\t", "{", "}", "[", "]", "#")):
        return None
    return s


def _split_yaml_value_and_comment(raw_rest: str) -> tuple[str, str] | None:
    """切分 YAML 值區段與行尾註解，尊重單雙引號邊界。"""
    s = raw_rest.lstrip(" ")
    if not s:
        return "", ""
    if s[0] == "#":
        return "", s
    if s[0] in ("|", ">", "{", "[", "&", "*"):
        return None

    in_single = False
    in_double = False
    i = 0
    while i < len(s):
        ch = s[i]
        if in_single:
            if ch == "'":
                if i + 1 < len(s) and s[i + 1] == "'":
                    i += 2
                    continue
                in_single = False
        elif in_double:
            if ch == "\\" and i + 1 < len(s):
                i += 2
                continue
            if ch == '"':
                in_double = False
        else:
            if ch == "'":
                in_single = True
            elif ch == '"':
                in_double = True
            elif ch == "#" and (i == 0 or s[i - 1] in (" ", "\t")):
                return s[:i].rstrip(" "), s[i:]
        i += 1

    if in_single or in_double:
        return None
    return s.rstrip(" "), ""


def _format_yaml_scalar(new_value: str, original_val: str) -> str:
    stripped = original_val.strip()
    if stripped.startswith("'") and "'" not in new_value:
        return f"'{new_value}'"
    escaped = new_value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _parse_kv_line(line: str) -> tuple[int, str, str] | None:
    """解析單行 YAML 鍵值，回傳 (縮排空白數, 鍵名, 冒號後內容)；忽略註解與空行。"""
    stripped = line.lstrip(" \t")
    if not stripped or stripped.startswith("#"):
        return None
    if "\t" in line[: len(line) - len(stripped)]:
        return None
    m = _YAML_KV_RE.match(line)
    if not m:
        return None
    indent_str, raw_key, rest = m.group(1), m.group(2), m.group(3)
    if stripped.startswith("-"):
        return None
    key = _unquote_yaml_key(raw_key)
    if key is None:
        return None
    return len(indent_str), key, rest


def _find_block_end(lines: list[str], start_idx: int, parent_indent: int) -> tuple[int, int | None]:
    """尋找區塊結束行索引與最後一個有效子行索引（用於安全插入新鍵）。"""
    last_content_idx: int | None = None
    idx = start_idx + 1
    while idx < len(lines):
        line = lines[idx]
        stripped = line.lstrip(" \t")
        if not stripped or stripped.startswith("#"):
            idx += 1
            continue
        if "\t" in line[: len(line) - len(stripped)]:
            return idx, last_content_idx
        indent = len(line) - len(stripped)
        if indent <= parent_indent:
            return idx, last_content_idx
        last_content_idx = idx
        idx += 1
    return idx, last_content_idx


def _find_child_key(
    lines: list[str],
    block_start: int,
    block_end: int,
    parent_indent: int,
    target_key: str,
) -> tuple[int, int, str] | None:
    """在父區塊內尋找指定直接子鍵，回傳 (行索引, 子縮排, 冒號後內容)。"""
    child_indent: int | None = None
    for idx in range(block_start + 1, block_end):
        parsed = _parse_kv_line(lines[idx])
        if parsed is None:
            continue
        indent, key, rest = parsed
        if indent <= parent_indent:
            break
        if child_indent is None:
            child_indent = indent
        if indent == child_indent and key == target_key:
            return idx, indent, rest
    return None


def update_mcp_config_env_value(
    config_path: Path,
    server_name: str,
    env_key: str,
    new_value: str,
) -> bool:
    """以保留註解、縮排與換行符之方式，原子更新 mcp_config.yaml 中指定 env 鍵值。"""
    if not config_path.is_file():
        return False
    try:
        if config_path.resolve() == mcp_example_path().resolve():
            return False
    except OSError:
        pass

    try:
        with open(config_path, encoding="utf-8", newline="") as fh:
            raw_text = fh.read()
    except OSError:
        return False

    try:
        parsed_doc = yaml.safe_load(raw_text)
    except yaml.YAMLError:
        return False
    if not isinstance(parsed_doc, dict):
        return False
    servers_obj = parsed_doc.get("servers")
    if not isinstance(servers_obj, dict) or not isinstance(servers_obj.get(server_name), dict):
        return False

    eol = "\r\n" if "\r\n" in raw_text else "\n"
    has_trailing_eol = raw_text.endswith(("\r\n", "\n"))
    lines = raw_text.splitlines()

    servers_loc = _find_child_key(lines, -1, len(lines), -1, "servers")
    if servers_loc is None:
        return False
    servers_idx, servers_indent, servers_rest = servers_loc
    if _split_yaml_value_and_comment(servers_rest) is None:
        return False
    servers_end, _ = _find_block_end(lines, servers_idx, servers_indent)

    srv_loc = _find_child_key(lines, servers_idx, servers_end, servers_indent, server_name)
    if srv_loc is None:
        return False
    srv_idx, srv_indent, srv_rest = srv_loc
    if _split_yaml_value_and_comment(srv_rest) is None:
        return False
    srv_end, srv_last_idx = _find_block_end(lines, srv_idx, srv_indent)

    env_loc = _find_child_key(lines, srv_idx, srv_end, srv_indent, "env")
    if env_loc is None:
        child_indent = srv_indent + 2
        for probe in range(srv_idx + 1, srv_end):
            p = _parse_kv_line(lines[probe])
            if p is not None and p[0] > srv_indent:
                child_indent = p[0]
                break
        env_indent_str = " " * child_indent
        key_indent_str = " " * (child_indent + 2)
        insert_pos = (srv_last_idx + 1) if srv_last_idx is not None else (srv_idx + 1)
        lines.insert(insert_pos, f"{env_indent_str}env:")
        lines.insert(insert_pos + 1, f"{key_indent_str}{env_key}: {_format_yaml_scalar(new_value, '')}")
    else:
        env_idx, env_indent, env_rest = env_loc
        env_split = _split_yaml_value_and_comment(env_rest)
        if env_split is None:
            return False
        env_val, env_comment = env_split
        if env_val and env_val.lower() not in ("null", "~"):
            return False

        env_end, env_last_idx = _find_block_end(lines, env_idx, env_indent)
        key_loc = _find_child_key(lines, env_idx, env_end, env_indent, env_key)
        if key_loc is not None:
            key_idx, key_indent, key_rest = key_loc
            key_split = _split_yaml_value_and_comment(key_rest)
            if key_split is None:
                return False
            orig_val, orig_comment = key_split
            formatted = _format_yaml_scalar(new_value, orig_val)
            comment_suffix = f" {orig_comment}" if orig_comment else ""
            raw_key_part = lines[key_idx].split(":", 1)[0]
            lines[key_idx] = f"{raw_key_part}: {formatted}{comment_suffix}"
        else:
            if env_val:
                comment_suffix = f" {env_comment}" if env_comment else ""
                raw_env_part = lines[env_idx].split(":", 1)[0]
                lines[env_idx] = f"{raw_env_part}:{comment_suffix}"
            child_indent = env_indent + 2
            for probe in range(env_idx + 1, env_end):
                p = _parse_kv_line(lines[probe])
                if p is not None and p[0] > env_indent:
                    child_indent = p[0]
                    break
            insert_pos = (env_last_idx + 1) if env_last_idx is not None else (env_idx + 1)
            lines.insert(insert_pos, f"{' ' * child_indent}{env_key}: {_format_yaml_scalar(new_value, '')}")

    updated_text = eol.join(lines) + (eol if has_trailing_eol else "")
    try:
        validated = yaml.safe_load(updated_text)
    except yaml.YAMLError:
        return False
    try:
        actual = validated["servers"][server_name]["env"][env_key]
    except TypeError, KeyError:
        return False
    if actual != new_value:
        return False

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=config_path.parent,
            prefix=".mcp_config_",
            suffix=".tmp",
            delete=False,
        ) as tmp_fh:
            tmp_path = tmp_fh.name
            tmp_fh.write(updated_text)
        os.replace(tmp_path, config_path)
        tmp_path = None
        return True
    except OSError:
        return False
    finally:
        if tmp_path is not None:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except OSError:
                pass
