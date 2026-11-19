"""設定精靈 — python main.py setup"""

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.rule import Rule
from rich.text import Text

from pocket_agent.system.i18n import get_available_locales, get_locale, set_locale, t
from pocket_agent.system.paths import mcp_config_path

console = Console()


def _init_setup_locale() -> None:
    """初始化精靈語系：若 config.json 有明確設定 locale 則採用，否則預設英文。"""
    import json

    from pocket_agent.system.config import CONFIG_PATH

    initial_locale = "en_US"
    if CONFIG_PATH.is_file():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("locale"), str) and raw["locale"].strip():
                initial_locale = raw["locale"].strip()
        except Exception:
            pass
    set_locale(initial_locale)


def run_setup() -> None:
    _init_setup_locale()

    console.print()
    console.print(
        Panel(
            Text(t("wizard.title"), justify="center", style="bold cyan"),
            subtitle=t("wizard.subtitle"),
            border_style="cyan",
        )
    )
    console.print()

    try:
        _step_locale()
        provider = _step_provider()
        _step_login(provider)
        _step_model(provider)
        _step_mcp()
    except KeyboardInterrupt:
        console.print(f"\n[yellow]{t('wizard.cancelled')}[/yellow]")
        return

    console.print()
    console.print(Rule(style="green"))
    completed_msg = t("wizard.completed", command="python main.py")
    console.print(f"[bold green]{completed_msg}[/bold green]")
    console.print()


def _step_locale() -> str:
    from pocket_agent.system.config import config

    console.print(Rule(t("wizard.step_locale_title")))
    console.print()

    current_locale = get_locale()
    locales = get_available_locales()
    for i, loc in enumerate(locales, 1):
        tag = f" [green]{t('wizard.current_tag')}[/green]" if loc == current_locale else ""
        console.print(f"  [cyan]{i}[/cyan]. [bold]{loc}[/bold]{tag}")

    default_idx = str(locales.index(current_locale) + 1) if current_locale in locales else "1"
    choice = Prompt.ask(
        t("wizard.choose_prompt"),
        choices=[str(i) for i in range(1, len(locales) + 1)],
        default=default_idx,
    )
    selected = locales[int(choice) - 1]
    config.set("locale", selected)
    set_locale(selected)
    console.print(f"[green]{t('wizard.locale_configured', locale=selected)}[/green]\n")
    return selected


def _step_provider() -> str:
    from pocket_agent.system.config import config
    from pocket_agent.system.providers import PROVIDER_STRATEGIES, SUPPORTED_PROVIDERS

    console.print(Rule(t("wizard.step_provider_title")))
    console.print()

    current_provider = config.get("active_provider", "openai")

    providers = SUPPORTED_PROVIDERS
    for i, p in enumerate(providers, 1):
        tag = f" [green]{t('wizard.current_tag')}[/green]" if p == current_provider else ""
        display = PROVIDER_STRATEGIES[p].display_name
        console.print(f"  [cyan]{i}[/cyan]. [bold]{display}[/bold]{tag}")

    choice = Prompt.ask(
        t("wizard.choose_prompt"),
        choices=[str(i) for i in range(1, len(providers) + 1)],
        default=str(providers.index(current_provider) + 1 if current_provider in providers else 1),
    )
    selected = providers[int(choice) - 1]

    config.set("active_provider", selected)
    configured_msg = t(
        "wizard.provider_configured",
        provider=PROVIDER_STRATEGIES[selected].display_name,
    )
    console.print(f"[green]{configured_msg}[/green]\n")
    return selected


def _step_login(provider: str) -> None:
    from pocket_agent.system.config import config
    from pocket_agent.system.providers import PROVIDER_STRATEGIES

    console.print(Rule(t("wizard.step_login_title")))
    console.print()

    strategy_cls = PROVIDER_STRATEGIES.get(provider)
    if strategy_cls and hasattr(strategy_cls, "setup_wizard"):
        strategy_cls.setup_wizard(config)


