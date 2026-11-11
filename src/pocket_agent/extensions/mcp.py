"""管理多個 MCP server 連線，並把所有 tools 合併成 OpenAI Responses API 格式。

支援 inject 參數自動注入：
  在 mcp_config.yaml 的 server 底下加 inject 區段，
  指定的參數會自動帶入工具呼叫，並從送給模型的 schema 中移除。
  模型不需要知道這些參數的存在。
"""

import asyncio
import copy
import logging
import os
import re
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from pocket_agent.core.cancellation import run_sync
from pocket_agent.system.paths import bundle_dir, is_frozen, log_dir, mcp_config_path, mcp_example_path

try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

try:
    from mcp.client.sse import sse_client

    HAS_SSE = True
except ImportError:
    HAS_SSE = False

try:
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    HAS_STREAMABLE_HTTP = True
except ImportError:
    HAS_STREAMABLE_HTTP = False

try:
    from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider
    from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

    HAS_MCP_OAUTH = True
except ImportError:
    HAS_MCP_OAUTH = False


_CONN_PARAM_MAP = {
    "ssh_host": "RHEL_HOST",
    "ssh_port": "RHEL_SSH_PORT",
    "ssh_user": "RHEL_SSH_USER",
    "ssh_password": "RHEL_SSH_PASSWORD",
    "ssh_key_path": "RHEL_SSH_KEY_PATH",
    "rdp_host": "WIN_HOST",
    "rdp_port": "WIN_RDP_PORT",
    "rdp_user": "WIN_RDP_USER",
    "rdp_password": "WIN_RDP_PASSWORD",
    "mysql_host": "MYSQL_HOST",
    "mysql_port": "MYSQL_PORT",
    "mysql_user": "MYSQL_USER",
    "mysql_password": "MYSQL_PASSWORD",
    "mysql_database": "MYSQL_DATABASE",
    "mssql_host": "MSSQL_HOST",
    "mssql_port": "MSSQL_PORT",
    "mssql_user": "MSSQL_USER",
    "mssql_password": "MSSQL_PASSWORD",
    "mssql_database": "MSSQL_DATABASE",
    "db_path": "DB_PATH",
    "github_token": "GITHUB_TOKEN",
}

# 切換 SQLite 資料庫的工具；成功後需同步 _runtime_conn_cache 的 DB_PATH
_DB_SWITCH_TOOLS = frozenset({"sqlite_switch_database", "db_switch_database"})


@dataclass
class ToolEntry:
    name: str
    server_name: str
    session: ClientSession
    schema: dict  # 送給模型的 schema（已移除 inject 參數）
    inject: dict = field(default_factory=dict)  # 自動注入的參數值
    conn_params: dict[str, str] = field(default_factory=dict)
    is_sensitive: bool = False


@dataclass
class _ServerRunner:
    name: str
    task: asyncio.Task
    stop_event: asyncio.Event
    session: ClientSession | None = None


@dataclass(frozen=True)
class MCPStartupResult:
    """本次初始化結果；ready 表示連線與工具註冊皆成功。"""

    configured_servers: int
    ready_servers: int
    failed_servers: int
    tool_count: int


