"""設定精靈 — python main.py setup"""

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.rule import Rule
from rich.text import Text

from pocket_agent.system.auth import get_access_token, logout
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
    from pocket_agent.system.constants import SUPPORTED_PROVIDERS

    console.print(Rule("① 選擇服務供應商 (Provider)"))
    console.print()

    current_provider = config.get("active_provider", "openai")

    providers = SUPPORTED_PROVIDERS
    for i, p in enumerate(providers, 1):
        tag = " [green]← 目前[/green]" if p == current_provider else ""
        console.print(f"  [cyan]{i}[/cyan]. [bold]{p.upper()}[/bold]{tag}")

    choice = Prompt.ask(
        "選擇",
        choices=[str(i) for i in range(1, len(providers) + 1)],
        default=str(providers.index(current_provider) + 1 if current_provider in providers else 1),
    )
    selected = providers[int(choice) - 1]

    config.set("active_provider", selected)
    console.print(f"[green]✓ 已設定為 {selected.upper()}[/green]\n")
    return selected


def _step_login(provider: str) -> None:
    from pocket_agent.system.config import config
    from pocket_agent.system.constants import (
        PROVIDER_GEMINI,
        PROVIDER_GEMINI_OAUTH,
        PROVIDER_GITHUB,
        PROVIDER_GROQ,
        PROVIDER_MISTRAL,
        PROVIDER_OLLAMA,
        PROVIDER_OLLAMACLOUD,
        PROVIDER_OPENAI,
        PROVIDER_OPENROUTER,
        PROVIDER_SAMBANOVA,
    )

    if provider == PROVIDER_OPENAI:
        console.print(Rule("② 登入 OPENAI"))
        console.print()
        from pocket_agent.system.auth import _is_expired, _load_token

        token = _load_token(provider)
        if token and not _is_expired(token):
            console.print("[green]✓ 已登入 OPENAI（token 有效）[/green]")
            if not Confirm.ask("要重新登入嗎？", default=False):
                console.print()
                return
            logout(provider)
        console.print("即將開啟瀏覽器，請用你的 [bold]OPENAI 帳號[/bold] 登入並授權…\n")
        input("按 Enter 繼續…")
        get_access_token(provider=provider)
        console.print()
    elif provider == PROVIDER_OPENROUTER:
        console.print(Rule("② 設定 OpenRouter"))
        console.print()
        current_key = config.get("providers.openrouter.api_key", "")
        current_url = config.get("providers.openrouter.base_url", "https://openrouter.ai/api/v1")

        console.print(f"[dim]目前 Base URL: {current_url}[/dim]")
        new_url = Prompt.ask("請輸入 Base URL", default=current_url)
        config.set("providers.openrouter.base_url", new_url)

        key_hint = "*" * 10 + current_key[-4:] if current_key and len(current_key) > 4 else "未設定"
        console.print(f"[dim]目前 API Key: {key_hint}[/dim]")
        new_key = Prompt.ask("請輸入 API Key (直接 Enter 保持原狀)", default="")
        if new_key.strip():
            config.set("providers.openrouter.api_key", new_key.strip())
        console.print("[green]✓ OpenRouter 設定已更新[/green]\n")
    elif provider == PROVIDER_OLLAMA:
        console.print(Rule("② 設定 Ollama (Local)"))
        console.print()
        current_url = config.get("providers.ollama.base_url", "http://localhost:11434")
        console.print(f"[dim]目前 Base URL: {current_url}[/dim]")
        new_url = Prompt.ask("請輸入 Ollama 連線位址", default=current_url)
        config.set("providers.ollama.base_url", new_url)
        console.print("[green]✓ Ollama 設定已更新[/green]\n")
    elif provider == PROVIDER_OLLAMACLOUD:
        console.print(Rule("② 設定 Ollama Cloud"))
        console.print()
        current_key = config.get("providers.ollama_cloud.api_key", "")
        current_url = config.get("providers.ollama_cloud.base_url", "https://ollama.com/v1")

        console.print(f"[dim]目前 Base URL: {current_url}[/dim]")
        new_url = Prompt.ask("請輸入 Base URL", default=current_url)
        config.set("providers.ollama_cloud.base_url", new_url)

        key_hint = "*" * 10 + current_key[-4:] if current_key and len(current_key) > 4 else "未設定"
        console.print(f"[dim]目前 API Key: {key_hint}[/dim]")
        new_key = Prompt.ask("請輸入 API Key (直接 Enter 保持原狀)", default="")
        if new_key.strip():
            config.set("providers.ollama_cloud.api_key", new_key.strip())
        console.print("[green]✓ Ollama Cloud 設定已更新[/green]\n")
    elif provider == PROVIDER_GEMINI:
        console.print(Rule("② 設定 Gemini (Google AI Studio)"))
        console.print()
        current_key = config.get("providers.gemini.api_key", "")
        key_hint = "*" * 10 + current_key[-4:] if current_key and len(current_key) > 4 else "未設定"
        console.print(f"[dim]目前 API Key: {key_hint}[/dim]")
        new_key = Prompt.ask("請輸入 Gemini API Key (直接 Enter 保持原狀)", default="")
        if new_key.strip():
            config.set("providers.gemini.api_key", new_key.strip())
        console.print("[green]✓ Gemini API Key 設定已更新[/green]\n")
    elif provider == PROVIDER_GEMINI_OAUTH:
        console.print(Rule("② 登入 GEMINI (OAuth)"))
        console.print()

        current_client_id = config.get("providers.gemini.client_id", "")
        console.print(f"[dim]目前 Google OAuth Client ID: {current_client_id or '未設定'}[/dim]")
        new_client_id = Prompt.ask("請輸入 Client ID (直接 Enter 保持原狀)", default="")
        if new_client_id.strip():
            config.set("providers.gemini.client_id", new_client_id.strip())

        current_client_secret = config.get("providers.gemini.client_secret", "")
        console.print(
            f"[dim]目前 Google OAuth Client Secret (選填): {'***' if current_client_secret else '未設定'}[/dim]"
        )
        new_client_secret = Prompt.ask("請輸入 Client Secret (直接 Enter 保持原狀)", default="")
        if new_client_secret.strip():
            config.set("providers.gemini.client_secret", new_client_secret.strip())

        from pocket_agent.system.auth import _is_expired, _load_token

        token = _load_token(provider)
        if token and not _is_expired(token):
            console.print("[green]✓ 已登入 GEMINI-OAUTH（token 有效）[/green]")
            if not Confirm.ask("要重新登入嗎？", default=False):
                console.print()
                return
            logout(provider)

        client_id_check = config.get("providers.gemini.client_id")
        if not client_id_check:
            console.print("[red]✗ 必須提供 Client ID 才能進行 OAuth 授權，請重試。[/red]")
            return

        console.print("即將開啟瀏覽器，請用你的 [bold]Google 帳號[/bold] 登入並授權…\n")
        input("按 Enter 繼續…")
        get_access_token(provider=provider)
        console.print()
    elif provider == PROVIDER_GROQ:
        console.print(Rule("② 設定 Groq Cloud"))
        console.print()
        current_key = config.get("providers.groq.api_key", "")
        key_hint = "*" * 10 + current_key[-4:] if current_key and len(current_key) > 4 else "未設定"
        console.print(f"[dim]目前 API Key: {key_hint}[/dim]")
        new_key = Prompt.ask("請輸入 Groq API Key (直接 Enter 保持原狀)", default="")
        if new_key.strip():
            config.set("providers.groq.api_key", new_key.strip())
        console.print("[green]✓ Groq API Key 設定已更新[/green]\n")
    elif provider == PROVIDER_GITHUB:
        console.print(Rule("② 設定 GitHub Models"))
        console.print()
        current_key = config.get("providers.github.api_key", "")
        key_hint = "*" * 10 + current_key[-4:] if current_key and len(current_key) > 4 else "未設定"
        console.print(f"[dim]目前 Personal Access Token (PAT): {key_hint}[/dim]")
        new_key = Prompt.ask("請輸入 GitHub PAT (直接 Enter 保持原狀)", default="")
        if new_key.strip():
            config.set("providers.github.api_key", new_key.strip())
        console.print("[green]✓ GitHub Models 設定已更新[/green]\n")
    elif provider == PROVIDER_SAMBANOVA:
        console.print(Rule("② 設定 SambaNova Cloud"))
        console.print()
        current_key = config.get("providers.sambanova.api_key", "")
        key_hint = "*" * 10 + current_key[-4:] if current_key and len(current_key) > 4 else "未設定"
        console.print(f"[dim]目前 API Key: {key_hint}[/dim]")
        new_key = Prompt.ask("請輸入 SambaNova API Key (直接 Enter 保持原狀)", default="")
        if new_key.strip():
            config.set("providers.sambanova.api_key", new_key.strip())
        console.print("[green]✓ SambaNova API Key 設定已更新[/green]\n")
    elif provider == PROVIDER_MISTRAL:
        console.print(Rule("② 設定 Mistral API"))
        console.print()
        current_key = config.get("providers.mistral.api_key", "")
        key_hint = "*" * 10 + current_key[-4:] if current_key and len(current_key) > 4 else "未設定"
        console.print(f"[dim]目前 API Key: {key_hint}[/dim]")
        new_key = Prompt.ask("請輸入 Mistral API Key (直接 Enter 保持原狀)", default="")
        if new_key.strip():
            config.set("providers.mistral.api_key", new_key.strip())
        console.print("[green]✓ Mistral API Key 設定已更新[/green]\n")


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