def _step_model(provider: str) -> None:
    from pocket_agent.system.config import config

    console.print(Rule(t("wizard.step_model_title")))
    console.print()

    current = config.get("active_model", "gpt-4o")
    console.print(f"[dim]{t('wizard.model_current', model=current)}[/dim]\n")

    # 嘗試動態取得遠端/本地可用模型
    fetched = []
    try:
        from pocket_agent.system.auth import fetch_available_models

        fetched = fetch_available_models(provider)
    except Exception:
        pass

    if fetched:
        provider_models = [(m, t("wizard.model_from_remote")) for m in fetched]
    else:
        provider_models = []

    if not provider_models:
        console.print(f"[yellow]{t('wizard.model_unavailable')}[/yellow]")
        model = Prompt.ask(t("wizard.model_name_prompt")).strip()
    else:
        for i, (name, desc) in enumerate(provider_models, 1):
            tag = f" [green]{t('wizard.current_tag')}[/green]" if name == current else ""
            console.print(f"  [cyan]{i}[/cyan]. [bold]{name}[/bold]  {desc}{tag}")
        console.print(f"  [cyan]{len(provider_models) + 1}[/cyan]. {t('wizard.model_custom')}")
        console.print()

        choice = Prompt.ask(
            t("wizard.choose_prompt"),
            choices=[str(i) for i in range(1, len(provider_models) + 2)],
            default="1",
        )
        if int(choice) == len(provider_models) + 1:
            model = Prompt.ask(t("wizard.model_input_prompt")).strip()
        else:
            model = provider_models[int(choice) - 1][0]

    config.set("active_model", model)
    console.print(f"[green]{t('wizard.model_configured', model=model)}[/green]\n")


def _step_mcp() -> None:
    console.print(Rule(t("wizard.step_mcp_title")))
    console.print()
    console.print(f"[dim]{t('wizard.mcp_desc')}[/dim]\n")

    mcp_path = mcp_config_path()
    cfg = yaml.safe_load(mcp_path.read_text(encoding="utf-8")) if mcp_path.exists() else {}
    servers: dict = cfg.get("servers") or {}

    from pocket_agent.extensions.mcp import is_mcp_server_enabled

    configured = [
        k if is_mcp_server_enabled(v, server_name=k) else f"{k}{t('wizard.mcp_disabled_tag')}"
        for k, v in servers.items()
        if v is not None
    ]
    if configured:
        console.print(f"[dim]{t('wizard.mcp_configured_list', servers=', '.join(configured))}[/dim]\n")

    if not Confirm.ask(t("wizard.mcp_ask_add"), default=False):
        console.print(f"[dim]{t('wizard.mcp_skipped')}[/dim]\n")
        return

    while True:
        name = Prompt.ask(t("wizard.mcp_server_name")).strip()
        transport = Prompt.ask(
            t("wizard.mcp_transport"),
            choices=["stdio", "sse", "streamable-http"],
            default="stdio",
        )

        if transport == "stdio":
            command = Prompt.ask(t("wizard.mcp_command")).strip()
            args_raw = Prompt.ask(t("wizard.mcp_args"), default="").strip()
            servers[name] = {"transport": "stdio", "command": command, "args": args_raw.split() if args_raw else []}
        elif transport == "streamable-http":
            url = Prompt.ask(t("wizard.mcp_streamable_url")).strip()
            servers[name] = {"transport": "streamable-http", "url": url}
        else:
            url = Prompt.ask(t("wizard.mcp_sse_url")).strip()
            servers[name] = {"transport": "sse", "url": url}

        console.print(f"[green]{t('wizard.mcp_added', name=name)}[/green]")
        if not Confirm.ask(t("wizard.mcp_ask_more"), default=False):
            break

    cfg["servers"] = servers
    mcp_path.parent.mkdir(parents=True, exist_ok=True)
    mcp_path.write_text(yaml.dump(cfg, allow_unicode=True, default_flow_style=False), encoding="utf-8")
    console.print(f"[green]{t('wizard.mcp_updated')}[/green]\n")