class MCPManager:
    """載入 mcp_config.yaml，建立所有 server 連線，提供統一工具查詢與執行介面。"""

    def __init__(self, config_path: str | Path | None = None):
        self.config_path = Path(config_path) if config_path else mcp_config_path()
        self._tools: dict[str, ToolEntry] = {}
        self._sessions: list[ClientSession] = []
        self._runners: dict[str, _ServerRunner] = {}
        self._exit_stack_closers: list[Any] = []
        self._server_envs: dict[str, dict[str, str]] = {}
        self._runtime_conn_cache: dict[str, dict[str, str]] = {}
        self.on_prompt_conn: Callable[[str, list[str]], dict[str, str]] | None = None

    # ── Lifecycle ─────────────────────────────────

    async def start(self, *, on_start: Callable[[int], None] | None = None) -> MCPStartupResult:
        config = self._load_config()
        servers = {name: spec for name, spec in (config.get("servers") or {}).items() if spec is not None}
        if not servers:
            return MCPStartupResult(0, 0, 0, 0)
        if on_start is not None:
            on_start(len(servers))
        ready_servers = 0
        failed_servers = 0
        for name, spec in servers.items():
            try:
                raw_env = spec.get("env") or {}
                self._server_envs[name] = {str(k): str(v) for k, v in raw_env.items() if v is not None}
                session = await self._connect(name, spec)
                self._sessions.append(session)
                inject = spec.get("inject") or {}
                safe_read = spec.get("safe_read_tools") or []
                is_sensitive = bool(spec.get("is_sensitive", False))
                cmd_str = str(spec.get("command", "")).lower()
                args_str = " ".join(str(a).lower() for a in (spec.get("args") or []))
                if (
                    name.lower() in ("rhel-ops", "rhel", "win-rdp", "win_rdp", "mysql", "mssql")
                    or "rhel_mcp" in cmd_str
                    or "rhel_mcp" in args_str
                    or "mysql_mcp" in cmd_str
                    or "mysql_mcp" in args_str
                    or "mssql_mcp" in cmd_str
                    or "mssql_mcp" in args_str
                ):
                    is_sensitive = True
                await self._register_tools(name, session, inject, safe_read, is_sensitive=is_sensitive)
                ready_servers += 1
            except Exception as e:
                failed_servers += 1
                print(f"[MCP] ⚠ 無法連線 {name!r}: {e}")
        return MCPStartupResult(len(servers), ready_servers, failed_servers, len(self._tools))

    async def stop(self, timeout: float = 3.0) -> None:
        runners = list(self._runners.values())
        self._runners.clear()
        self._sessions.clear()
        self._tools.clear()

        closers = list(reversed(self._exit_stack_closers))
        self._exit_stack_closers.clear()

        async def _close_runner(r: _ServerRunner) -> None:
            r.stop_event.set()
            try:
                await asyncio.wait_for(r.task, timeout=1.5)
            except asyncio.TimeoutError, asyncio.CancelledError:
                r.task.cancel()
                try:
                    await asyncio.wait_for(r.task, timeout=0.5)
                except Exception:
                    pass
            except Exception as e:
                logging.getLogger("pocket-agent").warning("MCP runner failed for %s: %s", r.name, e)

        async def _close_legacy(closer: Any) -> None:
            try:
                await asyncio.wait_for(closer(None, None, None), timeout=1.0)
            except Exception as e:
                logging.getLogger("pocket-agent").warning("MCP legacy closer failed: %s", e)

        close_coros = [_close_runner(r) for r in runners] + [_close_legacy(c) for c in closers]
        if close_coros:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*close_coros, return_exceptions=True),
                    timeout=timeout,
                )
            except (asyncio.TimeoutError, Exception) as e:
                logging.getLogger("pocket-agent").warning("MCP 服務整體關閉逾時或失敗: %s", e)

    # ── Tool access ───────────────────────────────

    def openai_tools(self) -> list[dict]:
        """回傳送給模型的 tools 陣列（已隱藏 inject 參數）。"""
        return [entry.schema for entry in self._tools.values()]

    async def _resolve_entry_ssh_conn(self, entry: ToolEntry) -> dict[str, str]:
        server_env = self._server_envs.get(entry.server_name, {})
        cached = self._runtime_conn_cache.setdefault(entry.server_name, {})
        resolved: dict[str, str] = {}
        missing: list[str] = []

        # 預先解析已配置或快取的連線參數
        for env_key in entry.conn_params.values():
            val = cached.get(env_key)
            if val is None or val == "":
                val = server_env.get(env_key)
            if val is None or val == "":
                val = os.getenv(env_key) or ""
            clean = str(val) if "PASSWORD" in env_key or "PASSPHRASE" in env_key else str(val).strip()
            if clean and clean.lower() not in ("null", "none", "~"):
                resolved[env_key] = clean

        # 判斷是否已配置金鑰路徑
        has_key = bool(resolved.get("RHEL_SSH_KEY_PATH"))

        # 判定哪些必要參數缺失需要動態向使用者詢問
        for env_key in entry.conn_params.values():
            if env_key in resolved:
                continue
            # 金鑰路徑、Passphrase 與選填資料庫名稱均為選填，絕不主動視為 missing 攔截詢問
            if env_key in (
                "RHEL_SSH_KEY_PATH",
                "RHEL_SSH_KEY_PASSPHRASE",
                "MYSQL_DATABASE",
                "MSSQL_DATABASE",
            ):
                continue
            # 若已提供金鑰路徑，密碼（作為 Passphrase）亦為選填，不強制詢問
            if has_key and env_key == "RHEL_SSH_PASSWORD":
                continue
            missing.append(env_key)

        if missing and self.on_prompt_conn is not None:
            prompted = await run_sync(self.on_prompt_conn, entry.server_name, missing)
            for k, v in (prompted or {}).items():
                clean_v = str(v) if "PASSWORD" in k or "PASSPHRASE" in k else str(v).strip()
                if clean_v:
                    cached[k] = clean_v
                    resolved[k] = clean_v

        return {arg_name: resolved.get(env_key, "") for arg_name, env_key in entry.conn_params.items()}

    async def call(self, tool_name: str, arguments: dict) -> str:
        """執行指定工具，自動補入 inject 參數後呼叫。"""
        entry = self._tools.get(tool_name)
        if entry is None:
            raise KeyError(f"找不到工具 {tool_name!r}，可用：{list(self._tools)}")

        # 注入參數（模型看不到，但 server 需要）
        full_args = {**arguments, **entry.inject}
        if entry.conn_params:
            conn_args = await self._resolve_entry_ssh_conn(entry)
            full_args.update(conn_args)

        from pocket_agent.core.agent import log

        # Artifact 隔離攔截
        if tool_name in ("write_to_file", "write_file", "create_file") and "ArtifactMetadata" in full_args:
            artifact_meta = full_args.pop("ArtifactMetadata")
            if artifact_meta.get("UserFacing"):
                from pocket_agent.system.paths import user_dir

                target_key = "TargetFile" if "TargetFile" in full_args else "path"
                if target_key in full_args:
                    original_path = Path(full_args[target_key])
                    artifacts_dir = user_dir() / "artifacts"
                    artifacts_dir.mkdir(parents=True, exist_ok=True)
                    new_path = artifacts_dir / original_path.name
                    full_args[target_key] = str(new_path)
                    log.info(f"Artifact redirected from {original_path} to {new_path}")

        # 工具內容可能包含未知機密；所有 MCP 呼叫統一只記錄中繼資料。
        log.debug("MCP call_tool name=%r", tool_name)
        result = await entry.session.call_tool(tool_name, full_args)
        log.debug("MCP result name=%r blocks=%d", tool_name, len(result.content))

        parts = []
        for block in result.content:
            if hasattr(block, "text"):
                parts.append(block.text)
            else:
                parts.append(str(block))
        from pocket_agent.tools.fs import wrap_external_content

        joined = "\n".join(parts)
        if entry.conn_params and _is_conn_auth_error(joined):
            self._runtime_conn_cache.pop(entry.server_name, None)
        if tool_name in _DB_SWITCH_TOOLS and joined.lstrip().startswith("[SUCCESS]"):
            target = str(arguments.get("target_db_path") or "").strip().strip("'\"")
            if target:
                # 伺服器端已拒絕 UNC 路徑，成功即代表為本機路徑，可安全正規化為絕對路徑
                try:
                    target = str(Path(target).expanduser().resolve())
                except OSError, RuntimeError:
                    pass
                self.set_runtime_conn_param(entry.server_name, "DB_PATH", target)
        return wrap_external_content(joined)

    def set_runtime_conn_param(self, server_name: str, env_key: str, value: str) -> None:
        """更新指定伺服器的執行期連線參數快取（僅存於記憶體，不落盤）。"""
        self._runtime_conn_cache.setdefault(server_name, {})[env_key] = value

    def get_runtime_conn_param(self, server_name: str, env_key: str) -> str | None:
        """取得目前生效的連線參數：執行期快取 → mcp_config.yaml env → 環境變數。"""
        for source in (self._runtime_conn_cache.get(server_name, {}), self._server_envs.get(server_name, {})):
            val = str(source.get(env_key) or "").strip()
            if val and val.lower() not in ("null", "none", "~"):
                return val
        env_val = (os.getenv(env_key) or "").strip()
        return env_val or None

    def find_server_for_tool(self, tool_name: str) -> str | None:
        """回傳提供指定工具的 MCP 伺服器名稱；找不到時回傳 None。"""
        entry = self._tools.get(tool_name)
        return entry.server_name if entry is not None else None

    def get_grouped_tools(self) -> dict[str, list[dict[str, str]]]:
        """回傳依 MCP 伺服器分組的工具資訊清單。"""
        grouped: dict[str, list[dict[str, str]]] = {}
        for entry in self._tools.values():
            grouped.setdefault(entry.server_name, []).append(
                {
                    "name": entry.name,
                    "description": (entry.schema.get("description") or "").strip(),
                }
            )
        return grouped

    def list_tools(self) -> list[str]:
        return list(self._tools.keys())

    def is_sensitive_tool(self, tool_name: str) -> bool:
        """依據本機受信任伺服器註冊中繼資料，判定指定工具是否屬於高敏感系統工具。"""
        entry = self._tools.get(tool_name)
        if entry is not None:
            return bool(entry.is_sensitive)
        return (
            tool_name.startswith("rhel_")
            or tool_name.startswith("win_rdp_")
            or tool_name.startswith("mysql_")
            or tool_name.startswith("mssql_")
        )

    # ── Internal ──────────────────────────────────

    def _load_config(self) -> dict:
        # mcp_config.yaml 在 .gitignore 裡（含個人 server 設定）
        # 找不到時 fallback 到 example 檔（只有範例，servers 區段為空）
        for path in (self.config_path, mcp_example_path()):
            try:
                with open(path, encoding="utf-8") as f:
                    return yaml.safe_load(f) or {}
            except FileNotFoundError:
                continue
        return {}

    def _resolve_stdio_params(self, spec: dict) -> StdioServerParameters:
        raw_cmd = str(spec["command"]).strip()
        is_py_cmd = raw_cmd.lower() in ("python", "python3", "python.exe")

        resolved_args: list[str] = []
        for arg in spec.get("args") or []:
            arg_str = str(arg)
            if not arg_str.startswith("-") and "://" not in arg_str:
                p = Path(arg_str)
                if not p.is_absolute() and not (Path.cwd() / p).exists():
                    for base in (bundle_dir(), self.config_path.parent):
                        candidate = base / p
                        if candidate.exists():
                            arg_str = str(candidate.resolve())
                            break
            resolved_args.append(arg_str)

        if is_py_cmd:
            if is_frozen():
                command = sys.executable
                resolved_args = ["--internal-mcp-server", *resolved_args]
            else:
                exe_path = Path(sys.executable)
                if exe_path.name.lower() == "pythonw.exe":
                    console_py = exe_path.with_name("python.exe")
                    if console_py.exists():
                        exe_path = console_py
                command = str(exe_path)
        else:
            command = raw_cmd

        raw_env = spec.get("env")
        env_dict: dict[str, str] | None = None
        if raw_env or is_py_cmd:
            env_dict = dict(os.environ)
            env_dict.setdefault("PYTHONUTF8", "1")
            env_dict.setdefault("PYTHONIOENCODING", "utf-8")
            env_dict.setdefault("POCKET_AGENT_LOG_FILE", str(log_dir() / "agent.log"))
            if isinstance(raw_env, dict):
                for k, v in raw_env.items():
                    if v is not None:
                        env_dict[str(k)] = str(v)

        return StdioServerParameters(command=command, args=resolved_args, env=env_dict)

    def _open_stdio_errlog(self) -> Any:
        """將 stdio 子行程的 stderr 導向日誌檔案，避免 Windows CREATE_NO_WINDOW 下主控台控制代碼失效。"""
        try:
            err_path = log_dir() / "mcp_stdio.log"
            err_path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(err_path, "a", encoding="utf-8")

            async def _close_errlog(*_args: Any) -> None:
                try:
                    fh.close()
                except Exception:
                    pass

            self._exit_stack_closers.append(_close_errlog)
            return fh
        except Exception:
            return sys.stderr

    def _resolve_http_headers(self, name: str, spec: dict) -> dict[str, str] | None:
        """解析遠端 MCP 伺服器的自訂 HTTP Headers 與 Bearer Token。"""
        server_env = self._server_envs.get(name, {})
        headers: dict[str, str] = {}
        raw_headers = spec.get("headers")
        if isinstance(raw_headers, dict):
            for k, v in raw_headers.items():
                if v is None:
                    continue
                val_str = str(v).strip()
                missing_var = False

                def _replace_var(match: re.Match[str]) -> str:
                    nonlocal missing_var
                    env_key = match.group(1).strip()
                    resolved = (server_env.get(env_key) or os.getenv(env_key) or "").strip()
                    if not resolved:
                        missing_var = True
                    return resolved

                val_str = re.sub(r"\$\{([^}]+)\}", _replace_var, val_str).strip()
                if val_str and not missing_var:
                    headers[str(k)] = val_str

        has_auth_header = any(k.lower() == "authorization" for k in headers)
        if not has_auth_header:
            token = (
                spec.get("bearer_token") or server_env.get("BEARER_TOKEN") or server_env.get("MCP_BEARER_TOKEN") or ""
            )
            token_str = str(token).strip()
            if token_str:
                headers["Authorization"] = f"Bearer {token_str}"

        return headers or None

    def _build_oauth_provider(self, name: str, spec: dict, headers: dict[str, str] | None = None) -> Any:
        """為遠端 SSE / Streamable HTTP 伺服器建立 OAuth 2.1 PKCE 認證提供者。"""
        if not HAS_MCP_OAUTH:
            return None
        if spec.get("oauth") is False:
            return None
        if headers and any(k.lower() == "authorization" for k in headers) and spec.get("oauth") is not True:
            return None

        from pocket_agent.system.auth import REDIRECT_URI

        redirect_uri = str(spec.get("redirect_uri") or REDIRECT_URI).strip()
        storage = KeyringTokenStorage(name)
        flow_handler = _McpOAuthFlowHandler(name, redirect_uri)
        metadata = OAuthClientMetadata(
            client_name=str(spec.get("client_name") or "PocketAgent"),
            redirect_uris=[redirect_uri],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=spec.get("scope"),
        )
        return _KeyringOAuthClientProvider(
            server_url=str(spec["url"]),
            client_metadata=metadata,
            storage=storage,
            redirect_handler=flow_handler.redirect_handler,
            callback_handler=flow_handler.callback_handler,
        )

    async def _run_server_lifecycle(
        self,
        name: str,
        spec: dict,
        ready_future: asyncio.Future[ClientSession],
        stop_event: asyncio.Event,
    ) -> None:
        from contextlib import AsyncExitStack

        closers_before = len(self._exit_stack_closers)
        async with AsyncExitStack() as stack:
            try:
                transport = spec.get("transport", "stdio")
                norm_transport = str(transport).strip().lower().replace(" ", "-").replace("_", "-")

                if norm_transport == "stdio":
                    params = self._resolve_stdio_params(spec)
                    errlog = self._open_stdio_errlog()
                    res = await stack.enter_async_context(stdio_client(params, errlog=errlog))
                    read, write = res[0], res[1]
                elif norm_transport == "sse":
                    if not HAS_SSE:
                        raise ImportError("需要安裝 mcp[sse] 才能使用 SSE transport")
                    headers = self._resolve_http_headers(name, spec)
                    auth_provider = self._build_oauth_provider(name, spec, headers=headers)
                    sse_kwargs: dict[str, Any] = {}
                    if headers is not None:
                        sse_kwargs["headers"] = headers
                    if auth_provider is not None:
                        sse_kwargs["auth"] = auth_provider
                    res = await stack.enter_async_context(sse_client(spec["url"], **sse_kwargs))
                    read, write = res[0], res[1]
                elif norm_transport in ("streamable-http", "streamablehttp", "http"):
                    if not HAS_STREAMABLE_HTTP:
                        raise ImportError("目前 mcp 版本不支援 Streamable HTTP transport")
                    headers = self._resolve_http_headers(name, spec)
                    auth_provider = self._build_oauth_provider(name, spec, headers=headers)
                    http_client = create_mcp_http_client(headers=headers, auth=auth_provider)
                    await stack.enter_async_context(http_client)
                    res = await stack.enter_async_context(streamable_http_client(spec["url"], http_client=http_client))
                    read, write = res[0], res[1]
                else:
                    raise ValueError(f"未知 transport: {transport!r}")

                session = ClientSession(read, write)
                await stack.enter_async_context(session)
                await session.initialize()
                if not ready_future.done():
                    ready_future.set_result(session)
            except BaseException as exc:
                while len(self._exit_stack_closers) > closers_before:
                    closer = self._exit_stack_closers.pop()
                    try:
                        await closer(None, None, None)
                    except Exception:
                        pass
                if not ready_future.done():
                    ready_future.set_exception(exc)
                return

            try:
                await stop_event.wait()
            except asyncio.CancelledError:
                pass

    async def _connect(self, name: str, spec: dict) -> ClientSession:
        loop = asyncio.get_running_loop()
        ready_future: asyncio.Future[ClientSession] = loop.create_future()
        stop_event = asyncio.Event()

        task = asyncio.create_task(
            self._run_server_lifecycle(name, spec, ready_future, stop_event),
            name=f"mcp-server-{name}",
        )
        runner = _ServerRunner(name=name, task=task, stop_event=stop_event)
        self._runners[name] = runner

        try:
            session = await ready_future
            runner.session = session
            return session
        except BaseException:
            stop_event.set()
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=1.0)
            except Exception:
                pass
            self._runners.pop(name, None)
            raise

    async def _enter(self, cm):
        result = await cm.__aenter__()
        self._exit_stack_closers.append(cm.__aexit__)
        return *result, cm.__aexit__

    async def _register_tools(
        self,
        server_name: str,
        session: ClientSession,
        inject: dict,
        safe_read_tools: list[str] | tuple[str, ...] | set[str] | str = (),
        is_sensitive: bool = False,
    ) -> None:
        response = await session.list_tools()
        for tool in response.tools:
            # 從 schema 移除 inject 參數——模型不需要知道這些
            schema_attr = getattr(tool, "inputSchema", getattr(tool, "input_schema", None))
            raw_params = schema_attr or {"type": "object", "properties": {}}
            raw_props = raw_params.get("properties") or {}
            conn_params = {k: v for k, v in _CONN_PARAM_MAP.items() if k in raw_props and k not in inject}
            strip_keys = {**inject, **conn_params}
            params = _strip_inject_params(raw_params, strip_keys)

            # Artifact 隔離機制：針對檔案寫入工具注入 ArtifactMetadata 定義
            if tool.name in ("write_to_file", "write_file", "create_file"):
                if "properties" not in params:
                    params["properties"] = {}
                params["properties"]["ArtifactMetadata"] = {
                    "type": "object",
                    "description": (
                        "Metadata that defines artifact properties. Required when creating an artifact file."
                    ),
                    "properties": {"Summary": {"type": "string"}, "UserFacing": {"type": "boolean"}},
                    "required": ["Summary", "UserFacing"],
                }

            schema = {
                "type": "function",
                "name": tool.name,
                "description": tool.description or "",
                "parameters": params,
            }
            if safe_read_tools == "*" or (
                isinstance(safe_read_tools, list | tuple | set) and tool.name in safe_read_tools
            ):
                schema["_is_safe_read"] = True
            if tool.name in self._tools:
                from pocket_agent.core.agent import log

                log.warning(
                    f"MCP Tool conflict: '{tool.name}' from server '{server_name}' "
                    "will be ignored because it is already registered."
                )
                continue

            self._tools[tool.name] = ToolEntry(
                name=tool.name,
                server_name=server_name,
                session=session,
                schema=schema,
                inject=inject,
                conn_params=conn_params,
                is_sensitive=is_sensitive,
            )


