"""管理多個 MCP server 連線，並把所有 tools 合併成 OpenAI Responses API 格式。

支援 inject 參數自動注入：
  在 mcp_config.yaml 的 server 底下加 inject 區段，
  指定的參數會自動帶入工具呼叫，並從送給模型的 schema 中移除。
  模型不需要知道這些參數的存在。
"""

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .paths import mcp_config_path, mcp_example_path

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


class MCPManager:
    """載入 mcp_config.yaml，建立所有 server 連線，提供統一工具查詢與執行介面。"""

    def __init__(self, config_path: str | Path | None = None):
        self.config_path = Path(config_path) if config_path else mcp_config_path()
        self._tools: dict[str, ToolEntry] = {}
        self._sessions: list[ClientSession] = []
        self._exit_stack_closers: list[Any] = []

    # ── Lifecycle ─────────────────────────────────

    async def start(self) -> None:
        config = self._load_config()
        servers = config.get("servers") or {}
        for name, spec in servers.items():
            if spec is None:
                continue
            try:
                session = await self._connect(name, spec)
                self._sessions.append(session)
                inject = spec.get("inject") or {}
                await self._register_tools(name, session, inject)
            except Exception as e:
                print(f"[MCP] ⚠ 無法連線 {name!r}: {e}")

    async def stop(self) -> None:
        for closer in reversed(self._exit_stack_closers):
            try:
                await closer()
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
        from agent.core import log

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
            raw_params = tool.inputSchema or {"type": "object", "properties": {}}
            params = _strip_inject_params(raw_params, inject)

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
