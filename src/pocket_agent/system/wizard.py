"""設定精靈 — python main.py setup"""

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.rule import Rule
from rich.text import Text

from pocket_agent.system.paths import mcp_config_path

console = Console()


def run_setup() -> None:
    console.print()
    console.print(
        Panel(
            Text("Pocket Agent 設定精靈", justify="center", style="bold cyan"),
            subtitle="按 Ctrl+C 隨時離開",
            border_style="cyan",
        )
    )
    console.print()

    try:
        provider = _step_provider()
        _step_login(provider)
        _step_model(provider)
        _step_mcp()
    except KeyboardInterrupt:
        console.print("\n[yellow]已取消。[/yellow]")
        return

    console.print()
    console.print(Rule(style="green"))
    console.print("[bold green]✓ 設定完成！[/bold green]  執行 [cyan]python main.py[/cyan] 開始使用。")
    console.print()


def _step_provider() -> str:
    from pocket_agent.system.config import config
    from pocket_agent.system.providers import PROVIDER_STRATEGIES, SUPPORTED_PROVIDERS

    console.print(Rule("① 選擇服務供應商 (Provider)"))
    console.print()

    current_provider = config.get("active_provider", "openai")

    providers = SUPPORTED_PROVIDERS
    for i, p in enumerate(providers, 1):
        tag = " [green]← 目前[/green]" if p == current_provider else ""
        display = PROVIDER_STRATEGIES[p].display_name
        console.print(f"  [cyan]{i}[/cyan]. [bold]{display}[/bold]{tag}")

    choice = Prompt.ask(
        "選擇",
        choices=[str(i) for i in range(1, len(providers) + 1)],
        default=str(providers.index(current_provider) + 1 if current_provider in providers else 1),
    )
    selected = providers[int(choice) - 1]

    config.set("active_provider", selected)
    console.print(f"[green]✓ 已設定為 {PROVIDER_STRATEGIES[selected].display_name}[/green]\n")
    return selected


def _step_login(provider: str) -> None:
    from pocket_agent.system.config import config
    from pocket_agent.system.providers import PROVIDER_STRATEGIES

    console.print(Rule("② 供應商設定"))
    console.print()

    strategy_cls = PROVIDER_STRATEGIES.get(provider)
    if strategy_cls and hasattr(strategy_cls, "setup_wizard"):
        strategy_cls.setup_wizard(config)


def _step_model(provider: str) -> None:
    from pocket_agent.system.config import config

    console.print(Rule("③ 預設模型"))
    console.print()

    current = config.get("active_model", "gpt-4o")
    console.print(f"[dim]目前：{current}[/dim]\n")

    # 嘗試動態取得遠端/本地可用模型
    fetched = []
    try:
        from pocket_agent.system.auth import fetch_available_models

        fetched = fetch_available_models(provider)
    except Exception:
        pass

    if fetched:
        provider_models = [(m, "從遠端取得") for m in fetched]
    else:
        provider_models = []

    if not provider_models:
        console.print("[yellow]無法取得推薦模型，請手動輸入。[/yellow]")
        model = Prompt.ask("模型名稱").strip()
    else:
        for i, (name, desc) in enumerate(provider_models, 1):
            tag = " [green]← 目前[/green]" if name == current else ""
            console.print(f"  [cyan]{i}[/cyan]. [bold]{name}[/bold]  {desc}{tag}")
        console.print(f"  [cyan]{len(provider_models) + 1}[/cyan]. 自訂")
        console.print()

        choice = Prompt.ask("選擇", choices=[str(i) for i in range(1, len(provider_models) + 2)], default="1")
        if int(choice) == len(provider_models) + 1:
            model = Prompt.ask("請輸入模型名稱").strip()
        else:
            model = provider_models[int(choice) - 1][0]

    config.set("active_model", model)
    console.print(f"[green]✓ 已設定預設模型為 {model}[/green]\n")


def _step_mcp() -> None:
    console.print(Rule("④ MCP 工具（可略過）"))
    console.print()
    console.print("[dim]MCP 讓 agent 可以呼叫外部工具，例如讀寫檔案、查資料等。[/dim]\n")

    mcp_path = mcp_config_path()
    cfg = yaml.safe_load(mcp_path.read_text(encoding="utf-8")) if mcp_path.exists() else {}
    servers: dict = cfg.get("servers") or {}

    active = [k for k, v in servers.items() if v is not None]
    if active:
        console.print(f"[dim]目前已設定：{', '.join(active)}[/dim]\n")

    if not Confirm.ask("要新增 MCP server 嗎？", default=False):
        console.print("[dim]跳過。[/dim]\n")
        return

    while True:
        name = Prompt.ask("Server 名稱").strip()
        transport = Prompt.ask("傳輸方式", choices=["stdio", "sse"], default="stdio")

        if transport == "stdio":
            command = Prompt.ask("執行指令（例如 npx）").strip()
            args_raw = Prompt.ask("參數（空白分隔，可留空）", default="").strip()
            servers[name] = {"transport": "stdio", "command": command, "args": args_raw.split() if args_raw else []}
        else:
            url = Prompt.ask("SSE URL").strip()
            servers[name] = {"transport": "sse", "url": url}

        console.print(f"[green]✓ 已新增 {name}[/green]")
        if not Confirm.ask("繼續新增？", default=False):
            break

    cfg["servers"] = servers
    mcp_path.parent.mkdir(parents=True, exist_ok=True)
    mcp_path.write_text(yaml.dump(cfg, allow_unicode=True, default_flow_style=False), encoding="utf-8")
    console.print("[green]✓ mcp_config.yaml 已更新[/green]\n")