def _strip_inject_params(schema: dict, inject: dict) -> dict:
    """從 JSON Schema 移除會被自動注入的參數，避免模型看到並嘗試填寫。"""
    if not inject:
        return schema
    schema = copy.deepcopy(schema)
    props = schema.get("properties", {})
    schema["properties"] = {k: v for k, v in props.items() if k not in inject}
    if "required" in schema:
        schema["required"] = [r for r in schema["required"] if r not in inject]
    return schema


def _is_conn_auth_error(text: str) -> bool:
    """判斷工具回傳字串是否屬於「遠端連線失敗或帳號密碼認證失敗」。

    僅在此類錯誤發生時清除記憶體連線快取，避免因一般遠端指令錯誤（如檔案不存在、參數格式不符）
    而誤刪已驗證成功的帳密快取。
    """
    s = text.strip()
    if not s.startswith("[ERROR]"):
        return False
    lower = s.lower()
    return (
        "[conn_auth]" in lower
        or "exit 255" in lower
        or "permission denied" in lower
        or "access denied" in lower
        or "authentication failed" in lower
    )


class KeyringTokenStorage:
    """基於作業系統原生 Keyring 的 MCP OAuth 2.1 憑證儲存器（零明碼落盤）。"""

    def __init__(self, server_name: str):
        self.server_name = server_name
        self._token_key = f"mcp-oauth-{server_name}"
        self._client_key = f"mcp-client-{server_name}"

    async def get_tokens(self) -> Any:
        if not HAS_MCP_OAUTH:
            return None
        from pocket_agent.system.auth import _load_token

        data = _load_token(self._token_key)
        if not isinstance(data, dict) or not data.get("access_token"):
            return None
        expires_at = data.get("expires_at")
        if isinstance(expires_at, int | float):
            remaining = int(expires_at - time.time())
            if remaining <= 0 and not data.get("refresh_token"):
                return None
            data = {**data, "expires_in": remaining}
        return OAuthToken.model_validate(data)

    async def set_tokens(self, tokens: Any) -> None:
        from pocket_agent.system.auth import _save_token

        payload = tokens.model_dump(mode="json", exclude_none=True)
        _save_token(self._token_key, payload)

    async def get_client_info(self) -> Any:
        if not HAS_MCP_OAUTH:
            return None
        from pocket_agent.system.auth import _load_token

        data = _load_token(self._client_key)
        if not isinstance(data, dict) or not data.get("client_id"):
            return None
        return OAuthClientInformationFull.model_validate(data)

    async def set_client_info(self, client_info: Any) -> None:
        from pocket_agent.system.auth import _save_token

        payload = client_info.model_dump(mode="json", exclude_none=True)
        _save_token(self._client_key, payload)


