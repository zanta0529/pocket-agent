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

    @staticmethod
    def _format_command(args_tuple: tuple[Any, ...]) -> str:
        if len(args_tuple) == 1 and isinstance(args_tuple[0], str):
            return args_tuple[0]
        return " ".join(
            json.dumps(a, ensure_ascii=False) if isinstance(a, (dict, list)) else str(a) for a in args_tuple
        )

    @staticmethod
    def _is_fs_edit(command: str) -> bool:
        return command.startswith(("建立新檔：", "修改檔案：", "附加內容至檔案：", "局部修改檔案："))

    def _is_auto_allowed(self, command: str, is_fs_edit: bool) -> bool:
        if is_fs_edit and getattr(self.state, "accept_edits", False):
            return True
        if is_command_allowed(command):
            return True
        return command.strip() in self.state.session_approved_commands

    def _enable_accept_edits(self) -> None:
        self.state.accept_edits = True
        self.agent.accept_edits = True
        for sub in getattr(self.agent, "active_subagents", []):
            sub.accept_edits = True
        self.renderer.message("✓ 已啟用 Accept-edits 模式：本次對話自動允許檔案編輯", "green")

    def _render_confirm_panel(self, command: str) -> Any:
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
        return status

    def _apply_fixed_choice(self, answer: str, command: str) -> bool | None:
        """處理非自訂指示類的確認選項；若需進入自訂指示或直接輸入則回傳 None。"""
        if answer == "__ACCEPT_EDITS__":
            self._enable_accept_edits()
            return True
        if answer in ("y", "Y"):
            return True
        if answer == "a":
            self.state.session_approved_commands.add(command.strip())
            self.renderer.message(f"✓ 已同意本次對話中自動執行指令: {command}", "green")
            return True
        if answer == "A":
            prefix = add_command_to_allowlist(command)
            self.renderer._ensure_newline()
            self.renderer.message(f"✓ 已將「{prefix}」加入指令白名單，未來將自動執行此指令。", "green")
            return True
        if answer in ("n", "N"):
            return False
        return None

    def _record_custom_instruction(self, custom_inst: str) -> bool | str:
        if not custom_inst:
            self.renderer.message("未輸入指示，已取消執行指令。", "yellow")
            return False
        if hasattr(self.agent, "user_instructions"):
            self.agent.user_instructions.append(custom_inst)
        self.renderer.message(f"✓ 已收到您的指示：「{custom_inst}」，已轉達給 Agent 並加入規範。", "green")
        return custom_inst

    def _sync_confirm(self, *args_tuple: Any) -> bool | str:
        import builtins
        import sys

        command = self._format_command(args_tuple)
        is_fs_edit = self._is_fs_edit(command)
        if self._is_auto_allowed(command, is_fs_edit):
            return True

        status = self._render_confirm_panel(command)
        prompt_msg = "允許？[y: 單次 | a: 本次對話 | A: 永遠加入白名單 | c: 自訂指示 | N: 拒絕] "
        try:
            if is_fs_edit and sys.stdin.isatty():
                import prompt_toolkit
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
                ans = ans.strip() if ans is not None else ""
            else:
                ans = builtins.input(prompt_msg).strip()

            fixed = self._apply_fixed_choice(ans, command)
            if fixed is not None:
                return fixed
            if ans in ("c", "C"):
                self.renderer._ensure_newline()
                custom_inst = builtins.input("👉 請輸入給代理工具的指示：").strip()
                return self._record_custom_instruction(custom_inst)
            if ans:
                return self._record_custom_instruction(ans)
            return False
        except EOFError, KeyboardInterrupt:
            return False
        finally:
            if status:
                status.start()

    async def confirm(self, *args: Any) -> bool | str:
        scope = current_scope.get()
        task = asyncio.current_task()
        if scope is not None and task is not None:
            scope.prompts.add(task)
        status = None
        try:
            async with self.lock:
                check_cancelled()
                command = self._format_command(args)
                is_edit = self._is_fs_edit(command)
                if self._is_auto_allowed(command, is_edit):
                    return True

                status = self._render_confirm_panel(command)
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

                fixed = self._apply_fixed_choice(answer, command)
                if fixed is not None:
                    return fixed
                if answer in ("c", "C"):
                    answer = (await session.prompt_async("👉 請輸入給代理工具的指示：", handle_sigint=False)).strip()
                    check_cancelled()
                    return self._record_custom_instruction(answer)
                if answer:
                    return self._record_custom_instruction(answer)
                return False
        except EOFError:
            return False
        finally:
            if scope is not None and task is not None:
                scope.prompts.discard(task)
            if status and not (scope is not None and scope.cancelled.is_set()):
                status.start()
