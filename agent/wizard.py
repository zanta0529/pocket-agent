"""設定精靈 — python main.py setup"""

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.rule import Rule
from rich.text import Text

from .auth import get_access_token, load_config, logout, save_config
from .paths import mcp_config_path

console = Console()

MODELS = [
    ("gpt-4o", "最強，適合複雜任務"),
    ("gpt-4o-mini", "快速省錢，適合日常對話"),
    ("gpt-4-turbo", "舊旗艦，支援長文"),
    ("o1-mini", "推理型，擅長數學/程式"),
]


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
        _step_login()
        _step_model()
        _step_mcp()
    except KeyboardInterrupt:
        console.print("\n[yellow]已取消。[/yellow]")
        return

    console.print()
    console.print(Rule(style="green"))
    console.print("[bold green]✓ 設定完成！[/bold green]  執行 [cyan]python main.py[/cyan] 開始使用。")
    console.print()


# ── Step 1：登入 OpenAI ───────────────────────────


def _step_login() -> None:
    console.print(Rule("① 登入 OpenAI 帳號"))
    console.print()

    import os

    client_id = os.getenv("OPENAI_CLIENT_ID")
    if not client_id:
        console.print(
            "[yellow]⚠ 尚未設定 OPENAI_CLIENT_ID。[/yellow]\n\n"
            "請先：\n"
            "  1. 複製 [dim].env.example[/dim] → [dim].env[/dim]\n"
            "  2. 前往 [link=https://platform.openai.com/settings/organization/apps]"
            "https://platform.openai.com/settings/organization/apps[/link] 建立 OAuth App\n"
            "  3. 把 [bold]Client ID[/bold] 填入 [dim].env[/dim] 的 OPENAI_CLIENT_ID\n"
            "  4. Redirect URI 填 [bold]http://localhost:8899/callback[/bold]\n"
        )
        console.print("[dim]設定好 .env 後重新執行 python main.py setup[/dim]")
        raise SystemExit(0)

    # 有 client_id → 試著取得 token
    from .auth import _is_expired, _load_token

    token = _load_token()
    if token and not _is_expired(token):
        console.print("[green]✓ 已登入（token 有效）[/green]")
        if not Confirm.ask("要重新登入嗎？", default=False):
            console.print()
            return
        logout()

    console.print("即將開啟瀏覽器，請用你的 [bold]OpenAI 帳號[/bold] 登入並授權…\n")
    input("按 Enter 繼續…")
    get_access_token()  # 觸發瀏覽器 OAuth
    console.print()


# ── Step 2：模型 ──────────────────────────────────


def _step_model() -> None:
    console.print(Rule("② 預設模型"))
    console.print()

    current = load_config().get("model", "gpt-4o")
    console.print(f"[dim]目前：{current}[/dim]\n")

    for i, (name, desc) in enumerate(MODELS, 1):
        tag = " [green]← 目前[/green]" if name == current else ""
        console.print(f"  [cyan]{i}[/cyan]. [bold]{name}[/bold]  {desc}{tag}")
    console.print(f"  [cyan]{len(MODELS) + 1}[/cyan]. 自訂")
    console.print()

    choice = Prompt.ask("選擇", choices=[str(i) for i in range(1, len(MODELS) + 2)], default="1")
    idx = int(choice) - 1
    model = MODELS[idx][0] if idx < len(MODELS) else Prompt.ask("模型名稱").strip()

    cfg = load_config()
    cfg["model"] = model
    save_config(cfg)
    console.print(f"[green]✓ 已設定為 {model}[/green]\n")


# ── Step 3：MCP ───────────────────────────────────


def _step_mcp() -> None:
    console.print(Rule("③ MCP 工具（可略過）"))
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
