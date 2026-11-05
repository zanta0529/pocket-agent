#!/usr/bin/env python3
"""Pocket Agent — 命令列 (CLI) 模式"""

import asyncio
import runpy
import sys
from pathlib import Path

import click
from dotenv import load_dotenv

from pocket_agent.system.compat import setup_asyncio_policy
from pocket_agent.system.paths import env_path

if len(sys.argv) >= 3 and sys.argv[1] == "--internal-mcp-server":
    _script = sys.argv[2]
    sys.argv = [_script, *sys.argv[3:]]
    runpy.run_path(_script, run_name="__main__")
    raise SystemExit(0)
elif len(sys.argv) >= 2 and sys.argv[1].endswith(".py") and Path(sys.argv[1]).is_file():
    _script = str(Path(sys.argv[1]).resolve())
    sys.argv = [_script, *sys.argv[2:]]
    runpy.run_path(_script, run_name="__main__")
    raise SystemExit(0)

setup_asyncio_policy()
load_dotenv(env_path())


# ── Entry point ───────────────────────────────────


@click.group(invoke_without_command=True)
@click.pass_context
@click.option("--cli", "--console", "--tui", is_flag=True, help="終端機模式（舊相容參數，現為預設）")
@click.option("--system", default="You are a helpful assistant.", help="System prompt")
@click.option("--resume", is_flag=True, help="啟動時自動恢復最近一次 session")
def main(ctx, cli, system, resume):
    """Pocket Agent — 用 OpenAI 帳號登入，不需要 API Key"""
    from pocket_agent.system.updater import update_manager

    update_manager.consume_launcher_env()
    if ctx.invoked_subcommand is None:
        from pocket_agent.frontend import CliFrontend

        strategy = CliFrontend()
        strategy.run(system, resume=resume)


@main.command()
def setup():
    """互動式設定精靈（CLI）"""
    from pocket_agent.system.wizard import run_setup

    run_setup()


@main.command()
def logout():
    """登出（清除本機 token）"""
    from pocket_agent.system.auth import logout as _lo

    _lo()


@main.command()
def update():
    """檢查並套用 GitHub 最新更新（僅限 Modern Portable 版）"""
    from rich.console import Console

    from pocket_agent.system.updater import EXIT_CODE_UPDATE_APPLY_ONLY, get_current_build_info, update_manager

    console = Console()
    if not update_manager.is_supported():
        console.print("[yellow]線上更新僅支援 Modern Portable 打包版；開發環境請直接使用 git pull 更新。[/yellow]")
        return

    try:
        staged = asyncio.run(
            update_manager.download_and_stage(
                on_status=lambda msg: console.print(f"[cyan]{msg}[/cyan]"),
            )
        )
    except Exception as exc:
        console.print(f"[red]✗ 更新失敗：{exc}[/red]")
        raise SystemExit(1) from exc

    if staged is None:
        curr = get_current_build_info()
        label = curr.commit[:7] or curr.version
        console.print(f"[green]✓ 目前已是最新版本 ({label})。[/green]")
        return

    applied, exit_code = update_manager.apply_staged_update_for_cli(restart=False, cwd=Path.cwd(), foreground=True)
    if applied:
        if exit_code == EXIT_CODE_UPDATE_APPLY_ONLY:
            console.print(f"[green]✓ 最新更新 ({staged.short_sha}) 已準備完成，正在原視窗完成檔案替換…[/green]")
            raise SystemExit(exit_code)
        console.print(f"[green]✓ 最新更新 ({staged.short_sha}) 已準備完成，正在背景完成檔案替換。[/green]")


@main.command()
@click.argument("prompt")
@click.option("-o", "--output", default="image.png", help="輸出檔名（存在時自動加 -v2）")
@click.option("-i", "--input", "inputs", multiple=True, help="參考圖（可多個，圖生圖/編輯用）")
@click.option("--size", default="auto", type=click.Choice(["auto", "1024x1024", "1536x1024", "1024x1536"]))
@click.option("--quality", default="auto", type=click.Choice(["auto", "low", "medium", "high"]))
def image(prompt, output, inputs, size, quality):
    """用 gpt-image-2 生圖（走 ChatGPT 訂閱配額）"""
    from rich.console import Console

    from pocket_agent.tools.image import generate_image

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
