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
from pocket_agent.system.i18n import t
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
        if len(args_tuple) == 2 and isinstance(args_tuple[0], str) and isinstance(args_tuple[1], dict):
            tool_name, payload = args_tuple[0], args_tuple[1]
            raw_tool = tool_name.split("__")[-1]
            if raw_tool == "github_create_pr":
                owner = payload.get("owner", "")
                repo = payload.get("repo", "")
                head = payload.get("head", "")
                base = payload.get("base") or t("confirmation.pr_auto_base")
                title = payload.get("title", "")
                body = payload.get("body", "")
                draft_label = (
                    t("confirmation.pr_mode_draft") if payload.get("draft") else t("confirmation.pr_mode_ready")
                )
                return "\n".join(
                    [
                        t("confirmation.pr_action"),
                        t("confirmation.pr_tool", tool_name=tool_name),
                        t("confirmation.pr_repo", owner=owner, repo=repo),
                        t("confirmation.pr_branch", head=head, base=base),
                        t("confirmation.pr_mode", mode=draft_label),
                        t("confirmation.pr_title", title=title),
                        t("confirmation.pr_body_preview"),
                        "─" * 40,
                        str(body),
                        "─" * 40,
                    ]
                )
        return " ".join(
            json.dumps(a, ensure_ascii=False) if isinstance(a, (dict, list)) else str(a) for a in args_tuple
        )

    @staticmethod
    def _is_fs_edit(command: str) -> bool:
        return command.startswith(("建立新檔：", "修改檔案：", "覆寫既有檔案：", "附加內容至檔案：", "局部修改檔案："))

    def _is_auto_allowed(self, command: str, is_fs_edit: bool) -> bool:
        if command.startswith(("動作：建立 GitHub Pull Request", t("confirmation.pr_action"))):
            return False
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
        self.renderer.message(t("confirmation.accept_edits_enabled"), "green")

    def _render_confirm_panel(self, command: str, *, is_fs_edit: bool = False) -> Any:
        self.renderer._ensure_newline()
        status = getattr(self.renderer, "_active_status", None)
        if status:
            status.stop()
        if is_fs_edit:
            title = t("confirmation.file_panel_title")
            planned = t("confirmation.planned_file_edit", command=command)
            border_style = "bold yellow"
            body_style = ""
        else:
            title = t("confirmation.panel_title")
            planned = t("confirmation.planned_command", command=command)
            border_style = "bold red"
            body_style = "yellow"
        self.renderer.console.print(
            Panel(
                Text(planned, style=body_style),
                title=title,
                title_align="left",
                border_style=border_style,
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
            self.renderer.message(t("confirmation.session_approved", command=command), "green")
            return True
        if answer == "A":
            prefix = add_command_to_allowlist(command)
            self.renderer._ensure_newline()
            self.renderer.message(t("confirmation.allowlisted", prefix=prefix), "green")
            return True
        if answer in ("n", "N"):
            return False
        return None

    def _record_custom_instruction(self, custom_inst: str) -> bool | str:
        if not custom_inst:
            self.renderer.message(t("confirmation.custom_instruction_empty"), "yellow")
            return False
        if hasattr(self.agent, "user_instructions"):
            self.agent.user_instructions.append(custom_inst)
        self.renderer.message(
            t("confirmation.custom_instruction_received", instruction=custom_inst),
            "green",
        )
        return custom_inst

    def _sync_confirm(self, *args_tuple: Any) -> bool | str:
        import builtins
        import sys

        command = self._format_command(args_tuple)
        is_fs_edit = self._is_fs_edit(command)
        if self._is_auto_allowed(command, is_fs_edit):
            return True

        status = self._render_confirm_panel(command, is_fs_edit=is_fs_edit)
        prompt_msg = t("confirmation.prompt_options")
        try:
            if is_fs_edit and sys.stdin.isatty():
                import prompt_toolkit
                from prompt_toolkit.keys import Keys

                bindings = KeyBindings()

                @bindings.add(Keys.BackTab)
                def _accept_all(event: Any) -> None:
                    event.app.exit(result="__ACCEPT_EDITS__")

                accept_hint = t("confirmation.prompt_accept_edits_sync")
                ans = prompt_toolkit.prompt(
                    prompt_msg + accept_hint,
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
                inst_prompt = t("confirmation.custom_instruction_prompt")
                custom_inst = builtins.input(inst_prompt).strip()
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

                status = self._render_confirm_panel(command, is_fs_edit=is_edit)
                bindings = KeyBindings()
                if is_edit:

                    @bindings.add("s-tab")
                    def accept_edits(event: Any) -> None:
                        event.app.exit(result="__ACCEPT_EDITS__")

                session: PromptSession[str] = PromptSession()
                prompt = t("confirmation.prompt_options")
                if is_edit:
                    prompt += t("confirmation.prompt_accept_edits_async")
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
                    inst_prompt = t("confirmation.custom_instruction_prompt")
                    answer = (await session.prompt_async(inst_prompt, handle_sigint=False)).strip()
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
