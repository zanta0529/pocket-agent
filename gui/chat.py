"""聊天介面：訊息泡泡、推理展開區塊、輸入欄、附加檔案。"""

import json
import logging
import queue
import threading
from pathlib import Path
from types import SimpleNamespace

import customtkinter as ctk
from PIL import Image
from tkinterdnd2 import DND_FILES

from gui.components.autocomplete import AutocompleteMenu
from gui.components.ime_helper import IMEHelperMixin
from gui.components.scroll_helper import ScrollManager

log = logging.getLogger("pocket-agent")


class ChatView(ctk.CTkFrame, IMEHelperMixin):
    def __init__(self, parent, app):
        super().__init__(parent, fg_color="transparent")

        self.app = app

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self._pending_files: list[str] = []
        self._chip_images: list = []  # 縮圖的 CTkImage 參照，防止被 GC

        # 對話重建與捲動排程控制。
        self._rendering_history = False
        self._scroll_jobs: list[str] = []
        self._render_generation = 0

        self._build()

    # ─────────────────────────────────────────────
    # 介面建立
    # ─────────────────────────────────────────────

    def _build(self):

        # 外層對話訊息捲動區
        self.msg_frame = ctk.CTkScrollableFrame(
            self,
            fg_color=("gray95", "gray12"),
            scrollbar_fg_color="transparent",
            scrollbar_button_color=("gray80", "gray22"),
            scrollbar_button_hover_color=("gray65", "gray40"),
            corner_radius=0,
        )
        self.msg_frame.grid(
            row=0,
            column=0,
            sticky="nsew",
        )
        self.msg_frame.grid_columnconfigure(0, weight=1)
        self.scroll_manager = ScrollManager(self.msg_frame)

        # 附件顯示列
        self.attach_bar = ctk.CTkLabel(
            self,
            text="",
            font=ctk.CTkFont(size=11),
            text_color="gray",
            anchor="w",
        )
        self.attach_bar.grid(
            row=1,
            column=0,
            sticky="ew",
            padx=14,
            pady=(4, 0),
        )

        # 附件縮圖列（有附件時才顯示）
        self.attach_frame = ctk.CTkFrame(
            self,
            fg_color="transparent",
        )
        self.attach_frame.grid(
            row=2,
            column=0,
            sticky="w",
            padx=14,
            pady=(2, 0),
        )
        self.attach_frame.grid_remove()

        # 輸入區
        input_frame = ctk.CTkFrame(
            self,
            corner_radius=14,
            border_width=1,
            border_color=("gray80", "gray25"),
            fg_color=("gray95", "gray14"),
        )
        input_frame.grid(
            row=3,
            column=0,
            sticky="ew",
            padx=16,
            pady=(4, 16),
        )
        input_frame.grid_columnconfigure(0, weight=1)

        self.input = ctk.CTkTextbox(
            input_frame,
            height=68,
            wrap="word",
            font=ctk.CTkFont(size=self._chat_font_size()),
            border_width=0,
            fg_color="transparent",
        )

        self.input.grid(
            row=0,
            column=0,
            padx=12,
            pady=(12, 4),
            sticky="ew",
        )

        # 微軟倉頡 Ctrl 組合鍵中文標點修正
        tk_text = self.input._textbox

        tk_text.bind(
            "<Control-KeyPress>",
            self._on_ctrl_ime_character,
            add="+",
        )

        tk_text.bind(
            "<Return>",
            self._on_enter,
        )
        tk_text.bind(
            "<Shift-Return>",
            lambda event: None,
        )

        # ── Slash Command Autocomplete ──────────────
        self.autocomplete_menu = AutocompleteMenu(self.winfo_toplevel(), self.input, self._on_autocomplete_select)
        tk_text.bind("<KeyRelease>", self._on_key_release)
        tk_text.bind("<Up>", self._on_autocomplete_up)
        tk_text.bind("<Down>", self._on_autocomplete_down)
        tk_text.bind("<Tab>", self._on_autocomplete_tab)
        tk_text.bind("<FocusOut>", lambda e: self.autocomplete_menu.hide())

        # ── IME 修正（Windows 中文輸入法）──────────
        self._ime_state = {
            "comp": "",
            "base": "",
            "pending": None,
            "skip_until": 0.0,
        }

        for sequence in (
            "<KeyPress-Escape>",
            "<KeyPress-BackSpace>",
        ):
            self.input.bind(
                sequence,
                self._ime_mark_cancel,
                add="+",
            )

        self.input.bind(
            "<Control-space>",
            self._on_ctrl_space,
        )

        self.after(
            50,
            self._ime_poll,
        )

        # Ctrl+V：剪貼簿若為圖片，直接附加
        self.input.bind(
            "<Control-v>",
            self._on_paste,
        )

        icon_path = Path(__file__).parent / "assets" / "icon_attach.png"

        self.attach_icon = ctk.CTkImage(
            light_image=Image.open(icon_path),
            dark_image=Image.open(icon_path),
            size=(22, 22),
        )

        # ── 底部工具列 ────────────────
        bottom_bar = ctk.CTkFrame(
            input_frame,
            fg_color="transparent",
        )
        bottom_bar.grid(
            row=1,
            column=0,
            sticky="ew",
            padx=12,
            pady=(0, 12),
        )
        bottom_bar.grid_columnconfigure(1, weight=1)

        # 模型 / 推理強度快速選單
        from agent.auth import get_model, load_config
        from gui.settings import REASONING

        opts_row = ctk.CTkFrame(bottom_bar, fg_color="transparent")
        opts_row.grid(row=0, column=0, sticky="w")

        cfg = load_config()
        model = cfg.get("model") or get_model()
        model_values = [model]

        self.model_var = ctk.StringVar(value=model)
        self.model_menu = ctk.CTkOptionMenu(
            opts_row,
            values=model_values,
            variable=self.model_var,
            width=130,
            height=32,
            font=ctk.CTkFont(size=12),
            fg_color=("gray85", "gray22"),
            button_color=("gray75", "gray32"),
            button_hover_color=("gray65", "gray42"),
            text_color=("gray10", "gray90"),
            command=lambda _v: self._apply_chat_options(),
        )
        self.model_menu.grid(row=0, column=0, padx=(0, 8))

        self._effort_short = [(key, label.split("（")[0]) for key, label in REASONING]
        effort = cfg.get("reasoning_effort", "medium")
        short = next((s for k, s in self._effort_short if k == effort), "Medium")

        self.effort_var = ctk.StringVar(value=short)
        self.effort_menu = ctk.CTkOptionMenu(
            opts_row,
            values=[s for _k, s in self._effort_short],
            variable=self.effort_var,
            width=90,
            height=32,
            font=ctk.CTkFont(size=12),
            fg_color=("gray85", "gray22"),
            button_color=("gray75", "gray32"),
            button_hover_color=("gray65", "gray42"),
            text_color=("gray10", "gray90"),
            command=lambda _v: self._apply_chat_options(),
        )
        self.effort_menu.grid(row=0, column=1)

        # 按鈕區 (右側)
        btn_row = ctk.CTkFrame(bottom_bar, fg_color="transparent")
        btn_row.grid(row=0, column=2, sticky="e")

        ctk.CTkButton(
            btn_row,
            text="📂",
            width=32,
            height=32,
            font=ctk.CTkFont(size=18),
            fg_color="transparent",
            hover_color=("gray80", "gray30"),
            command=self._add_workspace_dir,
        ).grid(row=0, column=0, padx=2)

        ctk.CTkButton(
            btn_row,
            text="",
            image=self.attach_icon,
            width=32,
            height=32,
            fg_color="transparent",
            hover_color=("gray80", "gray30"),
            command=self._attach_file,
        ).grid(row=0, column=1, padx=2)

        self.send_btn = ctk.CTkButton(
            btn_row,
            text="送出",
            width=64,
            height=32,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._send,
        )
        self.send_btn.grid(row=0, column=2, padx=(6, 0))

        # 背景抓取後端即時模型列表更新選單
        self._fetch_models_async()

        self._add_system("登入後即可開始對話。Enter 送出，Shift+Enter 換行。")

        # 啟用檔案拖放。
        self._setup_file_drop()

    # ─────────────────────────────────────────────
    # 檔案拖放
    # ─────────────────────────────────────────────

    def _setup_file_drop(self):
        """讓聊天區、輸入框與附件列支援拖放檔案。"""

        drop_targets = [
            self.msg_frame,
            self.input,
            self.attach_bar,
        ]

        registered = set()

        for widget in drop_targets:
            targets = [widget]

            # CustomTkinter 的事件有時由內部 Tk 元件接收。
            for attribute in (
                "_textbox",
                "_canvas",
                "_text_label",
                "_parent_canvas",
            ):
                internal_widget = getattr(widget, attribute, None)

                if internal_widget is not None:
                    targets.append(internal_widget)

            for target in targets:
                target_name = str(target)

                if target_name in registered:
                    continue

                try:
                    target.drop_target_register(DND_FILES)
                    target.dnd_bind(
                        "<<Drop>>",
                        self._on_file_drop,
                    )
                    target.dnd_bind(
                        "<<DropEnter>>",
                        self._on_drop_enter,
                    )
                    target.dnd_bind(
                        "<<DropLeave>>",
                        self._on_drop_leave,
                    )
                    registered.add(target_name)
                except Exception:
                    # 並非每個 CustomTkinter 內部元件都能註冊拖放。
                    log.debug(
                        "拖放目標註冊失敗：%s",
                        target_name,
                        exc_info=True,
                    )

    def _on_file_drop(self, event):
        """接收拖入的檔案，並加入待送出的附件清單。"""

        try:
            # splitlist 能正確處理含空格與大括號的 Windows 路徑。
            dropped_paths = self.tk.splitlist(event.data)
            added_count = 0

            for raw_path in dropped_paths:
                file_path = Path(raw_path).expanduser()

                if not file_path.is_file():
                    continue

                normalized_path = str(file_path.resolve())

                if normalized_path in self._pending_files:
                    continue

                self._pending_files.append(normalized_path)
                added_count += 1

            self._restore_drop_style()

            if added_count:
                self._add_system(f"已附加 {added_count} 個檔案。")

        except Exception:
            log.exception("拖放附加檔案失敗")
            self._restore_drop_style()
            self._add_system("檔案拖放失敗，請重新嘗試。")

        return "break"

    def _on_drop_enter(self, event):
        """檔案進入拖放區時顯示提示。"""

        self.attach_bar.configure(
            text="放開滑鼠即可附加檔案",
            text_color=("#1a56db", "#6ea8ff"),
        )
        self.input.configure(border_color=("#1a56db", "#6ea8ff"), border_width=2)

        return getattr(event, "action", "copy")

    def _on_drop_leave(self, event):
        """檔案離開拖放區時還原附件列。"""

        self._restore_drop_style()
        return getattr(event, "action", "copy")

    def _restore_drop_style(self):
        """還原附件列的提示文字與縮圖列。"""

        self.attach_bar.configure(text="", text_color="gray")
        self.input.configure(border_width=0)
        self._update_attachment_bar()

    def _update_attachment_bar(self):
        """重建待送出附件與工作區的縮圖列。"""

        for widget in self.attach_frame.winfo_children():
            widget.destroy()
        self._chip_images = []

        has_workspace = self.app.agent and hasattr(self.app.agent, "workspace_dirs") and self.app.agent.workspace_dirs

        if not self._pending_files and not has_workspace:
            self.attach_frame.grid_remove()
            return

        self.attach_frame.grid()

        image_exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico"}

        col = 0

        # 顯示工作區
        if has_workspace:
            for path in self.app.agent.workspace_dirs:
                file_path = Path(path)
                chip = ctk.CTkFrame(self.attach_frame, corner_radius=8, fg_color=("gray85", "gray30"))
                chip.grid(row=0, column=col, padx=(0, 6), pady=2)

                ctk.CTkLabel(
                    chip,
                    text=f"📂 {file_path.name}",
                    font=ctk.CTkFont(size=12),
                ).grid(row=0, column=0, padx=(8, 2), pady=4)

                ctk.CTkButton(
                    chip,
                    text="✕",
                    width=22,
                    height=22,
                    corner_radius=6,
                    fg_color="transparent",
                    text_color="gray",
                    hover_color=("gray70", "gray40"),
                    command=lambda p=path: self._remove_workspace(p),
                ).grid(row=0, column=1, padx=(0, 4))
                col += 1

        # 顯示附件
        for path in self._pending_files:
            file_path = Path(path)

            chip = ctk.CTkFrame(
                self.attach_frame,
                corner_radius=8,
            )
            chip.grid(row=0, column=col, padx=(0, 6), pady=2)

            thumb = None
            if file_path.suffix.lower() in image_exts:
                try:
                    img = Image.open(file_path)
                    w, h = img.size
                    scale = 48 / max(w, h)
                    thumb = ctk.CTkImage(
                        light_image=img,
                        dark_image=img,
                        size=(
                            max(1, round(w * scale)),
                            max(1, round(h * scale)),
                        ),
                    )
                    self._chip_images.append(thumb)
                except Exception:
                    thumb = None

            if thumb is not None:
                ctk.CTkLabel(chip, text="", image=thumb).grid(row=0, column=0, padx=(6, 2), pady=4)
            else:
                ctk.CTkLabel(
                    chip,
                    text=f"📎 {file_path.name}",
                    font=ctk.CTkFont(size=12),
                ).grid(row=0, column=0, padx=(8, 2), pady=4)

            ctk.CTkButton(
                chip,
                text="✕",
                width=22,
                height=22,
                corner_radius=6,
                fg_color="transparent",
                text_color="gray",
                hover_color=("gray70", "gray30"),
                command=lambda p=path: self._remove_attachment(p),
            ).grid(row=0, column=1, padx=(0, 4))
            col += 1

    def _remove_attachment(self, path: str):
        """移除單一待送出附件。"""

        if path in self._pending_files:
            self._pending_files.remove(path)

        self._update_attachment_bar()

    def _add_workspace_dir(self):
        from tkinter import filedialog

        path = filedialog.askdirectory(title="選擇要加入工作區的資料夾")
        if path:
            normalized_path = str(Path(path).resolve())
            if self.app.agent and hasattr(self.app.agent, "workspace_dirs"):
                if normalized_path not in self.app.agent.workspace_dirs:
                    self.app.agent.workspace_dirs.append(normalized_path)
                    self._update_attachment_bar()

    def _remove_workspace(self, path: str):
        if self.app.agent and hasattr(self.app.agent, "workspace_dirs"):
            if path in self.app.agent.workspace_dirs:
                self.app.agent.workspace_dirs.remove(path)
                self._update_attachment_bar()

    def _apply_chat_options(self, _value=None):
        """Chat 快速選單變更時寫入設定並即時更新 agent。"""

        from agent.auth import load_config, save_config

        cfg = load_config()
        cfg["model"] = self.model_var.get()

        short = self.effort_var.get()
        cfg["reasoning_effort"] = next(
            (k for k, s in self._effort_short if s == short),
            "medium",
        )
        save_config(cfg)

        if self.app.agent:
            self.app.agent.model = cfg["model"]
            self.app.agent.reasoning_effort = cfg["reasoning_effort"]

    def _fetch_models_async(self):
        """背景抓取後端即時模型列表；失敗或未登入時維持現有選單。"""

        def worker():
            from agent.auth import fetch_available_models, load_config

            cfg = load_config()
            provider = cfg.get("provider", "openai")

            models = fetch_available_models(provider)
            if models:
                try:
                    self.after(0, lambda: self._apply_models(models))
                except Exception:
                    pass  # 視窗已關閉

        threading.Thread(target=worker, daemon=True).start()

    def _apply_models(self, models: list[str]):
        current = self.model_var.get()
        if current and current not in models:
            models = models + [current]
        self.model_menu.configure(values=models)

    def _sync_chat_options(self):
        """從設定檔同步選單（設定頁改過時保持一致）。"""

        from agent.auth import get_model, load_config

        cfg = load_config()
        model = cfg.get("model") or get_model()

        values = list(self.model_menu.cget("values"))
        if model not in values:
            self.model_menu.configure(values=values + [model])
        self.model_var.set(model)

        effort = cfg.get("reasoning_effort", "medium")
        self.effort_var.set(
            next(
                (s for k, s in self._effort_short if k == effort),
                "Medium",
            )
        )

    # (Scroll logic moved to ScrollHelperMixin)

    # ─────────────────────────────────────────────
    # 送出訊息
    # ─────────────────────────────────────────────
    # IME 組字救援
    # ─────────────────────────────────────────────

    # ── Slash Command Autocomplete ──────────────

    def _on_key_release(self, event):
        # Ignore navigation and return keys
        if event.keysym in ("Up", "Down", "Return", "Tab", "Shift_L", "Shift_R", "Control_L", "Control_R"):
            return

        text = self.input.get("1.0", "insert")

        if not text:
            self.autocomplete_menu.hide()
            return

        lines = text.split("\n")
        last_line = lines[-1]
        words = last_line.split(" ")
        last_word = words[-1]

        if last_word.startswith("/"):
            from agent.commands import list_commands

            cmd_prefix = last_word[1:].lower()
            all_cmds = list_commands()
            # 加入內建指令
            all_cmds.append({"name": "model", "description": "切換當次服務提供者與模型"})
            all_cmds.append({"name": "learn", "description": "從近期對話萃取為共用技能"})
            matched_cmds = []
            for cmd in all_cmds:
                if cmd["name"].lower().startswith(cmd_prefix):
                    matched_cmds.append(
                        {
                            "name": cmd["name"],
                            "display": f"/{cmd['name']}",
                            "description": cmd["description"],
                            "insert_text": f"/{cmd['name']} ",
                            "is_cmd": True,
                        }
                    )

            if matched_cmds:
                self.autocomplete_menu.update_items(matched_cmds, cmd_prefix)
                self.autocomplete_menu.show_at_cursor()
            else:
                self.autocomplete_menu.hide()

        elif last_word.startswith("@"):
            import os

            prefix = last_word[1:]
            dirname, basename = os.path.split(prefix)
            search_dir = dirname if dirname else "."

            matched_paths = []
            if os.path.isdir(search_dir):
                try:
                    for entry in os.scandir(search_dir):
                        if basename.lower() in entry.name.lower():
                            is_dir = entry.is_dir()
                            display_meta = "Directory" if is_dir else "File"
                            insert_text = entry.name + (os.sep if is_dir else "")

                            matched_paths.append(
                                {
                                    "name": entry.name,
                                    "display": entry.name + (os.sep if is_dir else ""),
                                    "description": display_meta,
                                    "insert_text": f"@{dirname + os.sep if dirname else ''}{insert_text}"
                                    + ("" if is_dir else " "),
                                    "is_path": True,
                                    "basename": basename,
                                }
                            )
                except PermissionError:
                    pass

            if matched_paths:
                self.autocomplete_menu.update_items(matched_paths, basename)
                self.autocomplete_menu.show_at_cursor()
            else:
                self.autocomplete_menu.hide()
        else:
            self.autocomplete_menu.hide()

    def _on_autocomplete_up(self, event):
        if self.autocomplete_menu.is_open:
            self.autocomplete_menu.move_selection(-1)
            return "break"

    def _on_autocomplete_down(self, event):
        if self.autocomplete_menu.is_open:
            self.autocomplete_menu.move_selection(1)
            return "break"

    def _on_autocomplete_tab(self, event):
        if self.autocomplete_menu.is_open:
            cmd = self.autocomplete_menu.get_selected()
            if cmd:
                self._on_autocomplete_select(cmd)
            return "break"

    def _on_autocomplete_select(self, item):
        # Replace the current typing word with the full command or path
        insert_text = item.get("insert_text", "")
        if not insert_text:
            # Fallback for old cmd format just in case
            insert_text = f"/{item.get('name', '')} "

        if item.get("is_path"):
            search_regex = r"@\S*"
        else:
            search_regex = r"/[a-zA-Z0-9_-]*"

        start_pos = self.input.search(search_regex, "insert", backwards=True, regexp=True, stopindex="1.0")
        if not start_pos:
            start_pos = "1.0"

        self.input.delete(start_pos, "insert")
        self.input.insert("insert", insert_text)
        self.autocomplete_menu.hide()

        # If it's a file path, we probably want to trigger something later,
        # but for now we just leave it in the input text

    def _on_enter(self, event):
        if self.autocomplete_menu.is_open:
            cmd = self.autocomplete_menu.get_selected()
            if cmd:
                self._on_autocomplete_select(cmd)
            return "break"

        from gui.ime import has_composition

        if has_composition():
            # IME 組字中，Enter 是選字而非送出
            return "break"

        self._send()

        return "break"

    def _show_model_selection_dialog(self):
        from agent.auth import fetch_available_models

        dialog = ctk.CTkToplevel(self)
        dialog.title("切換服務提供者與模型")
        dialog.geometry("400x300")
        dialog.transient(self.app)
        dialog.grab_set()

        dialog.grid_columnconfigure(0, weight=1)
        dialog.grid_columnconfigure(1, weight=2)

        ctk.CTkLabel(dialog, text="Provider:", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, padx=20, pady=(20, 10), sticky="w"
        )

        providers = ["openai", "ollama"]
        current_provider = "openai"
        if hasattr(self.app.agent, "client") and self.app.agent.client.base_url:
            url_str = str(self.app.agent.client.base_url)
            if "localhost" in url_str:
                current_provider = "ollama"

        provider_var = ctk.StringVar(value=current_provider)
        provider_menu = ctk.CTkOptionMenu(dialog, variable=provider_var, values=providers)
        provider_menu.grid(row=0, column=1, padx=20, pady=(20, 10), sticky="ew")

        ctk.CTkLabel(dialog, text="Model:", font=ctk.CTkFont(weight="bold")).grid(
            row=1, column=0, padx=20, pady=10, sticky="w"
        )

        model_var = ctk.StringVar(value=self.app.agent.model)
        model_menu = ctk.CTkOptionMenu(dialog, variable=model_var, values=[self.app.agent.model])
        model_menu.grid(row=1, column=1, padx=20, pady=10, sticky="ew")

        def update_models(*args):
            p = provider_var.get()
            try:
                models = fetch_available_models(p)
                if not models:
                    models = ["gpt-6-astra"] if p == "openai" else ["llama3"]
            except Exception:
                models = ["自訂"]
            model_menu.configure(values=models)
            if self.app.agent.model in models and p == current_provider:
                model_var.set(self.app.agent.model)
            else:
                model_var.set(models[0])

        provider_var.trace_add("write", update_models)
        update_models()

        def on_confirm():
            p = provider_var.get()
            m = model_var.get()
            from agent.auth import get_openai_client

            try:
                self.app.agent.client = get_openai_client(provider=p, model=m)
                self.app.agent.model = m
                self._add_system(f"✓ 當次對話已切換為：{p.upper()} - {m}")
            except Exception as e:
                self._add_system(f"⚠ 切換模型失敗：{e}")
            dialog.destroy()

        ctk.CTkButton(dialog, text="確認切換", command=on_confirm).grid(row=2, column=0, columnspan=2, pady=30)

    def _poll_learn(self, future):
        if future.done():
            try:
                skill_content = future.result()
                self._show_learn_dialog(skill_content)
            except Exception as e:
                self._add_system(f"⚠ 萃取失敗：{e}")
        else:
            self.after(100, lambda: self._poll_learn(future))

    def _show_learn_dialog(self, content: str):
        dialog = ctk.CTkToplevel(self)
        dialog.title("儲存 Skill")
        dialog.geometry("600x600")
        dialog.transient(self.app)
        dialog.grab_set()

        dialog.grid_columnconfigure(0, weight=1)
        dialog.grid_rowconfigure(2, weight=1)

        # Name
        name_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        name_frame.grid(row=0, column=0, padx=20, pady=(20, 5), sticky="ew")
        name_frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(name_frame, text="名稱 (英數字與折線):", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, padx=(0, 10)
        )
        name_var = ctk.StringVar()
        ctk.CTkEntry(name_frame, textvariable=name_var).grid(row=0, column=1, sticky="ew")

        # Description
        desc_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        desc_frame.grid(row=1, column=0, padx=20, pady=5, sticky="ew")
        desc_frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(desc_frame, text="簡短描述:", font=ctk.CTkFont(weight="bold")).grid(row=0, column=0, padx=(0, 10))
        desc_var = ctk.StringVar()
        ctk.CTkEntry(desc_frame, textvariable=desc_var).grid(row=0, column=1, sticky="ew")

        # Content Textbox
        ctk.CTkLabel(dialog, text="Skill 內容:", font=ctk.CTkFont(weight="bold")).grid(
            row=2, column=0, padx=20, pady=(10, 0), sticky="w"
        )
        content_box = ctk.CTkTextbox(dialog, wrap="word")
        content_box.grid(row=3, column=0, padx=20, pady=(5, 10), sticky="nsew")
        content_box.insert("1.0", content)

        # Scope & Buttons
        bottom_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        bottom_frame.grid(row=4, column=0, padx=20, pady=(10, 20), sticky="ew")

        ctk.CTkLabel(bottom_frame, text="儲存範圍:", font=ctk.CTkFont(weight="bold")).pack(side="left", padx=(0, 10))
        scope_var = ctk.StringVar(value="workspace")
        ctk.CTkOptionMenu(bottom_frame, variable=scope_var, values=["workspace", "global"]).pack(side="left")

        def on_confirm():
            name = name_var.get().strip()
            desc = desc_var.get().strip()
            if not name:
                import tkinter.messagebox as mb

                mb.showerror("錯誤", "請輸入 Skill 名稱", parent=dialog)
                return

            from agent.skills import save_skill

            final_content = content_box.get("1.0", "end-1c")
            save_path = save_skill(name, desc, final_content, scope_var.get())
            self._add_system(f"✓ 已儲存 Skill 至: {save_path}")
            dialog.destroy()

        ctk.CTkButton(bottom_frame, text="儲存", command=on_confirm).pack(side="right", padx=(10, 0))
        ctk.CTkButton(bottom_frame, text="取消", fg_color="gray", hover_color="darkgray", command=dialog.destroy).pack(
            side="right"
        )

    def _send(self):
        text = self.input.get(
            "1.0",
            "end-1c",
        ).strip()

        if not text:
            return

        if not self.app.is_ready:
            self._add_system("⚠ 尚未登入，請先到「帳號」頁面登入。")
            return

        # slash command：展開成提示詞模板；未知指令則提示
        display_text = text
        if text.startswith("/"):
            parts = text.split()
            if parts[0] in ("/model", "/add-dir", "/workspace", "/learn"):
                from agent.sys_commands import sys_registry

                result = sys_registry.execute(text, self.app.agent)
                if result.status == "error":
                    self._add_system(result.message)
                elif result.status == "success":
                    self._add_system(result.message)
                elif result.status == "needs_auth":
                    import tkinter.messagebox as mb

                    ans = mb.askyesno(
                        "安全警告", f"{result.message}\n\n是否授權本次會話讀寫該目錄？", parent=self.winfo_toplevel()
                    )
                    if ans and result.payload.get("target_dir"):
                        self.app.agent.add_workspace_dir(result.payload["target_dir"])
                        self._add_system(f"✓ 已授權：{result.payload['target_dir']}，可透過工具讀寫。")
                    else:
                        self._add_system("⚠ 已拒絕授權。")
                elif result.status == "interactive_menu":
                    if parts[0] == "/model":
                        self._show_model_selection_dialog()
                elif result.status == "learn_interactive":
                    self._add_system("正在萃取對話知識，請稍候...")
                    # 啟動非同步萃取任務
                    future = self.app.run_async(self.app.agent.extract_skill())
                    self.after(100, lambda: self._poll_learn(future))

                self.input.delete("1.0", "end")
                return

            from agent.commands import expand_command, get_command

            expanded = expand_command(text)
            if expanded is None:
                name = text[1:].split(" ", 1)[0]
                if get_command(name) is None:
                    self._add_system(f"⚠ 找不到指令 /{name}（可到 Commands 頁管理）。")
                    return
            else:
                text = expanded  # 送給模型的是展開後的內容

        self.input.delete(
            "1.0",
            "end",
        )

        files = self._pending_files
        self._pending_files = []

        self.attach_bar.configure(text="")
        self._update_attachment_bar()

        # 泡泡顯示使用者輸入的原文（/指令），實際送出的是展開內容
        self._add_bubble(
            display_text,
            "user",
        )

        spinner = self._add_spinner()

        # 執行期間送出鈕變成「停止」
        self._send_btn_fg = self.send_btn.cget("fg_color")
        self._send_btn_hover = self.send_btn.cget("hover_color")
        self.send_btn.configure(
            text="停止",
            command=self._stop,
            fg_color="#c0392b",
            hover_color="#922b21",
        )

        # ── 串流狀態 ──────────────────────────────
        # agent 在背景 thread 發片段 → queue → GUI 主執行緒消化
        self._stream_q: queue.Queue = queue.Queue()
        self._stream_row = None
        self._stream_box = None
        self._think_panel = None  # 本輪即時推理區塊
        self._streamed_thinking = False  # 已即時顯示過，結束時不再補一份
        self._tool_panels: dict = {}  # call_id -> 可折疊區塊把手
        self.app.agent.on_stream = lambda kind, data: self._stream_q.put((kind, data))

        future = self.app.run_async(
            self.app.agent.chat(
                text,
                attachments=files,
            )
        )

        self.after(
            100,
            lambda: self._poll(
                future,
                spinner,
            ),
        )

    def _stop(self):
        """要求 agent 停止本輪工作（在下一個安全點收尾）。"""

        if self.app.agent:
            self.app.agent.request_stop()

        self.send_btn.configure(
            text="停止中…",
            state="disabled",
        )

    def _restore_send_btn(self):
        self.send_btn.configure(
            text="送出",
            state="normal",
            command=self._send,
            fg_color=getattr(self, "_send_btn_fg", None) or ["#3B8ED0", "#1F6AA5"],
            hover_color=getattr(self, "_send_btn_hover", None) or ["#36719F", "#144870"],
        )

    # 工具內容可能很長（整份檔案、查詢結果），全塞進畫面會讓
    # Tk 變慢又沒人看得完，超過就截斷並標示原始長度。
    _TOOL_TEXT_LIMIT = 2000

    @classmethod
    def _truncate_tool_text(cls, text: str) -> str:
        text = str(text)
        if len(text) <= cls._TOOL_TEXT_LIMIT:
            return text
        return text[: cls._TOOL_TEXT_LIMIT] + f"\n\n…（省略，共 {len(text)} 字）"

    @classmethod
    def _format_tool_args(cls, raw: str) -> str:
        """把工具參數排版成好讀的樣子；不是 JSON 就原樣顯示。"""
        raw = (raw or "").strip()
        if not raw:
            return "（無參數）"
        try:
            parsed = json.loads(raw)
        except Exception:
            return cls._truncate_tool_text(raw)

        return cls._truncate_tool_text(json.dumps(parsed, ensure_ascii=False, indent=2))

    def _drain_stream(self, spinner):
        """把背景送來的串流片段畫到即時泡泡上。"""

        got_text = False

        try:
            while True:
                kind, data = self._stream_q.get_nowait()

                if kind == "round_start":
                    # 工具呼叫後重跑：清掉上一輪的暫定文字，
                    # 推理與工具區塊保留（那是本次任務的完整軌跡）
                    if self._stream_box is not None:
                        self._stream_box.configure(state="normal")
                        self._stream_box.delete("1.0", "end")
                        self._stream_box.configure(state="disabled")
                    self._think_panel = None

                elif kind == "thinking":
                    # 推理片段即時顯示。沒有這段時，遇到長推理或連續
                    # 工具呼叫，畫面會一直停在 spinner 完全沒有反應。
                    if self._think_panel is None:
                        if spinner.winfo_exists():
                            spinner.destroy()
                        self._think_panel = self._add_collapsible(
                            "思考中…",
                            expanded=True,
                            max_lines=10,
                        )
                        self._streamed_thinking = True

                    self._think_panel.append(data)

                elif kind == "tool_start":
                    if spinner.winfo_exists():
                        spinner.destroy()

                    # 推理告一段落，收起來讓出版面給工具
                    if self._think_panel is not None:
                        self._think_panel.set_title("思考過程")
                        self._think_panel.collapse()
                        self._think_panel = None

                    panel = self._add_collapsible(
                        f"⚙  {data['name']}  執行中…",
                        max_lines=14,
                    )
                    panel.append(self._format_tool_args(data.get("arguments", "")))
                    panel.tool_name = data["name"]
                    self._tool_panels[data["call_id"]] = panel

                elif kind == "tool_done":
                    panel = self._tool_panels.pop(
                        data["call_id"],
                        None,
                    )
                    if panel is not None:
                        mark = "✓" if data.get("ok") else "✕"
                        panel.set_title(f"⚙  {data['name']}  {mark}")
                        panel.append("\n\n── 結果 ──\n" + self._truncate_tool_text(data.get("result", "")))

                elif kind == "text":
                    if self._stream_box is None:
                        if spinner.winfo_exists():
                            spinner.destroy()

                        # 正式回覆開始，把推理收起來
                        if self._think_panel is not None:
                            self._think_panel.set_title("思考過程")
                            self._think_panel.collapse()
                            self._think_panel = None

                        self._stream_row, self._stream_box = self._add_stream_bubble()

                    self._stream_box.configure(state="normal")
                    self._stream_box.insert("end", data)
                    self._stream_box.configure(state="disabled")
                    got_text = True

                elif kind == "system":
                    self._add_system(data)
                elif kind == "read":
                    self._add_system(f"● Read ({data})")
                elif kind == "ask_question":
                    self._show_ask_question_modal(data)

        except queue.Empty:
            pass

        if got_text and self._stream_box is not None:
            self._fit_textbox_height(
                self._stream_box,
                max_lines=self._bubble_max_lines(),
            )

    def _poll(self, future, spinner):
        # 串流繪製失敗絕不能中斷輪詢：_poll 一旦拋出，下面的
        # self.after 就排不到，迴圈直接死掉——agent 明明跑完了，
        # 畫面卻永遠停在思考中，要重開程式從存檔讀回才看得到回覆。
        try:
            self._drain_stream(spinner)
        except Exception:
            log.exception("串流繪製失敗（已略過該批片段，繼續輪詢）")

        if future.done():
            if spinner.winfo_exists():
                spinner.destroy()

            self._restore_send_btn()

            self.app.agent.on_stream = None

            # 以下收尾必須在取結果之前做：future.result() 可能拋例外，
            # 放進 try 裡的話出錯時區塊會永遠停在「思考中…／執行中…」
            if self._think_panel is not None:
                self._think_panel.set_title("思考過程")
                self._think_panel.collapse()
                self._think_panel = None

            # 中途被停止或出錯時工具不會收到 tool_done
            for panel in self._tool_panels.values():
                panel.set_title(f"⚙  {panel.tool_name}  ⏹ 已中斷")
            self._tool_panels.clear()

            try:
                thinking, reply, usage_str = future.result()

                # 已經即時顯示過就不再補一份，否則會出現兩塊思考過程
                if thinking and not self._streamed_thinking:
                    self._add_reasoning(
                        thinking,
                        before=self._stream_row,
                    )

                if self._stream_box is not None:
                    # 以最終文字取代串流內容（含圖片/檔案路徑附註）
                    self._stream_box.configure(state="normal")
                    self._stream_box.delete("1.0", "end")
                    self._stream_box.insert("1.0", reply)
                    self._stream_box.configure(state="disabled")
                    self._fit_textbox_height(
                        self._stream_box,
                        max_lines=self._bubble_max_lines(),
                    )
                else:
                    self._add_bubble(
                        reply,
                        "assistant",
                    )

                self._add_message_footer(reply, usage_str)

                for image_path in getattr(
                    self.app.agent,
                    "last_images",
                    [],
                ):
                    self._add_image(image_path)

                for file_path in getattr(
                    self.app.agent,
                    "last_files",
                    [],
                ):
                    self._add_file_link(file_path)

                self.app.save_current_chat()

            except Exception as error:
                self._add_bubble(
                    f"錯誤：{error}",
                    "error",
                )
        else:
            self.after(
                80,
                lambda: self._poll(
                    future,
                    spinner,
                ),
            )

    # ─────────────────────────────────────────────
    # Modal 表單 (ask_question)
    # ─────────────────────────────────────────────
    def _show_ask_question_modal(self, data: dict):
        """處理 ask_question tool，顯示 modal 表單。"""
        questions = data.get("questions", [])
        callback = data.get("callback")

        modal = ctk.CTkToplevel(self)
        modal.title("需要您的確認 / 選擇")
        modal.geometry("500x600")
        modal.transient(self.winfo_toplevel())
        modal.grab_set()

        scroll = ctk.CTkScrollableFrame(modal)
        scroll.pack(fill="both", expand=True, padx=10, pady=10)

        answers = {}
        for idx, q in enumerate(questions):
            q_text = q.get("question", "")
            opts = q.get("options", [])
            is_multi = q.get("is_multi_select", False)

            ctk.CTkLabel(scroll, text=q_text, font=ctk.CTkFont(weight="bold"), wraplength=450, justify="left").pack(
                anchor="w", pady=(10, 5)
            )

            if is_multi:
                answers[idx] = []
                for opt in opts:
                    var = ctk.StringVar(value="")
                    cb = ctk.CTkCheckBox(scroll, text=opt, variable=var, onvalue=opt, offvalue="")
                    cb.pack(anchor="w", padx=10, pady=2)
                    answers[idx].append(var)
            else:
                var = ctk.StringVar(value="")
                if opts:
                    var.set(opts[0])  # Default to first option
                answers[idx] = var
                for opt in opts:
                    rb = ctk.CTkRadioButton(scroll, text=opt, variable=var, value=opt)
                    rb.pack(anchor="w", padx=10, pady=2)

        def submit():
            result_dict = {"answers": []}
            for idx, q in enumerate(questions):
                ans = answers[idx]
                if isinstance(ans, list):
                    selected = [v.get() for v in ans if v.get()]
                    result_dict["answers"].append({"question": q.get("question"), "answer": selected})
                else:
                    result_dict["answers"].append({"question": q.get("question"), "answer": ans.get()})

            modal.destroy()
            if callback:
                callback(result_dict)

            summary = ", ".join(str(a["answer"]) for a in result_dict["answers"] if a["answer"])
            self._add_system(f"● 已回覆選擇：{summary}")

        def skip():
            modal.destroy()
            if callback:
                callback({"answers": [], "skipped": True})
            self._add_system("● 已跳過問題")

        btn_frame = ctk.CTkFrame(modal, fg_color="transparent")
        btn_frame.pack(fill="x", padx=10, pady=10)

        ctk.CTkButton(btn_frame, text="Submit", command=submit).pack(side="right", padx=5)
        ctk.CTkButton(btn_frame, text="Skip", fg_color="gray", command=skip).pack(side="right", padx=5)

        modal.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() // 2) - (modal.winfo_width() // 2)
        y = self.winfo_rooty() + (self.winfo_height() // 2) - (modal.winfo_height() // 2)
        modal.geometry(f"+{x}+{y}")

    # ─────────────────────────────────────────────
    # 附加檔案
    # ─────────────────────────────────────────────

    def _on_paste(self, event):
        """
        剪貼簿有圖片時存成暫存 PNG 並附加；
        若剪貼簿為文字，交由預設貼上處理。
        """

        try:
            from PIL import ImageGrab

            data = ImageGrab.grabclipboard()
        except Exception:
            return None

        if data is None:
            return None

        import tempfile
        import time as current_time

        paths: list[str] = []

        if isinstance(data, list):
            # 從檔案總管複製的圖片檔
            paths = [
                path
                for path in data
                if Path(path).suffix.lower()
                in {
                    ".png",
                    ".jpg",
                    ".jpeg",
                    ".gif",
                    ".webp",
                }
            ]
        else:
            # 截圖或點陣圖
            temporary_path = Path(tempfile.gettempdir()) / (f"pocket-agent-paste-{current_time.strftime('%H%M%S')}.png")

            data.save(
                temporary_path,
                "PNG",
            )

            paths = [
                str(temporary_path),
            ]

        if not paths:
            return None

        for path in paths:
            if path not in self._pending_files:
                self._pending_files.append(path)

        self._update_attachment_bar()

        return "break"

    def _attach_file(self):
        from tkinter import filedialog

        paths = filedialog.askopenfilenames(
            title="選擇要附加的檔案",
            filetypes=[
                (
                    "所有檔案",
                    "*.*",
                ),
                (
                    "圖片",
                    "*.png *.jpg *.jpeg *.gif *.webp",
                ),
                (
                    "文件",
                    "*.pdf *.txt *.md *.csv *.docx *.xlsx *.pptx",
                ),
            ],
        )

        for path in paths:
            normalized_path = str(Path(path).resolve())

            if normalized_path not in self._pending_files:
                self._pending_files.append(normalized_path)

        self._update_attachment_bar()

    # ─────────────────────────────────────────────
    # 訊息泡泡
    # ─────────────────────────────────────────────

    DEFAULT_BUBBLE_MAX_LINES = 15
    DEFAULT_CHAT_FONT_SIZE = 13
    MIN_CHAT_FONT_SIZE = 8
    MAX_CHAT_FONT_SIZE = 32

    def _bubble_max_lines(self) -> int:
        """訊息泡泡的最大顯示行數（config.json 的 bubble_max_lines）。"""
        from agent.auth import load_config

        try:
            return max(
                1,
                int(
                    load_config().get(
                        "bubble_max_lines",
                        self.DEFAULT_BUBBLE_MAX_LINES,
                    )
                ),
            )
        except Exception:
            return self.DEFAULT_BUBBLE_MAX_LINES

    def _chat_font_size(self) -> int:
        """對話視窗的基準字體大小（config.json 的 chat_font_size）。
        套用到訊息泡泡、輸入框；思考過程面板固定小 2 號。"""
        from agent.auth import load_config

        try:
            size = int(load_config().get("chat_font_size", self.DEFAULT_CHAT_FONT_SIZE))
        except Exception:
            return self.DEFAULT_CHAT_FONT_SIZE
        return min(max(size, self.MIN_CHAT_FONT_SIZE), self.MAX_CHAT_FONT_SIZE)

    def _apply_input_font(self):
        """輸入框是常駐元件，設定變更時直接套用（不用像泡泡那樣等下一則訊息）。"""
        self.input.configure(font=ctk.CTkFont(size=self._chat_font_size()))

    def _bubble_width(self) -> int:
        """訊息泡泡寬度：依對話區目前實際寬度按比例縮放。

        以前寫死 480px，視窗放大後泡泡完全不會變寬，右側留一大片
        空白、文字卻擠著換行。改成量 msg_frame 目前寬度的 8 成，
        並夾在 320～1600px 之間（太窄不好讀、上限只是防超寬螢幕
        單行字數失控，正常視窗大小都吃得到接近 8 成）。
        """
        try:
            available = self.msg_frame.winfo_width()
        except Exception:
            available = 0

        if available <= 1:
            # 尚未完成排版（例如程式剛啟動時的第一則系統訊息），退回預設值
            return 480

        return min(max(int(available * 0.8), 320), 1600)

    def _fit_textbox_height(
        self,
        textbox: ctk.CTkTextbox,
        max_lines: int = 15,
        pad: int = 26,  # CTkTextbox 圓角內距（上下各約 corner_radius）+ 餘裕
    ):
        """依實際換行後的顯示行數調整高度。

        字數粗估對中文（全形字寬）嚴重失準，改在排版完成後
        用 tk 的 displaylines 量真實行數。超過 max_lines 才出現
        泡泡內捲軸。
        """

        def fit(attempt: int = 0):
            try:
                tk_text = textbox._textbox

                # 寬度尚未確定時換行數不可信，稍後重量
                if tk_text.winfo_width() <= 1 and attempt < 10:
                    textbox.after(30, lambda: fit(attempt + 1))
                    return

                # displaylines 回傳的是「換行次數」，單行時為 None → 行數要 +1
                lines = tk_text.count("1.0", "end-1c", "displaylines")
                if isinstance(lines, (tuple, list)):
                    lines = lines[0]
                lines = max(1, min(int(lines or 0) + 1, max_lines))

                line_height = int(tk_text.tk.call("font", "metrics", tk_text.cget("font"), "-linespace"))

                textbox.configure(height=lines * line_height + pad)
                self._scroll_bottom()
            except Exception:
                pass  # 元件可能已被銷毀

        textbox.after_idle(fit)

    # ─────────────────────────────────────────────
    # [MessageList 重構 - 階段一]
    # 下方所有 _add_* 相關的渲染與元件建立方法，已歸類為 MessageList 的核心職責。
    # 將於階段三正式搬移至 gui.components.message_list.MessageList 中，
    # 解除 ChatView 與實際 DOM 節點及 Layout 邏輯的深度耦合。
    # ─────────────────────────────────────────────
    def _add_bubble(
        self,
        text: str,
        role: str,
    ):
        row = ctk.CTkFrame(
            self.msg_frame,
            fg_color="transparent",
        )
        row.pack(
            fill="x",
            padx=8,
            pady=6,
        )

        is_user = role == "user"

        background_color = {
            "user": ("#1a56db", "#1a56db"),
            "assistant": ("gray85", "gray20"),
            "error": ("#c0392b", "#922b21"),
        }.get(role, ("gray80", "gray20"))

        text_color = {
            "user": ("white", "white"),
            "assistant": ("gray10", "gray95"),
            "error": ("white", "white"),
        }.get(role, ("gray10", "gray90"))

        # Avatar
        avatar_image = self.app._nav_icons.get("account") if is_user else self.app._app_icon
        avatar_label = ctk.CTkLabel(
            row,
            text="",
            image=avatar_image,
            width=32,
            height=32,
        )

        bubble = ctk.CTkFrame(
            row,
            fg_color=background_color,
            corner_radius=16,
        )

        if is_user:
            avatar_label.pack(side="right", padx=(8, 4), anchor="n")
            bubble.pack(side="right", padx=6)
        else:
            avatar_label.pack(side="left", padx=(4, 8), anchor="n")
            bubble.pack(side="left", padx=6)

        content_frame = ctk.CTkFrame(bubble, fg_color="transparent")
        content_frame.pack(padx=12, pady=10)

        import re

        parts = re.split(r"```([a-zA-Z0-9_\-\+]*)\n(.*?)```", text, flags=re.DOTALL)

        # parts structure: [text1, lang1, code1, text2, lang2, code2, text3]
        for i in range(0, len(parts), 3):
            # Normal text block
            txt = parts[i].strip("\r\n")
            if txt:
                tb = ctk.CTkTextbox(
                    content_frame,
                    width=self._bubble_width(),
                    height=36,
                    wrap="word",
                    fg_color="transparent",
                    text_color=text_color,
                    border_width=0,
                    scrollbar_button_color=("gray65", "gray40"),
                    scrollbar_button_hover_color=("gray50", "gray55"),
                    activate_scrollbars=True,
                    font=ctk.CTkFont(size=self._chat_font_size()),
                )
                tb.insert("1.0", txt)
                tb.configure(state="disabled")
                tb.pack(fill="x", pady=(0, 4) if len(parts) > 1 else 0)
                self._fit_textbox_height(tb, max_lines=self._bubble_max_lines())
                self.scroll_manager.bind_bubble(tb)

            # Code block
            if i + 2 < len(parts):
                lang = parts[i + 1].strip()
                code_txt = parts[i + 2].strip("\r\n")

                code_bg = ("#e6e6e6", "#141414")
                header_bg = ("#d9d9d9", "#1c1c1c")

                box_frame = ctk.CTkFrame(content_frame, fg_color=code_bg, corner_radius=8)
                box_frame.pack(fill="x", pady=4)

                header = ctk.CTkFrame(box_frame, fg_color=header_bg, corner_radius=8)
                header.pack(fill="x")
                ctk.CTkLabel(header, text=lang or "code", font=ctk.CTkFont(size=11), text_color="gray").pack(
                    side="left", padx=10, pady=2
                )

                def make_copy_cmd(ct=code_txt):
                    def copy_code():
                        self.clipboard_clear()
                        self.clipboard_append(ct)
                        self.update_idletasks()

                    return copy_code

                ctk.CTkButton(
                    header,
                    text="複製",
                    width=40,
                    height=20,
                    font=ctk.CTkFont(size=11),
                    fg_color="transparent",
                    text_color="gray",
                    hover_color=("gray75", "gray30"),
                    command=make_copy_cmd(),
                ).pack(side="right", padx=6, pady=2)

                cb = ctk.CTkTextbox(
                    box_frame,
                    width=self._bubble_width() - 20,
                    height=36,
                    wrap="none",
                    fg_color="transparent",
                    text_color=("gray20", "gray85"),
                    border_width=0,
                    activate_scrollbars=True,
                    font=ctk.CTkFont(family="Consolas", size=self._chat_font_size()),
                )
                cb.insert("1.0", code_txt)
                cb.configure(state="disabled")
                cb.pack(fill="x", padx=10, pady=8)
                self._fit_textbox_height(cb, max_lines=self._bubble_max_lines())
                self.scroll_manager.bind_bubble(cb)
                self.scroll_manager.bind_outer(box_frame)
                self.scroll_manager.bind_outer(header)

        self.scroll_manager.bind_outer(row)
        self.scroll_manager.bind_outer(bubble)
        self.scroll_manager.bind_outer(avatar_label)
        self.scroll_manager.bind_outer(content_frame)

        self._scroll_bottom()

    def _add_stream_bubble(self):
        """建立串流中的 assistant 泡泡，回傳 (row, textbox)。"""

        row = ctk.CTkFrame(
            self.msg_frame,
            fg_color="transparent",
        )
        row.pack(fill="x", padx=8, pady=6)

        avatar_label = ctk.CTkLabel(
            row,
            text="",
            image=self.app._app_icon,
            width=32,
            height=32,
        )
        avatar_label.pack(side="left", padx=(4, 8), anchor="n")

        bubble = ctk.CTkFrame(
            row,
            fg_color=("gray85", "gray20"),
            corner_radius=16,
        )
        bubble.pack(side="left", padx=6)

        textbox = ctk.CTkTextbox(
            bubble,
            width=self._bubble_width(),
            height=36,
            wrap="word",
            fg_color="transparent",
            text_color=("gray10", "gray95"),
            border_width=0,
            scrollbar_button_color=("gray65", "gray40"),
            scrollbar_button_hover_color=("gray50", "gray55"),
            activate_scrollbars=True,
            font=ctk.CTkFont(size=self._chat_font_size()),
        )

        textbox.configure(state="disabled")
        textbox.pack(padx=12, pady=10)

        self.scroll_manager.bind_bubble(textbox)
        self.scroll_manager.bind_outer(row)
        self.scroll_manager.bind_outer(bubble)
        self.scroll_manager.bind_outer(avatar_label)

        return row, textbox

    def _add_message_footer(self, text_to_copy: str, usage_str: str):
        row = ctk.CTkFrame(self.msg_frame, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(0, 10))

        if usage_str:
            ctk.CTkLabel(row, text=usage_str, text_color="gray", font=ctk.CTkFont(size=11)).pack(
                side="left", padx=(4, 10)
            )

        def copy_text():
            self.clipboard_clear()
            self.clipboard_append(text_to_copy)
            self.update_idletasks()
            copy_btn.configure(text="已複製")
            self.after(1500, lambda: copy_btn.configure(text="複製"))

        copy_btn = ctk.CTkButton(
            row,
            text="複製",
            width=50,
            height=20,
            corner_radius=4,
            fg_color="transparent",
            text_color="gray",
            hover_color=("gray80", "gray30"),
            font=ctk.CTkFont(size=11),
            command=copy_text,
        )
        copy_btn.pack(side="left")

        self.scroll_manager.bind_outer(row)
        self._scroll_bottom()

    # ─────────────────────────────────────────────
    # 推理展開區塊
    # ─────────────────────────────────────────────

    def _add_reasoning(self, thinking: str, before=None):
        """可展開或收合的推理過程區塊（一次給完內容）。"""

        panel = self._add_collapsible(
            "思考過程",
            before=before,
        )
        panel.append(thinking)
        return panel

    def _add_collapsible(
        self,
        title: str,
        before=None,
        expanded: bool = False,
        max_lines: int = 12,
    ):
        """
        可展開或收合的內容區塊，回傳可持續追加內容的把手。

        把手提供 append(text) / set_title(text) / expand() / collapse()，
        讓串流中的推理與工具呼叫可以邊跑邊長內容。

        before：指定插在哪個元件之前（串流泡泡完成後補在其上方）。
        """

        container = ctk.CTkFrame(
            self.msg_frame,
            fg_color="transparent",
        )

        pack_kwargs = {
            "fill": "x",
            "padx": 8,
            "pady": (4, 0),
        }
        if before is not None and before.winfo_exists():
            pack_kwargs["before"] = before

        container.pack(**pack_kwargs)

        toggle_button = ctk.CTkButton(
            container,
            text=f"▶  {title}",
            fg_color="transparent",
            text_color="gray",
            hover_color=(
                "gray85",
                "gray20",
            ),
            anchor="w",
            height=26,
            font=ctk.CTkFont(size=11),
        )
        toggle_button.pack(
            anchor="w",
            padx=4,
        )

        panel = ctk.CTkFrame(
            container,
            fg_color=(
                "gray88",
                "gray18",
            ),
            corner_radius=8,
        )

        textbox = ctk.CTkTextbox(
            panel,
            wrap="word",
            height=50,  # 展開時依實際行數調整
            fg_color=(
                "gray88",
                "gray18",
            ),
            border_width=0,
            text_color=(
                "gray40",
                "gray60",
            ),
            scrollbar_button_color=(
                "gray65",
                "gray40",
            ),
            scrollbar_button_hover_color=(
                "gray50",
                "gray55",
            ),
            font=ctk.CTkFont(
                size=max(9, self._chat_font_size() - 2),
                slant="italic",
            ),
            activate_scrollbars=True,
        )

        textbox.configure(
            state="disabled",
        )

        textbox.pack(
            padx=10,
            pady=8,
            fill="x",
        )

        # 思考內容也使用相同的智慧滾輪行為
        self.scroll_manager.bind_bubble(textbox)
        self.scroll_manager.bind_outer(container)
        self.scroll_manager.bind_outer(panel)
        self.scroll_manager.bind_outer(toggle_button)

        state = {
            "shown": False,
            "title": title,
        }

        def _refresh_arrow():
            arrow = "▼" if state["shown"] else "▶"
            toggle_button.configure(
                text=f"{arrow}  {state['title']}",
            )

        def collapse():
            if not state["shown"]:
                return
            panel.pack_forget()
            state["shown"] = False
            _refresh_arrow()

        def expand():
            if state["shown"]:
                return
            panel.pack(
                fill="x",
                padx=4,
                pady=(2, 4),
            )
            state["shown"] = True
            _refresh_arrow()

            # 首次展開後才知道實際寬度，依顯示行數調高度
            self._fit_textbox_height(textbox, max_lines=max_lines)
            self._scroll_bottom()

        def toggle():
            collapse() if state["shown"] else expand()

        def append(chunk: str):
            """追加內容；展開中則同步長高並跟著捲到底。"""
            if not chunk or not textbox.winfo_exists():
                return
            textbox.configure(state="normal")
            textbox.insert("end", chunk)
            textbox.configure(state="disabled")
            if state["shown"]:
                self._fit_textbox_height(textbox, max_lines=max_lines)
                textbox.see("end")

        def set_title(new_title: str):
            state["title"] = new_title
            _refresh_arrow()

        toggle_button.configure(
            command=toggle,
        )

        if expanded:
            expand()

        self._scroll_bottom()

        return SimpleNamespace(
            container=container,
            textbox=textbox,
            append=append,
            set_title=set_title,
            expand=expand,
            collapse=collapse,
        )

    # ─────────────────────────────────────────────
    # 圖片
    # ─────────────────────────────────────────────

    def _add_image(self, path):
        """
        在對話中顯示圖片。
        點擊圖片後使用系統預設程式開啟。
        """

        try:
            from PIL import Image

            image = Image.open(path)

            width, height = image.size

            display_width = min(
                width,
                360,
            )

            display_height = max(
                1,
                round(height * display_width / width),
            )

            custom_image = ctk.CTkImage(
                light_image=image,
                dark_image=image,
                size=(
                    display_width,
                    display_height,
                ),
            )

            row = ctk.CTkFrame(
                self.msg_frame,
                fg_color="transparent",
            )
            row.pack(
                fill="x",
                padx=8,
                pady=3,
            )

            label = ctk.CTkLabel(
                row,
                image=custom_image,
                text="",
                cursor="hand2",
            )
            label.pack(
                anchor="w",
                padx=6,
            )

            import webbrowser

            label.bind(
                "<Button-1>",
                lambda event: webbrowser.open(Path(path).as_uri()),
            )

            self.scroll_manager.bind_outer(row)
            self.scroll_manager.bind_outer(label)

            self._scroll_bottom()

        except Exception:
            log.exception(
                "無法顯示圖片：%s",
                path,
            )

    # ─────────────────────────────────────────────
    # 檔案連結
    # ─────────────────────────────────────────────

    def _add_file_link(self, path):
        """
        將產生的文件顯示成可點擊的檔案卡片。
        """

        file_path = Path(path)

        icon = {
            ".docx": "📝",
            ".pptx": "📊",
            ".pdf": "📄",
        }.get(
            file_path.suffix.lower(),
            "📄",
        )

        row = ctk.CTkFrame(
            self.msg_frame,
            fg_color="transparent",
        )
        row.pack(
            fill="x",
            padx=8,
            pady=2,
        )

        button = ctk.CTkButton(
            row,
            text=f"{icon}  {file_path.name}",
            anchor="w",
            height=34,
            corner_radius=8,
            fg_color=(
                "gray80",
                "gray25",
            ),
            text_color=(
                "gray10",
                "gray90",
            ),
            hover_color=(
                "gray70",
                "gray30",
            ),
            command=lambda: self._open_file(file_path),
        )
        button.pack(
            anchor="w",
            padx=6,
        )

        self.scroll_manager.bind_outer(row)
        self.scroll_manager.bind_outer(button)

        self._scroll_bottom()

    @staticmethod
    def _open_file(path: Path):
        import os

        try:
            # Windows
            os.startfile(str(path))
        except (AttributeError, OSError):
            import webbrowser

            webbrowser.open(path.as_uri())

    # ─────────────────────────────────────────────
    # 思考中提示
    # ─────────────────────────────────────────────

    def _add_spinner(self) -> ctk.CTkFrame:
        row = ctk.CTkFrame(
            self.msg_frame,
            fg_color="transparent",
        )
        row.pack(
            fill="x",
            padx=8,
            pady=3,
        )

        bubble = ctk.CTkFrame(
            row,
            fg_color=(
                "gray82",
                "gray22",
            ),
            corner_radius=14,
        )
        bubble.pack(
            anchor="w",
            padx=6,
        )

        image_path = Path(__file__).parent / "assets" / "icon_agent_thinking.png"

        self.thinking_icon = ctk.CTkImage(
            light_image=Image.open(image_path),
            dark_image=Image.open(image_path),
            size=(48, 48),
        )

        label = ctk.CTkLabel(
            bubble,
            text="  思考中…",
            image=self.thinking_icon,
            compound="left",
            text_color="gray",
        )
        label.pack(
            padx=14,
            pady=8,
        )

        self.scroll_manager.bind_outer(row)
        self.scroll_manager.bind_outer(bubble)
        self.scroll_manager.bind_outer(label)

        self._scroll_bottom()

        return row

    # ─────────────────────────────────────────────
    # 系統訊息
    # ─────────────────────────────────────────────

    def _add_system(self, text: str, image=None):
        label = ctk.CTkLabel(
            self.msg_frame,
            text=text,
            image=image,
            compound="left",  # 圖片顯示在文字左側
            text_color="gray",
            font=ctk.CTkFont(size=self._chat_font_size() + 1),
            wraplength=520,
        )
        label.pack(
            pady=12,
        )

        self.scroll_manager.bind_outer(label)
        self._scroll_bottom()

    # ─────────────────────────────────────────────
    # 自動捲動到底部
    # ─────────────────────────────────────────────

    def _cancel_scroll_jobs(self):
        """取消尚未執行的捲動工作，避免切換對話後套用舊位置。"""

        for job_id in self._scroll_jobs:
            try:
                self.after_cancel(job_id)
            except Exception:
                pass

        self._scroll_jobs.clear()

    def _refresh_scrollregion(self):
        """強制更新 CTkScrollableFrame 內部 Canvas 的內容範圍。"""

        canvas = self.msg_frame._parent_canvas

        self.update_idletasks()
        self.msg_frame.update_idletasks()
        canvas.update_idletasks()

        content_box = canvas.bbox("all")

        if content_box is not None:
            canvas.configure(scrollregion=content_box)

        return canvas

    def _scroll_bottom(self, force: bool = False, delay: int = 0):
        """
        等待訊息元件完成尺寸計算後，更新 scrollregion 並捲動到底部。

        對話紀錄重建期間，個別訊息不觸發捲動；全部建立完成後，
        由 render_history() 使用 force=True 統一處理。
        """

        if self._rendering_history and not force:
            return

        self._cancel_scroll_jobs()

        generation = self._render_generation

        # CustomTkinter 可能分數個事件循環才完成內部 Canvas 尺寸更新。
        # 多階段重算可避免必須縮放視窗或手動拉 scrollbar 才顯示。
        delays = (delay, delay + 35, delay + 100, delay + 220)

        def perform_scroll(expected_generation=generation):
            if expected_generation != self._render_generation:
                return

            try:
                canvas = self._refresh_scrollregion()
                canvas.yview_moveto(1.0)
            except Exception:
                log.exception("無法將對話區捲動到底部")

        for milliseconds in delays:
            job_id = self.after(milliseconds, perform_scroll)
            self._scroll_jobs.append(job_id)

    # ─────────────────────────────────────────────
    # Session 載入與重置
    # ─────────────────────────────────────────────

    def reset(self):
        """清空訊息區並開始新對話。"""

        self._render_generation += 1
        self._cancel_scroll_jobs()
        self._rendering_history = True

        try:
            for widget in self.msg_frame.winfo_children():
                widget.destroy()

            self._pending_files = []
            self.attach_bar.configure(text="")
            self._update_attachment_bar()

            # 先讓舊元件的刪除反映到 Canvas。
            self.update_idletasks()
            self.msg_frame.update_idletasks()

            self._add_system("新對話開始。")
        finally:
            self._rendering_history = False

        self._scroll_bottom(force=True, delay=30)

    def render_history(
        self,
        history: list[dict],
    ):
        """將載入的 session 歷史完整重建成訊息泡泡。"""

        self._render_generation += 1
        self._cancel_scroll_jobs()
        self._rendering_history = True

        try:
            canvas = self.msg_frame._parent_canvas

            # 清除舊對話。
            for widget in self.msg_frame.winfo_children():
                widget.destroy()

            # 立即清除舊 scrollregion，避免新對話沿用前一個對話高度。
            self.update_idletasks()
            self.msg_frame.update_idletasks()
            canvas.update_idletasks()
            canvas.configure(scrollregion=(0, 0, 0, 0))
            canvas.yview_moveto(0.0)

            # 建立新對話。重建期間 _scroll_bottom() 會被抑制，
            # 避免每則訊息排入一個尚未完成尺寸計算的捲動工作。
            for message in history:
                role = message.get("role", "")

                text = self._content_text(message.get("content", ""))

                if not text:
                    continue

                bubble_role = "user" if role == "user" else "assistant"

                self._add_bubble(text, bubble_role)

                if role == "assistant":
                    self._add_message_footer(text, "")
                    # 回覆中的圖片或文件路徑若仍存在，重新顯示。
                    for line in text.splitlines():
                        line = line.strip()

                        if line.startswith("🖼️"):
                            path = Path(line.lstrip("🖼️").strip())

                            if path.exists():
                                self._add_image(path)

                        elif line.startswith("📄"):
                            path = Path(line.lstrip("📄").strip())

                            if path.exists():
                                self._add_file_link(path)

            # 先完成一次完整版面配置。
            self.update_idletasks()
            self.msg_frame.update_idletasks()
            canvas.update_idletasks()

        finally:
            self._rendering_history = False

        # 全部訊息建立完成後，再分階段更新 scrollregion 並捲到底部。
        self._scroll_bottom(force=True, delay=30)

    @staticmethod
    def _content_text(content) -> str:
        if isinstance(content, str):
            return content

        parts = []

        for part in content:
            if not isinstance(part, dict):
                continue

            content_type = part.get("type")

            if content_type in (
                "input_text",
                "text",
            ):
                parts.append(
                    part.get(
                        "text",
                        "",
                    )
                )

            elif content_type in (
                "image_url",
                "input_image",
            ):
                parts.append("📎 [圖片附件]")

        return "\n".join(parts)

    # ─────────────────────────────────────────────
    # App callbacks
    # ─────────────────────────────────────────────

    def on_agent_ready(self):

        icon_path = Path(__file__).parent / "assets" / "icon_agent_check.png"

        self.check_icon = ctk.CTkImage(
            light_image=Image.open(icon_path),
            dark_image=Image.open(icon_path),
            size=(56, 56),
        )

        self._add_system(
            "    已連線，開始對話吧！",
            image=self.check_icon,
        )

        # CLI 指令執行前的使用者確認
        if self.app.agent:
            self.app.agent.on_confirm = self._confirm_command

        # 登入完成後再抓一次即時模型列表（啟動時可能尚未登入）
        self._fetch_models_async()

    def _confirm_command(self, command: str) -> bool:
        """CLI 工具的確認回呼。從背景執行緒呼叫：
        丟到主執行緒跳確認視窗，等使用者回答（最多 5 分鐘）。
        設定「CLI 免確認」開啟時直接放行。"""

        from agent.auth import load_config

        if load_config().get("cli_auto_approve"):
            return True

        answer: dict = {}
        done = threading.Event()

        def ask():
            from tkinter import messagebox

            answer["ok"] = messagebox.askyesno(
                "執行 CLI 指令 (⚠️ 高危險 Shell 腳本)",
                f"Agent 嘗試使用 Shell 執行以下指令：\n\n{command}\n\n"
                "⚠️ 警告：這是一段直接傳遞給作業系統的 Shell 腳本，具備完整權限，"
                "可能包含惡意負載或不可逆操作。\n請問是否允許執行？",
                parent=self.app,
                icon="warning",
            )
            done.set()

        self.after(0, ask)
        done.wait(timeout=300)

        return answer.get("ok", False)

    def on_show(self):
        self._sync_chat_options()
        self._apply_input_font()
        self.input.focus_set()

    # (IME logic moved to IMEHelperMixin)
