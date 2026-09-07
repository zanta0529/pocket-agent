#!/usr/bin/env python3
"""Pocket Agent — GUI（預設）或 CLI（--cli）模式"""

import sys

# Windows asyncio fix（uvicorn OAuth callback server 需要 SelectorEventLoop）
if sys.platform == "win32":
    import asyncio
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import asyncio
from pathlib import Path

import click
from dotenv import load_dotenv

from agent.paths import env_path

load_dotenv(env_path())


# ── GUI 模式 ──────────────────────────────────────


def launch_gui():
    try:
        from gui.app import App
    except ModuleNotFoundError as e:
        if "tkinter" in str(e):
            print("\n" + "=" * 60)
            print("[警告] 系統缺少 GUI 必備的 'tkinter' 函式庫。")
            print("這通常是因為目前的 Python 為 Embeddable（可攜版），未包含 Tcl/Tk 元件。")
            print("=" * 60)
            try:
                choice = input("是否直接切換為終端機模式 (CLI) 啟動？[Y/n]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                choice = "n"
            if choice in ("", "y", "yes"):
                asyncio.run(_run_cli("You are a helpful assistant."))
                return
            sys.exit(1)
        raise
    app = App()
    app.mainloop()


# ── CLI 模式 ──────────────────────────────────────


async def _run_cli(system_prompt: str):
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel

    from agent.auth import fetch_available_models, get_model, get_openai_client, load_config
    from agent.core import Agent
    from agent.mcp_manager import MCPManager

    console = Console()
    try:
        client = get_openai_client()
    except EnvironmentError as e:
        console.print(f"[red]{e}[/red]")
        return

    mcp = MCPManager()
    console.print("[dim]啟動 MCP servers...[/dim]")
    await mcp.start()

    tools = mcp.list_tools()
    if tools:
        console.print(f"[green]✓ 已載入 {len(tools)} 個工具：{', '.join(tools)}[/green]")

    model = get_model()
    cfg = load_config()
    provider = cfg.get("provider", "openai")

    available = fetch_available_models(provider)
    if available:
        console.print(f"[dim]✓ {provider.upper()} 可用模型清單：{', '.join(available)}[/dim]")
        if model not in available:
            console.print(f"[yellow]⚠️  設定的模型 '{model}' 不在 {provider.upper()} 支援清單中[/yellow]")
            model = available[0]
            console.print(f"[bold green]✓ 自動為您切換至可用模型：{model}[/bold green]")
    else:
        console.print(f"[dim]（無法從 {provider.upper()} 後端取得可用模型清單，使用當前設定）[/dim]")

    agent = Agent(client=client, mcp=mcp, model=model, system_prompt=system_prompt)

    cmd_hints = "/file <路徑>  附加檔案    /clear  清除對話    /logout  登出    /quit  離開"
    if provider == "openai":
        cmd_hints = "/file <路徑>  附加檔案    /image <描述>  生圖    /clear  清除對話    /logout  登出    /quit  離開"

    console.print(
        Panel(
            f"[bold]Pocket Agent[/bold]（{model}）\n{cmd_hints}",
            border_style="blue",
        )
    )

    pending_files: list[str] = []

    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.formatted_text import HTML

    class SlashCommandCompleter(Completer):
        def __init__(self):
            from agent.commands import list_commands

            self.list_commands = list_commands
            self.builtins = [
                {"name": "file", "description": "附加檔案"},
                {"name": "clear", "description": "清除對話"},
                {"name": "logout", "description": "登出"},
                {"name": "quit", "description": "離開"},
            ]
            if provider == "openai":
                self.builtins.insert(1, {"name": "image", "description": "生圖"})

        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            if text.startswith("/"):
                word_before = document.get_word_before_cursor(WORD=True)
                if word_before.startswith("/"):
                    prefix = word_before[1:].lower()

                    # 內建指令
                    for cmd in self.builtins:
                        if cmd["name"].lower().startswith(prefix):
                            yield Completion(
                                cmd["name"],
                                start_position=-len(prefix),
                                display=cmd["name"],
                                display_meta=cmd["description"],
                            )

                    # 自訂指令 (Skills)
                    for cmd in self.list_commands():
                        if cmd["name"].lower().startswith(prefix):
                            yield Completion(
                                cmd["name"],
                                start_position=-len(prefix),
                                display=cmd["name"],
                                display_meta=f"[skill] {cmd['description']}",
                            )

    session = PromptSession(completer=SlashCommandCompleter(), complete_while_typing=True)

    try:
        while True:
            try:
                raw = await session.prompt_async(HTML("<b><ansicyan>You: </ansicyan></b>"))
            except (EOFError, KeyboardInterrupt):
                break

            if not raw.strip():
                continue
            match raw.strip():
                case "/quit":
                    break
                case "/clear":
                    agent.clear_history()
                    console.print("[dim]✓ 清除[/dim]")
                    continue
                case "/logout":
                    from agent.auth import logout

                    logout()
                    break

            if raw.startswith("/file "):
                p = raw[6:].strip()
                if Path(p).exists():
                    pending_files.append(p)
                    console.print(f"[dim]✓ 已附加：{p}[/dim]")
                else:
                    console.print(f"[red]找不到：{p}[/red]")
                continue

            if raw.startswith("/image "):
                if provider != "openai":
                    console.print(
                        f"[yellow]⚠ /image 指令目前僅支援 OPENAI 服務提供者（目前為 {provider.upper()}）[/yellow]"
                    )
                    continue
                from agent.imagegen import generate_image

                img_prompt = raw[7:].strip()
                refs, pending_files = pending_files, []  # 已附加的圖片當參考圖（圖生圖）
                with console.status("[dim]生圖中（可能需要 1-2 分鐘）...[/dim]", spinner="dots"):
                    try:
                        out = await generate_image(
                            img_prompt,
                            output_path=Path("generated_images") / "image.png",
                            input_images=refs or None,
                        )
                    except Exception as e:
                        console.print(f"[red]{e}[/red]")
                        continue
                console.print(f"[green]✓ 已存檔：{out}[/green]")
                continue

            atts, pending_files = pending_files, []

            # 處理自訂 slash command 展開
            if raw.startswith("/"):
                from agent.commands import expand_command, get_command

                expanded = expand_command(raw)
                if expanded is None:
                    name = raw[1:].split(" ", 1)[0]
                    if get_command(name) is None:
                        console.print(f"[yellow]⚠ 找不到指令 /{name}[/yellow]")
                        continue
                else:
                    raw = expanded
                    console.print("[dim italic]（已展開指令模板）[/dim italic]")

            with console.status("[dim]思考中...[/dim]", spinner="dots"):
                try:
                    thinking, reply = await agent.chat(raw, attachments=atts)
                except Exception as e:
                    console.print(f"[red]{e}[/red]")
                    continue
            if thinking:
                console.print(f"[dim italic]💭 {thinking[:200]}{'…' if len(thinking) > 200 else ''}[/dim italic]")

            from rich.console import Console

            console.print()
            console.print(Panel(Markdown(reply), title="[bold green]Agent[/bold green]", border_style="green"))
            console.print()
    finally:
        await mcp.stop()


# ── Entry point ───────────────────────────────────


@click.group(invoke_without_command=True)
@click.pass_context
@click.option("--cli", "--console", is_flag=True, help="終端機模式（不開 GUI）")
@click.option("--gui", is_flag=True, help="強制圖形介面模式（預設）")
@click.option("--system", default="You are a helpful assistant.", help="System prompt（CLI 模式用）")
def main(ctx, cli, gui, system):
    """Pocket Agent — 用 OpenAI 帳號登入，不需要 API Key"""
    if ctx.invoked_subcommand is None:
        # 如果使用者指定了 --console (或 --cli)，就跑文字模式
        if cli:
            asyncio.run(_run_cli(system))
        else:
            launch_gui()


@main.command()
def setup():
    """互動式設定精靈（CLI）"""
    from agent.wizard import run_setup

    run_setup()


@main.command()
def logout():
    """登出（清除本機 token）"""
    from agent.auth import logout as _lo

    _lo()


@main.command()
@click.argument("prompt")
@click.option("-o", "--output", default="image.png", help="輸出檔名（存在時自動加 -v2）")
@click.option("-i", "--input", "inputs", multiple=True, help="參考圖（可多個，圖生圖/編輯用）")
@click.option("--size", default="auto", type=click.Choice(["auto", "1024x1024", "1536x1024", "1024x1536"]))
@click.option("--quality", default="auto", type=click.Choice(["auto", "low", "medium", "high"]))
def image(prompt, output, inputs, size, quality):
    """用 gpt-image-2 生圖（走 ChatGPT 訂閱配額）"""
    from rich.console import Console

    from agent.imagegen import generate_image

    console = Console()
    with console.status("[dim]生圖中（可能需要 1-2 分鐘）...[/dim]", spinner="dots"):
        out = asyncio.run(
            generate_image(
                prompt,
                output_path=output,
                input_images=list(inputs) or None,
                size=size,
                quality=quality,
            )
        )
    console.print(f"[green]✓ 已存檔：{out}[/green]")


if __name__ == "__main__":
    main()
