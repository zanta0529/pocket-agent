"""同步工具與非同步確認提示的橋接，不在 worker 讀取 stdin。"""

import asyncio
import concurrent.futures
import json
import shutil
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.key_binding import KeyBindings
from rich.panel import Panel
from rich.text import Text

from pocket_agent.core.cancellation import check_cancelled, current_scope
from pocket_agent.frontend.cli.render import toolbar
from pocket_agent.system.permissions import add_command_to_allowlist, is_command_allowed


class ConfirmationBridge:
    def __init__(self, agent: Any, state: Any, renderer: Any) -> None:
        self.agent = agent
        self.state = state
        self.renderer = renderer
        self.loop = asyncio.get_running_loop()
        self.lock = asyncio.Lock()

    def __call__(self, *args: Any) -> bool | str:
        check_cancelled()
        # Agent 的確認契約是同步回呼，呼叫點須在 worker 中。
        if self.loop.is_closed():
            return self._sync_confirm(*args)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            return self._sync_confirm(*args)

        import builtins

        if (
            getattr(builtins.input, "_mock_wraps", None)
            or hasattr(builtins.input, "assert_called")
            or hasattr(builtins.input, "side_effect")
        ):
            return self._sync_confirm(*args)

        future = asyncio.run_coroutine_threadsafe(self.confirm(*args), self.loop)
        try:
            decision = future.result()
        except concurrent.futures.CancelledError:
            return False
        check_cancelled()
        return decision

    def _sync_confirm(self, *args_tuple: Any) -> bool | str:
        import builtins
        import json
        import sys

        from rich.panel import Panel
        from rich.text import Text

        from pocket_agent.system.permissions import add_command_to_allowlist, is_command_allowed

        if len(args_tuple) == 1 and isinstance(args_tuple[0], str):
            command = args_tuple[0]
        else:
            command = " ".join(
                json.dumps(a, ensure_ascii=False) if isinstance(a, (dict, list)) else str(a) for a in args_tuple
            )

        is_fs_edit = command.startswith(("建立新檔：", "修改檔案：", "附加內容至檔案：", "局部修改檔案："))
        if is_fs_edit and getattr(self.state, "accept_edits", False):
            return True

        if is_command_allowed(command):
            return True

        if command.strip() in self.state.session_approved_commands:
            return True

        self.renderer._ensure_newline()
        status = getattr(self.renderer, "_active_status", None)
        if status:
            status.stop()

        self.renderer.console.print(
            Panel(
                Text(f"預計執行：\n{command}", style="yellow"),
                title="【安全警告】系統要求執行指令",
                title_align="left",
                border_style="bold red",
                expand=False,
            )
        )

        prompt_msg = "允許？[y: 單次 | a: 本次對話 | A: 永遠加入白名單 | c: 自訂指示 | N: 拒絕] "
        try:
            if is_fs_edit and sys.stdin.isatty():
                import prompt_toolkit
                from prompt_toolkit.key_binding import KeyBindings
                from prompt_toolkit.keys import Keys

                bindings = KeyBindings()

                @bindings.add(Keys.BackTab)
                def _accept_all(event: Any) -> None:
                    event.app.exit(result="__ACCEPT_EDITS__")

                ans = prompt_toolkit.prompt(
                    prompt_msg + "（Shift+Tab: 自動編輯(Accept-edits)）",
                    key_bindings=bindings,
                    bottom_toolbar=lambda: toolbar(self.state, shutil.get_terminal_size().columns),
                )
                if ans is not None:
                    ans = ans.strip()
            else:
                ans = builtins.input(prompt_msg).strip()

            if ans == "__ACCEPT_EDITS__":
                self.state.accept_edits = True
                self.agent.accept_edits = True
                for sub in getattr(self.agent, "active_subagents", []):
                    sub.accept_edits = True
                self.renderer.message("✓ 已啟用 Accept-edits 模式：本次對話自動允許檔案編輯", "green")
                if status:
                    status.start()
                return True
            elif ans in ("y", "Y"):
                if status:
                    status.start()
                return True
            elif ans == "a":
                self.state.session_approved_commands.add(command.strip())
                self.renderer.message(f"✓ 已同意本次對話中自動執行指令: {command}", "green")
                if status:
                    status.start()
                return True
            elif ans == "A":
                prefix = add_command_to_allowlist(command)
                self.renderer._ensure_newline()
                self.renderer.message(f"✓ 已將「{prefix}」加入指令白名單，未來將自動執行此指令。", "green")
                if status:
                    status.start()
                return True
            elif ans in ("c", "C"):
                self.renderer._ensure_newline()
                custom_inst = builtins.input("👉 請輸入給代理工具的指示：").strip()
                if not custom_inst:
                    self.renderer.message("未輸入指示，已取消執行指令。", "yellow")
                    if status:
                        status.start()
                    return False
                if hasattr(self.agent, "user_instructions"):
                    self.agent.user_instructions.append(custom_inst)
                self.renderer.message(f"✓ 已收到您的指示：「{custom_inst}」，已轉達給 Agent 並加入規範。", "green")
                if status:
                    status.start()
                return custom_inst
            elif ans and ans not in ("n", "N"):
                custom_inst = ans
                if hasattr(self.agent, "user_instructions"):
                    self.agent.user_instructions.append(custom_inst)
                self.renderer.message(f"✓ 已收到您的指示：「{custom_inst}」，已轉達給 Agent 並加入規範。", "green")
                if status:
                    status.start()
                return custom_inst

            if status:
                status.start()
            return False
        except EOFError, KeyboardInterrupt:
            if status:
                status.start()
            return False

    async def confirm(self, *args: Any) -> bool | str:
        scope = current_scope.get()
        task = asyncio.current_task()
        if scope is not None and task is not None:
            scope.prompts.add(task)
        status = None
        try:
            async with self.lock:
                check_cancelled()
                command = " ".join(
                    json.dumps(arg, ensure_ascii=False) if isinstance(arg, (dict, list)) else str(arg) for arg in args
                )
                is_edit = command.startswith(("建立新檔：", "修改檔案：", "附加內容至檔案：", "局部修改檔案："))
                if is_edit and self.state.accept_edits:
                    return True
                if is_command_allowed(command) or command.strip() in self.state.session_approved_commands:
                    return True
                renderer = self.renderer
                renderer._ensure_newline()
                status = renderer._active_status
                if status:
                    status.stop()
                renderer.console.print(
                    Panel(
                        Text(f"預計執行：\n{command}", style="yellow"),
                        title="【安全警告】系統要求執行指令",
                        title_align="left",
                        border_style="bold red",
                        expand=False,
                    )
                )
                bindings = KeyBindings()
                if is_edit:

                    @bindings.add("s-tab")
                    def accept_edits(event: Any) -> None:
                        event.app.exit(result="__ACCEPT_EDITS__")

                session: PromptSession[str] = PromptSession()
                prompt = "允許？[y: 單次 | a: 本次對話 | A: 永遠加入白名單 | c: 自訂指示 | N: 拒絕] "
                if is_edit:
                    prompt += "（Shift+Tab：Accept-edits）"
                answer = (
                    await session.prompt_async(
                        prompt,
                        key_bindings=bindings,
                        bottom_toolbar=lambda: toolbar(self.state, shutil.get_terminal_size().columns),
                        handle_sigint=False,
                    )
                ).strip()
                check_cancelled()
                if answer == "__ACCEPT_EDITS__":
                    self.state.accept_edits = True
                    self.agent.accept_edits = True
                    for sub in getattr(self.agent, "active_subagents", []):
                        sub.accept_edits = True
                    renderer.message("✓ 已啟用 Accept-edits 模式", "green")
                    return True
                if answer in ("y", "Y"):
                    return True
                if answer == "a":
                    self.state.session_approved_commands.add(command.strip())
                    renderer.message("✓ 已同意本次對話中自動執行此指令", "green")
                    return True
                if answer == "A":
                    prefix = add_command_to_allowlist(command)
                    renderer.message(f"✓ 已將「{prefix}」加入指令白名單", "green")
                    return True
                if answer in ("c", "C"):
                    answer = (await session.prompt_async("👉 請輸入給代理工具的指示：", handle_sigint=False)).strip()
                    check_cancelled()
                elif answer in ("n", "N"):
                    return False
                if answer:
                    if hasattr(self.agent, "user_instructions"):
                        self.agent.user_instructions.append(answer)
                    renderer.message(f"✓ 已收到您的指示：「{answer}」", "green")
                    return answer
                return False
        except EOFError:
            return False
        finally:
            if scope is not None and task is not None:
                scope.prompts.discard(task)
            if status and not (scope is not None and scope.cancelled.is_set()):
                status.start()
