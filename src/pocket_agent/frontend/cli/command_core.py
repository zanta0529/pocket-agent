from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pocket_agent.core.agent import Agent
    from pocket_agent.frontend.cli.input import CliInput
    from pocket_agent.frontend.cli.render import CliRenderer
    from pocket_agent.frontend.cli.state import CliState


@dataclass
class CommandResult:
    action: str = "handled"
    prompt: str = ""
    display_text: str | None = None
    extra: dict | None = None


@dataclass
class CommandContext:
    agent: "Agent"
    state: "CliState"
    reader: "CliInput"
    renderer: "CliRenderer"
    tools: list[str]


class CliCommand(Protocol):
    async def execute(self, ctx: CommandContext, args: str) -> CommandResult: ...


class CliCommandRegistry:
    def __init__(self) -> None:
        self._commands: dict[str, CliCommand] = {}

    def register(self, name: str, command: CliCommand) -> None:
        self._commands[name] = command

    def get(self, name: str) -> CliCommand | None:
        return self._commands.get(name)
