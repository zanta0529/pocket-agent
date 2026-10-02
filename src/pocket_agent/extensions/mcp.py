"""管理多個 MCP server 連線，並把所有 tools 合併成 OpenAI Responses API 格式。

支援 inject 參數自動注入：
  在 mcp_config.yaml 的 server 底下加 inject 區段，
  指定的參數會自動帶入工具呼叫，並從送給模型的 schema 中移除。
  模型不需要知道這些參數的存在。
"""

import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from pocket_agent.system.paths import mcp_config_path, mcp_example_path

try:
    from mcp.client.sse import sse_client

    HAS_SSE = True
except ImportError:
    HAS_SSE = False


@dataclass
class ToolEntry:
    name: str
    server_name: str
    session: ClientSession
    schema: dict  # 送給模型的 schema（已移除 inject 參數）
    inject: dict = field(default_factory=dict)  # 自動注入的參數值


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
                session = await self._connect(name, spec)
                self._sessions.append(session)
                inject = spec.get("inject") or {}
                await self._register_tools(name, session, inject)
                ready_servers += 1
            except Exception as e:
                failed_servers += 1
                print(f"[MCP] ⚠ 無法連線 {name!r}: {e}")
        return MCPStartupResult(len(servers), ready_servers, failed_servers, len(self._tools))

    async def stop(self) -> None:
        for closer in reversed(self._exit_stack_closers):
            try:
                await closer(None, None, None)
            except Exception:
                pass

    # ── Tool access ───────────────────────────────

    def openai_tools(self) -> list[dict]:
        """回傳送給模型的 tools 陣列（已隱藏 inject 參數）。"""
        return [entry.schema for entry in self._tools.values()]

    async def call(self, tool_name: str, arguments: dict) -> str:
        """執行指定工具，自動補入 inject 參數後呼叫。"""
        entry = self._tools.get(tool_name)
        if entry is None:
            raise KeyError(f"找不到工具 {tool_name!r}，可用：{list(self._tools)}")

        # 注入參數（模型看不到，但 server 需要）
        full_args = {**entry.inject, **arguments}
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

        log.debug("MCP call_tool name=%r full_args=%r", tool_name, full_args)

        result = await entry.session.call_tool(tool_name, full_args)
        log.debug("MCP raw result content=%r", result.content)

        parts = []
        for block in result.content:
            if hasattr(block, "text"):
                parts.append(block.text)
            else:
                parts.append(str(block))
        return "\n".join(parts)

    def list_tools(self) -> list[str]:
        return list(self._tools.keys())

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

    async def _connect(self, name: str, spec: dict) -> ClientSession:
        transport = spec.get("transport", "stdio")

        if transport == "stdio":
            params = StdioServerParameters(
                command=spec["command"],
                args=spec.get("args", []),
                env=spec.get("env"),
            )
            read, write, _ = await self._enter(stdio_client(params))
        elif transport == "sse":
            if not HAS_SSE:
                raise ImportError("需要安裝 mcp[sse] 才能使用 SSE transport")
            read, write, _ = await self._enter(sse_client(spec["url"]))
        else:
            raise ValueError(f"未知 transport: {transport!r}")

        session = ClientSession(read, write)
        await session.__aenter__()
        await session.initialize()
        self._exit_stack_closers.append(session.__aexit__)
        return session

    async def _enter(self, cm):
        result = await cm.__aenter__()
        self._exit_stack_closers.append(cm.__aexit__)
        return *result, cm.__aexit__

    async def _register_tools(self, server_name: str, session: ClientSession, inject: dict) -> None:
        response = await session.list_tools()
        for tool in response.tools:
            # 從 schema 移除 inject 參數——模型不需要知道這些
            schema_attr = getattr(tool, "inputSchema", getattr(tool, "input_schema", None))
            raw_params = schema_attr or {"type": "object", "properties": {}}
            params = _strip_inject_params(raw_params, inject)

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
            self._tools[tool.name] = ToolEntry(
                name=tool.name,
                server_name=server_name,
                session=session,
                schema=schema,
                inject=inject,
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
