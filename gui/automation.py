"""自動化管理頁：Slash Commands、Subagents、Hooks 三分頁。"""

from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk

from agent.commands import REPO_COMMANDS_DIR, list_commands
from agent.subagents import REPO_AGENTS_DIR, list_subagents
from agent.hooks import EVENTS, load_hooks, save_hooks


class AutomationView(ctk.CTkFrame):
    def __init__(self, parent, app):
        super().__init__(parent, fg_color="transparent")
        self.app = app
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._build()

    def _build(self):
        ctk.CTkLabel(
            self,
            text="自動化",
            font=ctk.CTkFont(size=20, weight="bold"),
        ).grid(row=0, column=0, padx=30, pady=(28, 6), sticky="w")

        self.tabs = ctk.CTkTabview(self)
        self.tabs.grid(row=1, column=0, sticky="nsew", padx=24, pady=(0, 16))

        self.tab_cmd = self.tabs.add("Slash 指令")
        self.tab_agent = self.tabs.add("子代理")
        self.tab_hook = self.tabs.add("Hooks")

        self._build_commands(self.tab_cmd)
        self._build_subagents(self.tab_agent)
        self._build_hooks(self.tab_hook)

    # ── Slash 指令 ───────────────────────────────

    def _build_commands(self, tab):
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)

        bar = ctk.CTkFrame(tab, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", pady=(6, 4))
        bar.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            bar,
            text="在 Chat 輸入 /指令名 即可展開成預存的提示詞。",
            text_color="gray",
            font=ctk.CTkFont(size=12),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(bar, text="＋ 新增", width=80, command=self._new_command).grid(row=0, column=1, sticky="e")

        self._cmd_list = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        self._cmd_list.grid(row=1, column=0, sticky="nsew")
        self._cmd_list.grid_columnconfigure(0, weight=1)

        self._cmd_editor = ctk.CTkFrame(tab)
        self._cmd_editor.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        self._cmd_editor.grid_columnconfigure(1, weight=1)
        self._cmd_editing: Path | None = None

        ctk.CTkLabel(self._cmd_editor, text="名稱").grid(row=0, column=0, padx=(12, 8), pady=(10, 4), sticky="w")
        self._cmd_name = ctk.CTkEntry(self._cmd_editor, placeholder_text="例：review（Chat 打 /review）")
        self._cmd_name.grid(row=0, column=1, padx=(0, 12), pady=(10, 4), sticky="ew")

        ctk.CTkLabel(self._cmd_editor, text="描述").grid(row=1, column=0, padx=(12, 8), pady=4, sticky="w")
        self._cmd_desc = ctk.CTkEntry(self._cmd_editor, placeholder_text="一句話說明")
        self._cmd_desc.grid(row=1, column=1, padx=(0, 12), pady=4, sticky="ew")

        ctk.CTkLabel(self._cmd_editor, text="模板").grid(row=2, column=0, padx=(12, 8), pady=4, sticky="nw")
        self._cmd_body = ctk.CTkTextbox(self._cmd_editor, height=120, wrap="word")
        self._cmd_body.grid(row=2, column=1, padx=(0, 12), pady=4, sticky="ew")

        ctk.CTkLabel(
            self._cmd_editor,
            text="$ARGUMENTS = /指令後的全部文字；$1 $2 = 各個參數",
            text_color="gray",
            font=ctk.CTkFont(size=11),
        ).grid(row=3, column=1, padx=(0, 12), pady=(0, 4), sticky="w")

        row = ctk.CTkFrame(self._cmd_editor, fg_color="transparent")
        row.grid(row=4, column=1, padx=(0, 12), pady=(4, 12), sticky="e")
        ctk.CTkButton(row, text="取消", width=70, fg_color=("gray60", "gray35"), command=self._hide_cmd_editor).grid(
            row=0, column=0, padx=(0, 8)
        )
        ctk.CTkButton(row, text="儲存", width=90, command=self._save_command).grid(row=0, column=1)
        self._cmd_editor.grid_remove()

        self._refresh_commands()

    def _refresh_commands(self):
        for w in self._cmd_list.winfo_children():
            w.destroy()
        cmds = list_commands()
        if not cmds:
            ctk.CTkLabel(self._cmd_list, text="還沒有指令。", text_color="gray").grid(row=0, column=0, pady=20)
            return
        for i, cmd in enumerate(cmds):
            card = ctk.CTkFrame(self._cmd_list)
            card.grid(row=i, column=0, sticky="ew", padx=4, pady=4)
            card.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(card, text=f"/{cmd['name']}", font=ctk.CTkFont(size=13, weight="bold"), anchor="w").grid(
                row=0, column=0, padx=14, pady=(8, 0), sticky="w"
            )
            ctk.CTkLabel(
                card, text=cmd["description"] or "（無描述）", text_color="gray", font=ctk.CTkFont(size=11), anchor="w"
            ).grid(row=1, column=0, padx=14, pady=(0, 8), sticky="w")
            ctk.CTkButton(card, text="編輯", width=56, command=lambda c=cmd: self._edit_command(c)).grid(
                row=0, column=1, rowspan=2, padx=2
            )
            ctk.CTkButton(
                card,
                text="刪除",
                width=56,
                fg_color=("gray60", "gray35"),
                hover_color=("#b3261e", "#8c1d18"),
                command=lambda c=cmd: self._delete_command(c),
            ).grid(row=0, column=2, rowspan=2, padx=(2, 10))

    def _new_command(self):
        self._cmd_editing = None
        self._cmd_name.delete(0, "end")
        self._cmd_desc.delete(0, "end")
        self._cmd_body.delete("1.0", "end")
        self._cmd_body.insert("1.0", "請針對以下內容進行處理：\n\n$ARGUMENTS")
        self._cmd_editor.grid()

    def _edit_command(self, cmd):
        from agent.commands import _parse

        meta, body = _parse(cmd["path"].read_text(encoding="utf-8"))
        self._cmd_editing = cmd["path"]
        self._cmd_name.delete(0, "end")
        self._cmd_name.insert(0, cmd["name"])
        self._cmd_desc.delete(0, "end")
        self._cmd_desc.insert(0, meta.get("description", ""))
        self._cmd_body.delete("1.0", "end")
        self._cmd_body.insert("1.0", body.strip())
        self._cmd_editor.grid()

    def _hide_cmd_editor(self):
        self._cmd_editor.grid_remove()
        self._cmd_editing = None

    def _save_command(self):
        name = self._cmd_name.get().strip()
        if not name or any(c in name for c in r'\/:*?"<>| '):
            messagebox.showwarning("名稱無效", "指令名不能含空白或特殊字元。")
            return
        REPO_COMMANDS_DIR.mkdir(parents=True, exist_ok=True)
        target = REPO_COMMANDS_DIR / f"{name}.md"
        body = self._cmd_body.get("1.0", "end-1c").strip()
        desc = self._cmd_desc.get().strip()
        target.write_text(f"---\ndescription: {desc}\n---\n\n{body}\n", encoding="utf-8")
        if self._cmd_editing and self._cmd_editing != target and self._cmd_editing.exists():
            self._cmd_editing.unlink()
        self._hide_cmd_editor()
        self._refresh_commands()

    def _delete_command(self, cmd):
        if messagebox.askyesno("刪除指令", f"確定刪除 /{cmd['name']}？"):
            cmd["path"].unlink(missing_ok=True)
            self._refresh_commands()

    # ── 子代理 ───────────────────────────────────

    def _build_subagents(self, tab):
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)

        bar = ctk.CTkFrame(tab, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", pady=(6, 4))
        bar.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            bar,
            text="子代理有獨立人設，主 agent 會在需要時自動委派任務。",
            text_color="gray",
            font=ctk.CTkFont(size=12),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(bar, text="＋ 新增", width=80, command=self._new_agent).grid(row=0, column=1, sticky="e")

        self._agent_list = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        self._agent_list.grid(row=1, column=0, sticky="nsew")
        self._agent_list.grid_columnconfigure(0, weight=1)

        self._agent_editor = ctk.CTkFrame(tab)
        self._agent_editor.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        self._agent_editor.grid_columnconfigure(1, weight=1)
        self._agent_editing: Path | None = None

        ctk.CTkLabel(self._agent_editor, text="名稱").grid(row=0, column=0, padx=(12, 8), pady=(10, 4), sticky="w")
        self._agent_name = ctk.CTkEntry(self._agent_editor, placeholder_text="例：researcher")
        self._agent_name.grid(row=0, column=1, padx=(0, 12), pady=(10, 4), sticky="ew")

        ctk.CTkLabel(self._agent_editor, text="描述").grid(row=1, column=0, padx=(12, 8), pady=4, sticky="w")
        self._agent_desc = ctk.CTkEntry(self._agent_editor, placeholder_text="何時該委派給它（主 agent 靠這句判斷）")
        self._agent_desc.grid(row=1, column=1, padx=(0, 12), pady=4, sticky="ew")

        ctk.CTkLabel(self._agent_editor, text="人設 Prompt").grid(row=2, column=0, padx=(12, 8), pady=4, sticky="nw")
        self._agent_body = ctk.CTkTextbox(self._agent_editor, height=110, wrap="word")
        self._agent_body.grid(row=2, column=1, padx=(0, 12), pady=4, sticky="ew")

        row = ctk.CTkFrame(self._agent_editor, fg_color="transparent")
        row.grid(row=3, column=1, padx=(0, 12), pady=(4, 12), sticky="e")
        ctk.CTkButton(row, text="取消", width=70, fg_color=("gray60", "gray35"), command=self._hide_agent_editor).grid(
            row=0, column=0, padx=(0, 8)
        )
        ctk.CTkButton(row, text="儲存", width=90, command=self._save_agent).grid(row=0, column=1)
        self._agent_editor.grid_remove()

        self._refresh_subagents()

    def _refresh_subagents(self):
        for w in self._agent_list.winfo_children():
            w.destroy()
        agents = list_subagents()
        if not agents:
            ctk.CTkLabel(self._agent_list, text="還沒有子代理。", text_color="gray").grid(row=0, column=0, pady=20)
            return
        for i, sa in enumerate(agents):
            card = ctk.CTkFrame(self._agent_list)
            card.grid(row=i, column=0, sticky="ew", padx=4, pady=4)
            card.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(card, text=sa["name"], font=ctk.CTkFont(size=13, weight="bold"), anchor="w").grid(
                row=0, column=0, padx=14, pady=(8, 0), sticky="w"
            )
            ctk.CTkLabel(
                card, text=sa["description"] or "（無描述）", text_color="gray", font=ctk.CTkFont(size=11), anchor="w"
            ).grid(row=1, column=0, padx=14, pady=(0, 8), sticky="w")
            ctk.CTkButton(card, text="編輯", width=56, command=lambda a=sa: self._edit_agent(a)).grid(
                row=0, column=1, rowspan=2, padx=2
            )
            ctk.CTkButton(
                card,
                text="刪除",
                width=56,
                fg_color=("gray60", "gray35"),
                hover_color=("#b3261e", "#8c1d18"),
                command=lambda a=sa: self._delete_agent(a),
            ).grid(row=0, column=2, rowspan=2, padx=(2, 10))

    def _new_agent(self):
        self._agent_editing = None
        self._agent_name.delete(0, "end")
        self._agent_desc.delete(0, "end")
        self._agent_body.delete("1.0", "end")
        self._agent_body.insert("1.0", "You are a focused assistant specialized in …")
        self._agent_editor.grid()

    def _edit_agent(self, sa):
        self._agent_editing = sa["path"]
        self._agent_name.delete(0, "end")
        self._agent_name.insert(0, sa["name"])
        self._agent_desc.delete(0, "end")
        self._agent_desc.insert(0, sa["description"])
        self._agent_body.delete("1.0", "end")
        self._agent_body.insert("1.0", sa["prompt"])
        self._agent_editor.grid()

    def _hide_agent_editor(self):
        self._agent_editor.grid_remove()
        self._agent_editing = None

    def _save_agent(self):
        name = self._agent_name.get().strip()
        if not name or any(c in name for c in r'\/:*?"<>| '):
            messagebox.showwarning("名稱無效", "子代理名稱不能含空白或特殊字元。")
            return
        desc = self._agent_desc.get().strip()
        if not desc:
            messagebox.showwarning("缺少描述", "描述是主 agent 判斷何時委派的依據。")
            return
        REPO_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
        target = REPO_AGENTS_DIR / f"{name}.md"
        body = self._agent_body.get("1.0", "end-1c").strip()
        target.write_text(f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}\n", encoding="utf-8")
        if self._agent_editing and self._agent_editing != target and self._agent_editing.exists():
            self._agent_editing.unlink()
        self._hide_agent_editor()
        self._refresh_subagents()

    def _delete_agent(self, sa):
        if messagebox.askyesno("刪除子代理", f"確定刪除「{sa['name']}」？"):
            sa["path"].unlink(missing_ok=True)
            self._refresh_subagents()

    # ── Hooks ────────────────────────────────────

    def _build_hooks(self, tab):
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)

        bar = ctk.CTkFrame(tab, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", pady=(6, 4))
        bar.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            bar,
            text="在特定事件自動執行 CLI 指令（可用 $POCKETAGENT_* 環境變數取得上下文）。",
            text_color="gray",
            font=ctk.CTkFont(size=12),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(bar, text="＋ 新增", width=80, command=self._new_hook).grid(row=0, column=1, sticky="e")

        self._hook_list = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        self._hook_list.grid(row=1, column=0, sticky="nsew")
        self._hook_list.grid_columnconfigure(0, weight=1)

        self._hook_editor = ctk.CTkFrame(tab)
        self._hook_editor.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        self._hook_editor.grid_columnconfigure(1, weight=1)
        self._hook_editing: int | None = None

        ctk.CTkLabel(self._hook_editor, text="名稱").grid(row=0, column=0, padx=(12, 8), pady=(10, 4), sticky="w")
        self._hook_name = ctk.CTkEntry(self._hook_editor, placeholder_text="例：自動commit")
        self._hook_name.grid(row=0, column=1, padx=(0, 12), pady=(10, 4), sticky="ew")

        ctk.CTkLabel(self._hook_editor, text="觸發事件").grid(row=1, column=0, padx=(12, 8), pady=4, sticky="w")
        self._hook_event = ctk.CTkOptionMenu(self._hook_editor, values=list(EVENTS))
        self._hook_event.grid(row=1, column=1, padx=(0, 12), pady=4, sticky="w")

        ctk.CTkLabel(self._hook_editor, text="指令").grid(row=2, column=0, padx=(12, 8), pady=4, sticky="nw")
        self._hook_cmd = ctk.CTkTextbox(self._hook_editor, height=70, wrap="word")
        self._hook_cmd.grid(row=2, column=1, padx=(0, 12), pady=4, sticky="ew")

        ctk.CTkLabel(
            self._hook_editor,
            text="事件：before_send／after_response／before_tool／after_tool",
            text_color="gray",
            font=ctk.CTkFont(size=11),
        ).grid(row=3, column=1, padx=(0, 12), pady=(0, 4), sticky="w")

        row = ctk.CTkFrame(self._hook_editor, fg_color="transparent")
        row.grid(row=4, column=1, padx=(0, 12), pady=(4, 12), sticky="e")
        ctk.CTkButton(row, text="取消", width=70, fg_color=("gray60", "gray35"), command=self._hide_hook_editor).grid(
            row=0, column=0, padx=(0, 8)
        )
        ctk.CTkButton(row, text="儲存", width=90, command=self._save_hook).grid(row=0, column=1)
        self._hook_editor.grid_remove()

        self._refresh_hooks()

    def _refresh_hooks(self):
        for w in self._hook_list.winfo_children():
            w.destroy()
        hooks = load_hooks()
        if not hooks:
            ctk.CTkLabel(self._hook_list, text="還沒有 hook。", text_color="gray").grid(row=0, column=0, pady=20)
            return
        for i, hook in enumerate(hooks):
            card = ctk.CTkFrame(self._hook_list)
            card.grid(row=i, column=0, sticky="ew", padx=4, pady=4)
            card.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(
                card,
                text=f"{hook.get('name', '(未命名)')}  [{hook.get('event')}]",
                font=ctk.CTkFont(size=13, weight="bold"),
                anchor="w",
            ).grid(row=0, column=0, padx=14, pady=(8, 0), sticky="w")
            ctk.CTkLabel(
                card, text=hook.get("command", ""), text_color="gray", font=ctk.CTkFont(size=11), anchor="w"
            ).grid(row=1, column=0, padx=14, pady=(0, 8), sticky="w")
            ctk.CTkButton(card, text="編輯", width=56, command=lambda idx=i: self._edit_hook(idx)).grid(
                row=0, column=1, rowspan=2, padx=2
            )
            ctk.CTkButton(
                card,
                text="刪除",
                width=56,
                fg_color=("gray60", "gray35"),
                hover_color=("#b3261e", "#8c1d18"),
                command=lambda idx=i: self._delete_hook(idx),
            ).grid(row=0, column=2, rowspan=2, padx=(2, 10))

    def _new_hook(self):
        self._hook_editing = None
        self._hook_name.delete(0, "end")
        self._hook_event.set(EVENTS[1])  # after_response
        self._hook_cmd.delete("1.0", "end")
        self._hook_editor.grid()

    def _edit_hook(self, idx):
        hooks = load_hooks()
        if idx >= len(hooks):
            return
        hook = hooks[idx]
        self._hook_editing = idx
        self._hook_name.delete(0, "end")
        self._hook_name.insert(0, hook.get("name", ""))
        self._hook_event.set(hook.get("event", EVENTS[0]))
        self._hook_cmd.delete("1.0", "end")
        self._hook_cmd.insert("1.0", hook.get("command", ""))
        self._hook_editor.grid()

    def _hide_hook_editor(self):
        self._hook_editor.grid_remove()
        self._hook_editing = None

    def _save_hook(self):
        command = self._hook_cmd.get("1.0", "end-1c").strip()
        if not command:
            messagebox.showwarning("缺少指令", "請輸入要執行的 CLI 指令。")
            return
        entry = {
            "name": self._hook_name.get().strip() or "(未命名)",
            "event": self._hook_event.get(),
            "command": command,
        }
        hooks = load_hooks()
        if self._hook_editing is not None and self._hook_editing < len(hooks):
            hooks[self._hook_editing] = entry
        else:
            hooks.append(entry)
        save_hooks(hooks)
        self._hide_hook_editor()
        self._refresh_hooks()

    def _delete_hook(self, idx):
        hooks = load_hooks()
        if idx < len(hooks) and messagebox.askyesno("刪除 Hook", f"確定刪除「{hooks[idx].get('name')}」？"):
            hooks.pop(idx)
            save_hooks(hooks)
            self._refresh_hooks()

    # ── App callback ─────────────────────────────

    def on_show(self):
        self._refresh_commands()
        self._refresh_subagents()
        self._refresh_hooks()
