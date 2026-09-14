"""設定頁面：system prompt。（模型與推理強度改在 Chat 頁選擇）"""

import threading

import customtkinter as ctk

REASONING = [
    ("medium", "Medium（預設，平衡速度與深度）"),
    ("high", "High（更深入，較慢）"),
    ("xhigh", "XHigh（最深，僅 gpt-5.4+ 支援）"),
    ("low", "Low（快速，淺層思考）"),
    ("none", "關閉（不推理，直接回答）"),
]


class SettingsView(ctk.CTkFrame):
    def __init__(self, parent, app):
        super().__init__(parent, fg_color="transparent")
        self.app = app
        self.grid_columnconfigure(0, weight=1)
        self._build()

    def _build(self):
        ctk.CTkLabel(self, text="設定", font=ctk.CTkFont(size=20, weight="bold")).grid(
            row=0, column=0, padx=30, pady=(28, 6), sticky="w"
        )

        card = ctk.CTkFrame(self)
        card.grid(row=1, column=0, padx=30, pady=10, sticky="ew")
        card.grid_columnconfigure(1, weight=1)

        # System prompt
        ctk.CTkLabel(card, text="System Prompt", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, padx=20, pady=(20, 6), sticky="nw"
        )
        self.prompt_box = ctk.CTkTextbox(card, height=120, wrap="word")
        self.prompt_box.grid(row=0, column=1, padx=20, pady=(20, 6), sticky="ew")

        # 訊息泡泡最大行數
        ctk.CTkLabel(card, text="泡泡最大行數", font=ctk.CTkFont(weight="bold")).grid(
            row=1, column=0, padx=20, pady=(10, 6), sticky="w"
        )
        bubble_row = ctk.CTkFrame(card, fg_color="transparent")
        bubble_row.grid(row=1, column=1, padx=20, pady=(10, 6), sticky="w")

        self.bubble_lines_entry = ctk.CTkEntry(bubble_row, width=70)
        self.bubble_lines_entry.grid(row=0, column=0)

        ctk.CTkLabel(
            bubble_row,
            text="行以內完整顯示，超過改用泡泡內捲動（預設 15）",
            text_color="gray",
            font=ctk.CTkFont(size=12),
        ).grid(row=0, column=1, padx=(8, 0))

        # 對話字體大小
        ctk.CTkLabel(card, text="對話字體大小", font=ctk.CTkFont(weight="bold")).grid(
            row=2, column=0, padx=20, pady=(10, 6), sticky="w"
        )
        font_row = ctk.CTkFrame(card, fg_color="transparent")
        font_row.grid(row=2, column=1, padx=20, pady=(10, 6), sticky="w")

        self.font_size_entry = ctk.CTkEntry(font_row, width=70)
        self.font_size_entry.grid(row=0, column=0)

        ctk.CTkLabel(
            font_row,
            text="訊息泡泡與輸入框的字級，8–32（預設 13）",
            text_color="gray",
            font=ctk.CTkFont(size=12),
        ).grid(row=0, column=1, padx=(8, 0))

        # 工具呼叫上限
        ctk.CTkLabel(card, text="工具呼叫上限", font=ctk.CTkFont(weight="bold")).grid(
            row=3, column=0, padx=20, pady=(10, 6), sticky="w"
        )
        rounds_row = ctk.CTkFrame(card, fg_color="transparent")
        rounds_row.grid(row=3, column=1, padx=20, pady=(10, 6), sticky="w")

        self.tool_rounds_entry = ctk.CTkEntry(rounds_row, width=70)
        self.tool_rounds_entry.grid(row=0, column=0)

        ctk.CTkLabel(
            rounds_row,
            text="每則訊息最多跑幾輪工具呼叫（預設 10；瀏覽器自動化建議 25+）",
            text_color="gray",
            font=ctk.CTkFont(size=12),
        ).grid(row=0, column=1, padx=(8, 0))

        # CLI 指令免確認
        ctk.CTkLabel(card, text="CLI 指令", font=ctk.CTkFont(weight="bold")).grid(
            row=4, column=0, padx=20, pady=(10, 6), sticky="w"
        )
        self.cli_auto_var = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(
            card,
            text="免確認直接執行（風險自負；關閉時每次執行前會先詢問）",
            variable=self.cli_auto_var,
            font=ctk.CTkFont(size=12),
        ).grid(row=4, column=1, padx=20, pady=(10, 6), sticky="w")

        # 模型 / 推理強度改到 Chat 頁選擇
        ctk.CTkLabel(
            card,
            text="模型與推理強度請在 Chat 頁下方的選單選擇。",
            text_color="gray",
            font=ctk.CTkFont(size=12),
        ).grid(row=5, column=1, padx=20, pady=(0, 6), sticky="w")

        # 儲存
        ctk.CTkButton(card, text="儲存", command=self._save, width=100).grid(
            row=6, column=1, padx=20, pady=(6, 20), sticky="e"
        )

        # ── 軟體更新 ──────────────────────────────
        update_card = ctk.CTkFrame(self)
        update_card.grid(row=2, column=0, padx=30, pady=(4, 10), sticky="ew")
        update_card.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(update_card, text="軟體更新", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, padx=20, pady=(20, 6), sticky="w"
        )

        self.update_status_label = ctk.CTkLabel(
            update_card,
            text="",
            text_color="gray",
            font=ctk.CTkFont(size=12),
            anchor="w",
            justify="left",
        )
        self.update_status_label.grid(row=0, column=1, padx=20, pady=(20, 6), sticky="w")

        self.update_btn = ctk.CTkButton(update_card, text="檢查更新", width=110, command=self._check_update)
        self.update_btn.grid(row=1, column=1, padx=20, pady=(0, 20), sticky="w")

        self._update_check_result = None
        self._refresh_version_label()

        self._load()

    def _load(self):
        from agent.auth import load_config

        cfg = load_config()
        self.prompt_box.delete("1.0", "end")
        self.prompt_box.insert("1.0", cfg.get("system_prompt", "You are a helpful assistant."))

        self.bubble_lines_entry.delete(0, "end")
        self.bubble_lines_entry.insert(0, str(cfg.get("bubble_max_lines", 15)))

        self.font_size_entry.delete(0, "end")
        self.font_size_entry.insert(0, str(cfg.get("chat_font_size", 13)))

        import os

        from agent.core import DEFAULT_MAX_TOOL_ROUNDS

        env_default = int(os.getenv("MAX_TOOL_ROUNDS", DEFAULT_MAX_TOOL_ROUNDS))
        self.tool_rounds_entry.delete(0, "end")
        self.tool_rounds_entry.insert(0, str(cfg.get("max_tool_rounds", env_default)))

        self.cli_auto_var.set(bool(cfg.get("cli_auto_approve", False)))

    def _save(self):
        from tkinter import messagebox

        from agent.auth import load_config, save_config

        try:
            bubble_lines = int(self.bubble_lines_entry.get().strip())
            if bubble_lines < 1:
                raise ValueError
        except ValueError:
            messagebox.showwarning("數值無效", "泡泡最大行數請輸入 1 以上的整數。")
            return

        try:
            tool_rounds = int(self.tool_rounds_entry.get().strip())
            if tool_rounds < 1:
                raise ValueError
        except ValueError:
            messagebox.showwarning("數值無效", "工具呼叫上限請輸入 1 以上的整數。")
            return

        try:
            font_size = int(self.font_size_entry.get().strip())
            if not (8 <= font_size <= 32):
                raise ValueError
        except ValueError:
            messagebox.showwarning("數值無效", "對話字體大小請輸入 8–32 之間的整數。")
            return

        cfg = load_config()
        cfg["system_prompt"] = self.prompt_box.get("1.0", "end-1c").strip()
        cfg["bubble_max_lines"] = bubble_lines
        cfg["max_tool_rounds"] = tool_rounds
        cfg["cli_auto_approve"] = bool(self.cli_auto_var.get())
        cfg["chat_font_size"] = font_size
        save_config(cfg)

        if self.app.agent:
            self.app.agent.system_prompt = cfg["system_prompt"]
            self.app.agent.max_tool_rounds = tool_rounds

        ctk.CTkButton(self, text="✓ 已儲存", state="disabled", fg_color="green", width=100).place(x=0, y=0)
        self.after(1500, lambda: None)  # 簡單的視覺回饋

    def on_show(self):
        self._load()

    # ── 軟體更新 ──────────────────────────────────

    def _refresh_version_label(self):
        from agent.updater import current_version

        version = current_version()
        short = version[:7] if version else "未知"
        self.update_status_label.configure(text=f"目前版本：{short}")

    def _check_update(self):
        self.update_btn.configure(state="disabled", text="檢查中…")
        self.update_status_label.configure(text="正在查詢 GitHub…", text_color="gray")

        def worker():
            from agent.updater import check_for_update

            try:
                result = check_for_update()
                self.after(0, lambda: self._on_check_done(result))
            except Exception as e:
                self.after(0, lambda err=e: self._on_check_failed(err))

        threading.Thread(target=worker, daemon=True).start()

    def _on_check_done(self, result: dict):
        self._update_check_result = result
        self.update_btn.configure(state="normal")

        if not result["update_available"]:
            self.update_status_label.configure(text="已是最新版本", text_color="gray")
            self.update_btn.configure(text="檢查更新", command=self._check_update)
            return

        msg = result.get("latest_message", "")
        self.update_status_label.configure(
            text=f"發現新版本：{msg}" if msg else "發現新版本",
            text_color=("#1a56db", "#6ea8ff"),
        )
        self.update_btn.configure(text="立即更新", command=self._apply_update)

    def _on_check_failed(self, error: Exception):
        self.update_btn.configure(state="normal", text="檢查更新", command=self._check_update)
        self.update_status_label.configure(text=f"檢查失敗：{error}", text_color="red")

    def _apply_update(self):
        from tkinter import messagebox

        if not messagebox.askyesno(
            "套用更新",
            "即將下載並套用最新版本，完成後需要重新啟動程式，確定要繼續嗎？",
        ):
            return

        self.update_btn.configure(state="disabled", text="更新中…")

        def progress(msg: str):
            self.after(0, lambda: self.update_status_label.configure(text=msg, text_color=("#1a56db", "#6ea8ff")))

        def worker():
            from agent.updater import apply_update

            try:
                apply_update(progress=progress)
                self.after(0, self._on_update_done)
            except Exception as e:
                self.after(0, lambda err=e: self._on_update_failed(err))

        threading.Thread(target=worker, daemon=True).start()

    def _on_update_done(self):
        from tkinter import messagebox

        self.update_btn.configure(state="normal", text="檢查更新", command=self._check_update)
        self.update_status_label.configure(text="更新完成", text_color="green")
        self._refresh_version_label()

        if messagebox.askyesno("更新完成", "更新完成，需要重新啟動才會生效，現在重新啟動嗎？"):
            from agent.updater import relaunch_and_exit

            relaunch_and_exit()

    def _on_update_failed(self, error: Exception):
        self.update_btn.configure(state="normal", text="立即更新", command=self._apply_update)
        self.update_status_label.configure(text=f"更新失敗：{error}", text_color="red")
