"""管理多個 MCP server 連線，並把所有 tools 合併成 OpenAI Responses API 格式。

支援 inject 參數自動注入：
  在 mcp_config.yaml 的 server 底下加 inject 區段，
  指定的參數會自動帶入工具呼叫，並從送給模型的 schema 中移除。
  模型不需要知道這些參數的存在。
"""

import copy
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from pocket_agent.system.paths import bundle_dir, is_frozen, mcp_config_path, mcp_example_path

try:
    from mcp.client.sse import sse_client

    HAS_SSE = True
except ImportError:
    HAS_SSE = False


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
}


@dataclass
class ToolEntry:
    name: str
    server_name: str
    session: ClientSession
    schema: dict  # 送給模型的 schema（已移除 inject 參數）
    inject: dict = field(default_factory=dict)  # 自動注入的參數值
    conn_params: dict[str, str] = field(default_factory=dict)
    is_sensitive: bool = False


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
                    name.lower() in ("rhel-ops", "rhel", "win-rdp", "win_rdp")
                    or "rhel_mcp" in cmd_str
                    or "rhel_mcp" in args_str
                ):
                    is_sensitive = True
                await self._register_tools(name, session, inject, safe_read, is_sensitive=is_sensitive)
                ready_servers += 1
            except Exception as e:
                failed_servers += 1
                print(f"[MCP] ⚠ 無法連線 {name!r}: {e}")
        return MCPStartupResult(len(servers), ready_servers, failed_servers, len(self._tools))

    async def stop(self) -> None:
        for closer in reversed(self._exit_stack_closers):
            try:
                await closer(None, None, None)
            except Exception as e:
                import logging

                logging.getLogger("pocket-agent").warning("MCP closer failed: %s", e, exc_info=True)

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
            if clean:
                resolved[env_key] = clean

        # 判斷是否已配置金鑰路徑
        has_key = bool(resolved.get("RHEL_SSH_KEY_PATH"))

        # 判定哪些必要參數缺失需要動態向使用者詢問
        for env_key in entry.conn_params.values():
            if env_key in resolved:
                continue
            # 金鑰路徑與 Passphrase 均為選填，絕不主動視為 missing 攔截詢問
            if env_key in ("RHEL_SSH_KEY_PATH", "RHEL_SSH_KEY_PASSPHRASE"):
                continue
            # 若已提供金鑰路徑，密碼（作為 Passphrase）亦為選填，不強制詢問
            if has_key and env_key == "RHEL_SSH_PASSWORD":
                continue
            missing.append(env_key)

        if missing and self.on_prompt_conn is not None:
            import asyncio

            prompted = await asyncio.to_thread(self.on_prompt_conn, entry.server_name, missing)
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
        return wrap_external_content(joined)

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
        return tool_name.startswith("rhel_") or tool_name.startswith("win_rdp_")

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
            if isinstance(raw_env, dict):
                for k, v in raw_env.items():
                    if v is not None:
                        env_dict[str(k)] = str(v)

        return StdioServerParameters(command=command, args=resolved_args, env=env_dict)

    async def _connect(self, name: str, spec: dict) -> ClientSession:
        transport = spec.get("transport", "stdio")
        closers_before = len(self._exit_stack_closers)

        try:
            if transport == "stdio":
                params = self._resolve_stdio_params(spec)
                read, write, _ = await self._enter(stdio_client(params))
            elif transport == "sse":
                if not HAS_SSE:
                    raise ImportError("需要安裝 mcp[sse] 才能使用 SSE transport")
                read, write, _ = await self._enter(sse_client(spec["url"]))
            else:
                raise ValueError(f"未知 transport: {transport!r}")

            session = ClientSession(read, write)
            await session.__aenter__()
            try:
                await session.initialize()
            except BaseException:
                await session.__aexit__(None, None, None)
                raise
            self._exit_stack_closers.append(session.__aexit__)
            return session
        except BaseException:
            while len(self._exit_stack_closers) > closers_before:
                closer = self._exit_stack_closers.pop()
                try:
                    await closer(None, None, None)
                except Exception:
                    pass
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
            conn_params = {k: v for k, v in _CONN_PARAM_MAP.items() if k in raw_props}
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