if HAS_MCP_OAUTH:

    class _KeyringOAuthClientProvider(OAuthClientProvider):
        """擴充標準 OAuthClientProvider，於載入儲存之 Token 時同步計算到期時間以觸發自動更新。"""

        async def _initialize(self) -> None:
            await super()._initialize()
            if self.context.current_tokens is not None:
                self.context.update_token_expiry(self.context.current_tokens)

else:
    _KeyringOAuthClientProvider = None  # type: ignore[assignment]


class _McpOAuthFlowHandler:
    """處理遠端 MCP 伺服器的本機瀏覽器 OAuth 2.1 PKCE 授權跳轉與回呼接收。"""

    def __init__(self, server_name: str, redirect_uri: str):
        self.server_name = server_name
        self.redirect_uri = redirect_uri
        self._bucket: dict[str, Any] = {}
        self._server: Any = None
        self._opened: bool = False

    async def redirect_handler(self, authorization_url: str) -> None:
        import uvicorn
        from fastapi import FastAPI
        from fastapi.responses import HTMLResponse

        self._bucket.clear()
        parsed_uri = urlparse(self.redirect_uri)
        listen_port = parsed_uri.port or 1455
        listen_path = parsed_uri.path or "/auth/callback"
        app = FastAPI()

        def _page(color: str, message: str) -> HTMLResponse:
            return HTMLResponse(
                f"<h1 style='font-family:sans-serif;text-align:center;padding-top:20vh;color:{color}'>{message}</h1>"
            )

        @app.get(listen_path)
        async def callback(
            code: str = "",
            state: str = "",
            iss: str = "",
            error: str = "",
            error_description: str = "",
        ):
            if error:
                self._bucket["error"] = error_description or error
                return _page("#c0392b", f"✗ MCP 授權未完成：{error}")
            if not code:
                self._bucket["error"] = "沒有收到授權碼。"
                return _page("#c0392b", "✗ 沒有收到授權碼。")
            self._bucket["code"] = code
            self._bucket["state"] = state or None
            self._bucket["iss"] = iss or None
            return _page("#10a37f", "✓ MCP 認證成功！請關閉此視窗回到 Pocket Agent。")

        cfg = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=listen_port,
            log_level="error",
            log_config=None,
        )
        self._server = uvicorn.Server(cfg)
        thread = threading.Thread(target=self._server.run, daemon=True)
        thread.start()

        for _ in range(30):
            if self._server.started:
                break
            if not thread.is_alive():
                raise RuntimeError(f"無法在 port {listen_port} 啟動 MCP OAuth 本機回呼服務，請確認連接埠未被占用。")
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError("MCP OAuth 本機回呼服務啟動逾時。")

        try:
            self._opened = bool(webbrowser.open(authorization_url))
        except Exception:
            self._opened = False

        if self._opened:
            print(f"\n[MCP:{self.server_name}] 瀏覽器已開啟，請完成 OAuth 登入並授權…")
        else:
            print(f"\n[MCP:{self.server_name}] 無法自動開啟瀏覽器，請手動複製以下網址完成授權：")
        print(f"\n  {authorization_url}\n")

    async def callback_handler(self) -> Any:
        try:
            for i in range(300):
                if "code" in self._bucket:
                    return AuthorizationCodeResult(
                        code=self._bucket["code"],
                        state=self._bucket.get("state"),
                        iss=self._bucket.get("iss"),
                    )
                if "error" in self._bucket:
                    raise RuntimeError(f"MCP OAuth 授權未完成：{self._bucket['error']}")
                if i == 15 and not self._opened and sys.stdin is not None and sys.stdin.isatty():
                    prompt_msg = f"\n[MCP:{self.server_name}] 登入後請貼上瀏覽器跳轉的完整 redirect URL：\n> "
                    redirect_url = (await run_sync(input, prompt_msg)).strip()
                    parsed = parse_qs(urlparse(redirect_url).query)
                    if "code" in parsed:
                        return AuthorizationCodeResult(
                            code=parsed["code"][0],
                            state=parsed.get("state", [None])[0],
                            iss=parsed.get("iss", [None])[0],
                        )
                await asyncio.sleep(1.0)
            raise TimeoutError(f"MCP OAuth 認證逾時（{self.server_name}）")
        finally:
            if self._server is not None:
                self._server.should_exit = True
