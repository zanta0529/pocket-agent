import asyncio
import logging
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING

from pocket_agent.frontend.base import FrontendStrategy

log = logging.getLogger("pocket-agent")

if TYPE_CHECKING:
    from pocket_agent.core.agent import Agent
    from pocket_agent.frontend.cli.commands import CommandResult
    from pocket_agent.frontend.cli.input import CliInput
    from pocket_agent.frontend.cli.render import CliRenderer
    from pocket_agent.frontend.cli.state import CliState


def _autosave_session(agent: "Agent", state: "CliState", renderer: "CliRenderer", silent: bool = False) -> None:
    """對話結束或每輪完成時自動存檔（失敗不中斷流程）。"""
    from pocket_agent.core.sessions import save_session

    if not getattr(agent, "history", None):
        return
    if getattr(agent, "is_sensitive_session", False) is True:
        if not silent:
            renderer.message("【安全提醒】本工作階段涉及敏感主機診斷，基於資安政策不予持久化落盤。", "yellow")
        return
    try:
        save_session(state.session_id, agent.history)
    except Exception:
        pass  # 存檔失敗不中斷退出


async def send_turn(
    agent: "Agent",
    state: "CliState",
    renderer: "CliRenderer",
    raw: str,
    result: "CommandResult",
) -> None:
    from pocket_agent.frontend.cli.input import references

    try:
        agent.on_stream = renderer.event
        state.add_files(references(result.prompt))
        snapshot = state.snapshot()

        display = result.display_text if getattr(result, "display_text", None) else raw
        renderer.user(display)

        started = monotonic()

        from pocket_agent.frontend.cli.interrupts import OperationInterrupted, run_operation

        def stopping() -> None:
            agent.request_stop()
            renderer._stop_status()
            renderer.message("⏸ 停止要求已送出，等待進行中的工作收尾…", "yellow")

        if result.action == "image":
            from rich.status import Status

            status = Status("生圖中（可能需要 1-2 分鐘）…", spinner="dots", console=renderer.console)
            renderer._active_status = status
            status.start()
            try:
                out_path = Path("generated_images") / "image.png"
                if hasattr(agent, "generate_image"):
                    coro = agent.generate_image(result.prompt, output_path=out_path, input_images=snapshot or None)
                else:
                    from pocket_agent.tools.image import generate_image

                    coro = generate_image(result.prompt, output_path=out_path, input_images=snapshot or None)
                reply = await run_operation(coro, stopping)
            finally:
                renderer._stop_status()
        else:
            # 串流模式：啟動思考中 spinner，等第一個 text 事件停止它
            from rich.status import Status

            status = Status("思考中…", spinner="dots", console=renderer.console)
            renderer._active_status = status
            status.start()

            try:
                _, reply, _ = await run_operation(agent.chat(result.prompt, attachments=snapshot), stopping)
            finally:
                # 確保串流狀態停止，並卸載回呼防止殘餘事件
                agent.on_stream = None
                renderer._stop_status()

    except OperationInterrupted, asyncio.CancelledError:
        agent.request_stop()
        agent.on_stream = None
        renderer._stop_status()
        state.last_failed_input = raw
        renderer.message("⏹ 已停止主代理", "yellow")
        return
    except Exception as exc:
        agent.on_stream = None
        renderer._stop_status()
        state.last_failed_input = raw
        renderer._ensure_newline()

        from rich.markdown import Markdown
        from rich.panel import Panel

        err_text = str(exc)

        panel = Panel(
            Markdown(err_text),
            title="⚠️ 本輪未完成 (錯誤)",
            title_align="left",
            border_style="bold red",
            expand=False,
        )
        renderer.console.print()
        renderer.console.print(panel)
        renderer.message("待送附件保留；本輪可能部分完成，重送前請檢查結果。未自動重試。", "yellow")
        await asyncio.to_thread(_autosave_session, agent, state, renderer, True)
        return

    # Normal return is the boundary; the core may return error text normally.
    state.consume(snapshot)
    await asyncio.to_thread(_autosave_session, agent, state, renderer, True)
    if result.action == "image":
        if not hasattr(agent, "generate_image") and hasattr(agent, "activity") and getattr(agent, "activity", None):
            agent.activity.record_file_change(reply, "added", existed_before=False)
        renderer.message(f"已存檔：{reply}", "green")
    else:
        turn_tokens = getattr(agent, "total_tokens", 0)
        state.session_tokens += turn_tokens
        budget_obj = getattr(agent, "_budget", None)
        if budget_obj is not None and getattr(budget_obj, "window", 0) > 0:
            state.context_window = budget_obj.window

        renderer.reply(reply, monotonic() - started, turn_tokens, streamed=True)

        # 背景非同步更新額度資訊，避免網路請求阻塞本輪結算與耗時計算
        if hasattr(agent, "get_quota"):

            async def _refresh_quota() -> None:
                try:
                    quota = await agent.get_quota()
                    if quota:
                        state.quota_supported = quota.is_supported
                        state.quota_limit = quota.limit
                        state.quota_remaining = quota.remaining
                except Exception:
                    pass

            bg_task = asyncio.create_task(_refresh_quota())
            bg_tasks = getattr(agent, "_bg_tasks", None)
            if isinstance(bg_tasks, set):
                bg_tasks.add(bg_task)
                bg_task.add_done_callback(bg_tasks.discard)


