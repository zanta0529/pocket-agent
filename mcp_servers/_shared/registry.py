"""MCP 伺服器工廠與工具註冊器契約（Factory + Command/Registrar Pattern）。"""

from collections.abc import Callable, Sequence
from typing import Any, Protocol, runtime_checkable

from mcp_servers._shared.bootstrap import get_mcp_server_class


@runtime_checkable
class ToolRegistrar(Protocol):
    """工具群組註冊契約：各子領域模組透過 register(mcp) 掛載工具，避免單檔膨脹。"""

    def register(self, mcp: Any) -> None:
        """將本模組所屬工具註冊至指定的 MCPServer 實例。"""
        ...


class FunctionRegistrar:
    """將一組純工具函式封裝為 ToolRegistrar，便於單元測試直接匯入純函式。"""

    def __init__(self, tools: Sequence[Callable[..., Any]]) -> None:
        self._tools = tuple(tools)

    def register(self, mcp: Any) -> None:
        for fn in self._tools:
            mcp.tool()(fn)


def create_mcp_server(
    name: str,
    registrars: Sequence[ToolRegistrar | Callable[[Any], None]] = (),
    *,
    lifespan: Any = None,
) -> Any:
    """建立 MCPServer 實例並依序執行所有註冊器（Factory Pattern）。"""
    server_cls = get_mcp_server_class()
    kwargs: dict[str, Any] = {}
    if lifespan is not None:
        kwargs["lifespan"] = lifespan
    mcp = server_cls(name, **kwargs)

    for registrar in registrars:
        if isinstance(registrar, ToolRegistrar):
            registrar.register(mcp)
        else:
            registrar(mcp)
    return mcp
