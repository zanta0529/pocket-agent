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

    from rich import box

    cmd_hints = (
        "[bold #B8860B]/file[/bold #B8860B] [dim]<路徑>[/dim] 附加檔案  [dim]│[/dim]  "
        "[bold #B8860B]/clear[/bold #B8860B] 清除對話  [dim]│[/dim]  "
        "[bold #B8860B]/logout[/bold #B8860B] 登出  [dim]│[/dim]  "
        "[bold #B8860B]/quit[/bold #B8860B] 離開"
    )
    if provider == "openai":
        cmd_hints = (
            "[bold #B8860B]/file[/bold #B8860B] [dim]<路徑>[/dim] 附加檔案  [dim]│[/dim]  "
            "[bold #B8860B]/image[/bold #B8860B] [dim]<描述>[/dim] 生圖  [dim]│[/dim]  "
            "[bold #B8860B]/clear[/bold #B8860B] 清除對話  [dim]│[/dim]  "
            "[bold #B8860B]/logout[/bold #B8860B] 登出  [dim]│[/dim]  "
            "[bold #B8860B]/quit[/bold #B8860B] 離開"
        )

    console.print(
        Panel(
            cmd_hints,
            title=f"🤖 [bold #B8860B]Pocket Agent[/bold #B8860B] [bold #D3D3D3]({model})[/bold #D3D3D3]",
            title_align="left",
            box=box.ROUNDED,
            border_style="#B8860B",
            padding=(1, 2),
        )
    )

    pending_files: list[str] = []
    workspace_dirs: list[str] = [str(Path.cwd())]

    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.formatted_text import HTML

    class SlashCommandCompleter(Completer):
        def __init__(self):
            from agent.commands import list_commands

            self.list_commands = list_commands
            self.builtins = [
                {"name": "file", "description": "附加檔案"},
                {"name": "add-dir", "description": "附加目錄內檔案"},
                {"name": "clear", "description": "清除對話"},
                {"name": "logout", "description": "登出"},
                {"name": "quit", "description": "離開"},
                {"name": "model", "description": "切換當次服務提供者與模型"},
            ]
            if provider == "openai":
                self.builtins.insert(1, {"name": "image", "description": "生圖"})

        def _get_path_completions(self, prefix, only_directories=False):
            import os

            dirname, basename = os.path.split(prefix)
            search_dir = dirname if dirname else "."

            if not os.path.isdir(search_dir):
                return

            try:
                for entry in os.scandir(search_dir):
                    if basename.lower() in entry.name.lower():
                        is_dir = entry.is_dir()
                        if only_directories and not is_dir:
                            continue

                        display_meta = "Directory" if is_dir else "File"
                        insert_text = entry.name + (os.sep if is_dir else "")

                        yield Completion(
                            insert_text, start_position=-len(basename), display=insert_text, display_meta=display_meta
                        )
            except PermissionError:
                pass

        def get_completions(self, document, complete_event):
            text = document.text_before_cursor

            # 路徑自動補齊：/add-dir
            if text.startswith("/add-dir "):
                yield from self._get_path_completions(text[9:], only_directories=True)
                return

            # 路徑自動補齊：/file
            if text.startswith("/file "):
                yield from self._get_path_completions(text[6:], only_directories=False)
                return

            # 路徑自動補齊：@
            import re

            m = re.search(r"(?:^|\s)@(\S*)$", text)
            if m:
                yield from self._get_path_completions(m.group(1), only_directories=False)
                return

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

    def get_bottom_toolbar():
        import shutil

        term_width = shutil.get_terminal_size().columns

        line = "─" * term_width
        left_text = " ↑/↓ History · Tab Complete"
        model_str = f" {model} "
        pad_len = max(0, term_width - len(left_text) - len(model_str))

        bottom_text = f"{left_text}{' ' * pad_len}{model_str}"
        return HTML(f'<style fg="#555555">{line}\n</style><style fg="gray">{bottom_text}</style>')

    from prompt_toolkit.key_binding import KeyBindings

    kb = KeyBindings()

    @kb.add("tab")
    def _(event):
        b = event.app.current_buffer
        if b.complete_state:
            # 結束補齊狀態，讓游標停在選定的項目後面
            b.complete_state = None
        else:
            b.start_completion(select_first=False)

    session = PromptSession(
        completer=SlashCommandCompleter(),
        complete_while_typing=True,
        bottom_toolbar=get_bottom_toolbar,
        key_bindings=kb,
        erase_when_done=True,
    )

    try:
        while True:
            try:
                console.print(f"[color(#555555)]{'─' * console.width}[/color(#555555)]")
                raw = await session.prompt_async(HTML("<b><ansiblue>></ansiblue></b> "))
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

            console.print()
            console.print(Panel(raw, title="[bold cyan]User[/bold cyan]", border_style="cyan"))

            if raw.startswith("/file "):
                p = raw[6:].strip()
                if Path(p).exists():
                    pending_files.append(p)
                    console.print(f"[dim]✓ 已附加：{p}[/dim]")
                else:
                    console.print(f"[red]找不到：{p}[/red]")
                continue

            if raw.startswith("/add-dir "):
                d = raw[9:].strip()
                p = Path(d)
                if not p.is_dir():
                    console.print(f"[red]找不到目錄：{d}[/red]")
                    continue

                abs_path = str(p.resolve())
                if abs_path not in workspace_dirs:
                    workspace_dirs.append(abs_path)
                console.print(f"[dim]L Added {abs_path} to workspace[/dim]")
                continue

            import re

            mentions = re.findall(r"(?:^|\s)@(\S+)", raw)
            found_mentions = False
            for m in mentions:
                if Path(m).exists() and Path(m).is_file():
                    if m not in pending_files:
                        if not found_mentions:
                            console.print()
                            found_mentions = True
                        pending_files.append(m)
                        console.print(f"[dim]✓ 已附加：{m}[/dim]")

            if found_mentions:
                console.print()

            if raw.startswith("/model"):
                parts = raw.split()
                if len(parts) >= 3:
                    p = parts[1].lower()
                    m = parts[2]
                    from agent.auth import get_openai_client

                    try:
                        agent.client = get_openai_client(provider=p, model=m)
                        agent.model = m
                        console.print(f"[green]✓ 當次對話已切換為：{p.upper()} - {m}[/green]")
                    except Exception as e:
                        console.print(f"[red]⚠ 切換模型失敗：{e}[/red]")
                else:
                    from agent.auth import fetch_available_models

                    console.print("[yellow]用法: /model <provider> <model_name>[/yellow]")
                    try:
                        openai_models = fetch_available_models("openai") or ["gpt-4o", "gpt-4o-mini"]
                        console.print(f"  [bold]OPENAI 模型:[/bold] {', '.join(openai_models)}")
                    except Exception:
                        pass
                    try:
                        ollama_models = fetch_available_models("ollama") or ["llama3"]
                        console.print(f"  [bold]OLLAMA 模型:[/bold] {', '.join(ollama_models)}")
                    except Exception:
                        pass
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

            def _cli_on_stream(kind, data):
                if kind == "system":
                    console.print(f"[dim]{data}[/dim]")
                elif kind == "read":
                    console.print(f"[bold green]●[/bold green] [dim]Read ({data})[/dim]")

            agent.on_stream = _cli_on_stream

            with console.status("[dim]思考中...[/dim]", spinner="dots"):
                try:
                    thinking, reply, usage_str = await agent.chat(raw, attachments=atts)
                except Exception as e:
                    console.print(f"[red]{e}[/red]")
                    continue
            if thinking:
                console.print(f"[dim italic]💭 {thinking[:200]}{'…' if len(thinking) > 200 else ''}[/dim italic]")

            from rich.console import Console

            console.print()
            if usage_str:
                console.print(f"[bold green]▶[/bold green] [dim]{usage_str}[/dim]")
            console.print(Panel(Markdown(reply), title="[bold green]Agent[/bold green]", border_style="green"))
            console.print()
    finally:
        await mcp.stop()


# ── Entry point ───────────────────────────────────


@click.group(invoke_without_command=True)
@click.pass_context
@click.option("--cli", "--console", "--tui", is_flag=True, help="終端機模式（不開 GUI）")
@click.option("--gui", is_flag=True, help="強制圖形介面模式（預設）")
@click.option("--system", default="You are a helpful assistant.", help="System prompt（CLI 模式用）")
def main(ctx, cli, gui, system):
    """Pocket Agent — 用 OpenAI 帳號登入，不需要 API Key"""
    if ctx.invoked_subcommand is None:
        # 如果使用者指定了 --console (或 --cli 或 --tui)，就跑文字模式
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