async def conversation(
    agent: "Agent",
    state: "CliState",
    reader: "CliInput",
    renderer: "CliRenderer",
    tools: list[str],
) -> None:
    from pocket_agent.frontend.cli.commands import CommandHandler
    from pocket_agent.frontend.cli.interrupts import OperationInterrupted, run_operation

    def stopping() -> None:
        agent.request_stop()
        renderer._stop_status()
        renderer.message("⏸ 停止要求已送出，等待進行中的工作收尾…", "yellow")

    handler = CommandHandler(agent, state, reader, renderer, tools)
    agent.on_stream = renderer.event
    last_sigint_time = 0.0
    while True:
        try:
            raw = await reader.read(default=state.last_failed_input)
        except EOFError:
            break
        except KeyboardInterrupt:
            now = monotonic()
            if now - last_sigint_time < 1.5:
                renderer.message("收到中斷訊號，正在離開…", "yellow")
                break
            last_sigint_time = now
            state.last_failed_input = ""
            renderer.message("再按一次 Ctrl+C 離開，或輸入 /exit（待送附件保留）。", "yellow")
            continue
        state.last_failed_input = ""
        last_sigint_time = 0.0
        if not raw.strip():
            continue
        try:
            result = await run_operation(handler.dispatch(raw), stopping)
        except OperationInterrupted:
            renderer._stop_status()
            renderer.message("⏹ 操作已收尾，未自動重送", "yellow")
            continue
        except Exception as exc:
            renderer.message(f"指令未執行：{exc}", "red")
            continue
        if result.action == "quit":
            break
        elif result.action in ("chat", "image"):
            try:
                await run_operation(send_turn(agent, state, renderer, raw, result), stopping)
            except OperationInterrupted:
                # Ctrl+C 在 send_turn 等待期間：中止當前輪次，不退出
                agent.request_stop()
                agent.on_stream = None
                renderer._stop_status()
                state.last_failed_input = raw
                renderer.message("⏹ 已停止", "yellow")

    # 對話結束，自動存檔
    _autosave_session(agent, state, renderer)


class CliFrontend(FrontendStrategy):
    """Scrollback-first CLI with separate input and rendering phases."""

    def run(self, system_prompt: str = "You are a helpful assistant.", resume: bool = False) -> None:
        exit_code = asyncio.run(self._run_cli(system_prompt, resume=resume))
        if exit_code:
            raise SystemExit(exit_code)

    async def _run_cli(self, system_prompt: str, resume: bool = False) -> int:
        import pathlib

        from pocket_agent.system.compat import setup_win_sigint_workaround

        sigint_task = setup_win_sigint_workaround()
        update_task = None
        housekeeping_task = None

        from rich.console import Console

        from pocket_agent.core.agent import Agent
        from pocket_agent.core.cancellation import run_sync
        from pocket_agent.extensions.mcp import MCPManager
        from pocket_agent.frontend.cli.input import CliInput
        from pocket_agent.frontend.cli.render import CliRenderer
        from pocket_agent.frontend.cli.state import CliState
        from pocket_agent.system.auth import fetch_available_models, get_model, get_provider_client, load_config

        renderer = CliRenderer(Console())

        # CLI 模式：抑制 StreamHandler INFO，避免 log 訊息干擾 terminal 輸出
        # 若開發者明確設定 DEBUG 等級則尊重，不抑制
        from pocket_agent.system.config import config

        _lvl = config.get("system.log_level", "WARNING").upper()
        if _lvl == "WARNING":
            for h in logging.getLogger("pocket-agent").handlers:
                if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                    h.setLevel(logging.WARNING)

        # F1.5 / F2：由核心統一載入全域規範 (instructions.md) 與工作區規範 (AGENTS.md)
        from pocket_agent.core.instructions import load_system_instructions

        system_prompt, loaded_sources = await load_system_instructions(system_prompt)

        client = None
        mcp = None
        agent = None
        state = None
        update_task: asyncio.Task | None = None
        update_exit_code = 0
        try:
            renderer.banner()

            for label, src_path in loaded_sources:
                renderer.message(f"已載入{label}：{src_path}")

            from rich.prompt import Prompt

            from pocket_agent.system.config import AuthException, MissingKeyException, config

            max_retries = int(config.get("cli.max_retries", 3))
            retry_count = 0
            while retry_count < max_retries:
                try:
                    client = get_provider_client()
                    state = CliState(load_config().get("active_provider", "openai"), get_model())
                    try:
                        available = await asyncio.to_thread(fetch_available_models, state.provider)
                    except Exception:
                        available = []

                    if available and state.model not in available:
                        renderer.message(f"設定模型 {state.model} 不在清單；改用 {available[0]}。", "yellow")
                        state.model = available[0]
                    elif not available:
                        renderer.message("無法取得模型清單，使用目前設定。", "yellow")
                    break  # 成功取得 client 與模型，跳出重試迴圈
                except MissingKeyException as e:
                    renderer.message(f"【尚未設定金鑰】{e}", "yellow")
                    renderer.message("請先離開，並在終端機執行 `pocket-agent setup` 完成 API Key 設定。")
                    return 0
                except AuthException as e:
                    retry_count += 1
                    renderer.message(f"【安全鎖】{e}", "yellow")
                    password = Prompt.ask("[bold cyan]請輸入主密碼解鎖[/bold cyan]", password=True)
                    if config.unlock(password):
                        renderer.message("解鎖成功，重新連線中...", "green")
                    else:
                        renderer.message(f"密碼錯誤或解鎖失敗。剩餘重試次數：{max_retries - retry_count}", "red")

                    if retry_count >= max_retries:
                        renderer.message("重試次數過多，啟動中止。", "red")
                        return 0

            mcp = MCPManager()
            startup = await mcp.start(on_start=lambda count: renderer.message("正在初始化 MCP 服務…"))
            if startup.configured_servers == 0:
                renderer.message("未設定 MCP 服務，略過初始化。")
            elif startup.failed_servers == 0:
                if startup.tool_count > 0:
                    renderer.message(
                        f"MCP 已就緒：已連線 {startup.ready_servers} 個服務，載入 {startup.tool_count} 個工具。"
                    )
                else:
                    renderer.message(f"MCP 已連線 {startup.ready_servers} 個服務，目前無可用工具。")
            elif startup.ready_servers > 0:
                renderer.message(
                    f"MCP 部分就緒：{startup.ready_servers} 個服務初始化成功，載入 {startup.tool_count} 個工具；"
                    f"{startup.failed_servers} 個服務初始化失敗。",
                    "yellow",
                )
            else:
                renderer.message("MCP 初始化失敗，請檢查設定或上述錯誤訊息。", "red")

            agent = Agent(
                client=client, mcp=mcp, model=state.model, system_prompt=system_prompt, provider=state.provider
            )
            state.reasoning_effort = getattr(agent, "reasoning_effort", "auto")

            # F3：讓 renderer 能取得 model 名稱顯示在標頭
            renderer._state = state

            from pocket_agent.frontend.cli.confirmation import ConfirmationBridge

            agent.on_confirm = ConfirmationBridge(agent, state, renderer)

            def _cli_prompt_mcp_conn(server_name: str, missing_keys: list[str]) -> dict[str, str]:
                status = renderer._active_status
                if status:
                    status.stop()

                renderer._ensure_newline()
                renderer.console.print()
                renderer.message(
                    f"🔐 [{server_name}] 請補充未設定的遠端連線資訊（僅存於本機記憶體，不會傳送給 AI 模型）：",
                    "bold cyan",
                )
                collected: dict[str, str] = {}
                labels = {
                    "RHEL_HOST": ("RHEL 主機位址 (IP / Host)", None, False),
                    "RHEL_SSH_PORT": ("SSH 連接埠 (Port)", "22", False),
                    "RHEL_SSH_USER": ("SSH 登入帳號 (Username)", None, False),
                    "RHEL_SSH_PASSWORD": ("SSH 登入密碼 (Password)", None, True),
                    "WIN_HOST": ("Windows 主機位址 (IP / Host)", None, False),
                    "WIN_RDP_PORT": ("RDP 遠端桌面連接埠 (Port)", "3389", False),
                    "WIN_RDP_USER": ("Windows 登入帳號 (Username / Domain\\User)", None, False),
                    "WIN_RDP_PASSWORD": ("Windows 登入密碼 (Password)", None, True),
                    "MYSQL_HOST": ("MySQL/MariaDB 主機位址 (IP / Host)", None, False),
                    "MYSQL_PORT": ("MySQL 連接埠 (Port)", "3306", False),
                    "MYSQL_USER": ("MySQL 登入帳號 (Username)", None, False),
                    "MYSQL_PASSWORD": ("MySQL 登入密碼 (Password)", None, True),
                    "MYSQL_DATABASE": ("MySQL 資料庫名稱 (Database)", None, False),
                    "MSSQL_HOST": ("SQL Server 主機位址 (IP / Host)", None, False),
                    "MSSQL_PORT": ("SQL Server 連接埠 (Port)", "1433", False),
                    "MSSQL_USER": ("SQL Server 登入帳號 (Username)", None, False),
                    "MSSQL_PASSWORD": ("SQL Server 登入密碼 (Password)", None, True),
                    "MSSQL_DATABASE": ("SQL Server 資料庫名稱 (Database)", None, False),
                    "GITHUB_TOKEN": ("GitHub Personal Access Token (GITHUB_TOKEN)", None, True),
                }
                try:
                    for key in missing_keys:
                        label, default_val, is_pw = labels.get(key, (key, None, "PASSWORD" in key))
                        if default_val is not None:
                            val = Prompt.ask(
                                f"[bold yellow]  {label}[/bold yellow]",
                                default=default_val,
                                password=is_pw,
                                console=renderer.console,
                            )
                        else:
                            val = Prompt.ask(
                                f"[bold yellow]  {label}[/bold yellow]",
                                password=is_pw,
                                console=renderer.console,
                            )
                        collected[key] = val if is_pw else val.strip()
                    return collected
                except EOFError, KeyboardInterrupt:
                    return collected
                finally:
                    if status:
                        status.start()

            mcp.on_prompt_conn = _cli_prompt_mcp_conn

            tools = list(mcp.list_tools())
            renderer.welcome(state, tools)

            # F7：--resume 自動恢復最近 session
            if resume:
                from pocket_agent.core.sessions import list_sessions, load_session

                sessions = list_sessions()
                if sessions:
                    latest = load_session(sessions[0]["id"])
                    if latest and latest.get("history"):
                        agent.history = latest["history"]
                        state.session_id = latest["id"]
                        renderer.message(
                            f"已恢復：{latest.get('title', '（無標題）')} · {len(agent.history)} 則訊息",
                            "green",
                        )
                else:
                    renderer.message("沒有找到歷史 session，以全新對話啟動。", "dim")

            # 啟動時背景執行自動 Housekeeping (具備 24 小時冷卻期，無感執行)
            from pocket_agent.system.housekeeping import default_housekeeper
            from pocket_agent.system.updater import update_manager

            housekeeping_task = asyncio.create_task(run_sync(default_housekeeper.run_cleanup, force=False))

            if update_manager.is_auto_update_enabled():

                async def _bg_auto_update() -> None:
                    try:
                        staged = await update_manager.download_and_stage()
                        if staged is not None and state is not None:
                            state.staged_update_version = staged.short_sha
                    except asyncio.CancelledError:
                        raise
                    except Exception as bg_exc:
                        log.debug("背景自動更新略過：%s", bg_exc)

                update_task = asyncio.create_task(_bg_auto_update())

            # F6：Ctrl+L 清屏後重印 banner
            await conversation(
                agent, state, CliInput(state, agent=agent, on_clear_screen=renderer.banner), renderer, tools
            )
        except Exception as exc:
            renderer.message(f"CLI 執行失敗：{exc}", "red")
        finally:
            for bg_t in (sigint_task, update_task, housekeeping_task):
                if bg_t is not None and not bg_t.done():
                    bg_t.cancel()
                    try:
                        await bg_t
                    except asyncio.CancelledError, Exception:
                        pass

            # 收斂 Agent（停止子代理、收斂背景任務，避免 client 關閉後呼叫）
            if agent is not None and hasattr(agent, "shutdown"):
                try:
                    await asyncio.wait_for(agent.shutdown(timeout=3.0), timeout=3.5)
                except Exception as agent_err:
                    log.warning("Agent 關閉逾時或失敗：%s", agent_err)

            # MCP 關閉失敗不阻礙關閉 Client
            try:
                if mcp is not None:
                    await asyncio.wait_for(mcp.stop(timeout=3.0), timeout=3.5)
            except (asyncio.TimeoutError, Exception) as mcp_err:
                log.warning("MCP 服務停止逾時或失敗：%s", mcp_err)
            finally:
                active = agent.client if agent is not None else client
                if active is not None:
                    try:
                        await asyncio.wait_for(active.close(), timeout=2.0)
                    except Exception:
                        renderer.message("模型連線清理失敗。", "yellow")

            from pocket_agent.system.updater import update_manager as _updater

            restart_after_update = bool(state and state.pending_update_restart)
            if _updater.is_supported() and (restart_after_update or _updater.is_auto_update_enabled()):
                try:
                    _, update_exit_code = _updater.apply_staged_update_for_cli(
                        restart=restart_after_update,
                        cwd=pathlib.Path.cwd(),
                        foreground=restart_after_update,
                    )
                except Exception as apply_exc:
                    log.warning("啟動更新套用腳本失敗：%s", apply_exc)
        return update_exit_code
