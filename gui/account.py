"""帳號頁面：登入狀態、OAuth 登入/登出。"""

import logging
import threading
from pathlib import Path

import customtkinter as ctk
from PIL import Image

log = logging.getLogger("pocket-agent")


class AccountView(ctk.CTkFrame):
    def __init__(self, parent, app):
        super().__init__(parent, fg_color="transparent")
        self.app = app
        self.grid_columnconfigure(0, weight=1)
        self._build()

    def _build(self):
        ctk.CTkLabel(self, text="帳號", font=ctk.CTkFont(size=20, weight="bold")).grid(
            row=0, column=0, padx=30, pady=(28, 6), sticky="w"
        )

        card = ctk.CTkFrame(self)
        card.grid(row=1, column=0, padx=30, pady=10, sticky="ew")

        self.status_icon = ctk.CTkLabel(card, text="", font=ctk.CTkFont(size=32))
        self.status_icon.grid(row=0, column=0, padx=20, pady=(20, 4))

        self.status_label = ctk.CTkLabel(card, text="檢查中…", font=ctk.CTkFont(size=14))
        self.status_label.grid(row=1, column=0, padx=20, pady=(0, 20))

        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.grid(row=2, column=0, padx=30, pady=6, sticky="w")

        self.login_btn = ctk.CTkButton(btn_row, text="登入", command=self._login, width=190, height=40)
        self.login_btn.grid(row=0, column=0, padx=(0, 10))

        self.logout_btn = ctk.CTkButton(
            btn_row,
            text="登出",
            command=self._logout,
            fg_color=("gray60", "gray35"),
            hover_color=("gray50", "gray25"),
            width=90,
            height=40,
        )
        self.logout_btn.grid(row=0, column=1)

        self.url_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.url_frame.grid(row=3, column=0, padx=30, pady=(6, 0), sticky="ew")
        self.url_frame.grid_columnconfigure(0, weight=1)
        self.url_frame.grid_remove()

        ctk.CTkLabel(
            self.url_frame,
            text="授權網址（可選取複製）",
            text_color="gray",
            font=ctk.CTkFont(size=11),
            anchor="w",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 2))

        self.url_box = ctk.CTkTextbox(
            self.url_frame,
            height=58,
            wrap="char",
            font=ctk.CTkFont(size=11),
            activate_scrollbars=True,
        )
        self.url_box.grid(row=1, column=0, sticky="ew", padx=(0, 8))

        self.copy_btn = ctk.CTkButton(
            self.url_frame,
            text="複製",
            width=64,
            height=30,
            command=self._copy_auth_url,
        )
        self.copy_btn.grid(row=1, column=1, sticky="n")

        ctk.CTkLabel(
            self,
            text="登入後 token 會安全存放在本機，重啟不需要再次登入。",
            text_color="gray",
            font=ctk.CTkFont(size=12),
            justify="left",
        ).grid(row=4, column=0, padx=30, pady=(10, 0), sticky="w")

        self._build_provider_settings()

        self._auth_url = ""
        self._refresh()

    def _build_provider_settings(self):
        settings_frame = ctk.CTkFrame(self, fg_color="transparent")
        settings_frame.grid(row=5, column=0, padx=30, pady=(20, 0), sticky="ew")

        ctk.CTkLabel(settings_frame, text="AI LLM Provider 設定", font=ctk.CTkFont(size=16, weight="bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 10)
        )

        from agent.auth import load_config

        cfg = load_config()

        self.provider_var = ctk.StringVar(value=cfg.get("provider", "openai"))

        radio_frame = ctk.CTkFrame(settings_frame, fg_color="transparent")
        radio_frame.grid(row=1, column=0, sticky="w")

        ctk.CTkRadioButton(
            radio_frame, text="OpenAI", variable=self.provider_var, value="openai", command=self._on_provider_change
        ).grid(row=0, column=0, padx=(0, 10), pady=(0, 10))

        ctk.CTkRadioButton(
            radio_frame,
            text="Ollama",
            variable=self.provider_var,
            value="ollama",
            command=self._on_provider_change,
        ).grid(row=0, column=1, padx=(0, 10), pady=(0, 10))

        ctk.CTkRadioButton(
            radio_frame,
            text="OpenRouter",
            variable=self.provider_var,
            value="openrouter",
            command=self._on_provider_change,
        ).grid(row=1, column=0, padx=(0, 10))

    def _on_provider_change(self):
        provider = self.provider_var.get()

        from agent.auth import load_config, save_config

        cfg = load_config()
        if cfg.get("provider", "openai") != provider:
            cfg["provider"] = provider
            save_config(cfg)

            self._hide_auth_url()
            self.app.agent = None
            self.app.mcp = None
            self.app.is_ready = False
            self._refresh()
            self.app.reinit()

    def _refresh(self):

        assets_dir = Path(__file__).resolve().parent / "assets"

        self.logged_in_image = ctk.CTkImage(
            light_image=Image.open(assets_dir / "chatgpt_logo.png"),
            dark_image=Image.open(assets_dir / "chatgpt_logo.png"),
            size=(42, 42),
        )

        self.logged_out_image = ctk.CTkImage(
            light_image=Image.open(assets_dir / "log_out.png"),
            dark_image=Image.open(assets_dir / "log_out.png"),
            size=(64, 64),
        )

        from agent.auth import get_user_info, has_valid_token, load_config

        cfg = load_config()
        self.provider = cfg.get("provider", "openai")

        if self.provider in ("ollama", "openrouter"):
            self.status_icon.configure(image=self.logged_in_image, text="")
            if self.provider == "ollama":
                label_text = "使用 Ollama 本地模型"
            else:
                label_text = f"使用 {self.provider.upper()} (.env 金鑰)"
            self.status_label.configure(text=label_text, text_color=("gray10", "gray90"))
            self.login_btn.configure(text="無須網頁登入", state="disabled")
            self.logout_btn.configure(state="disabled")
        elif has_valid_token(self.provider):
            info = get_user_info(self.provider)
            email = info.get("email", "")
            plan = info.get("plan", "")

            self.status_icon.configure(image=self.logged_in_image, text="")

            self.status_label.configure(
                text=f"已登入 ({self.provider.upper()})\n{email}" + (f"\n{plan.capitalize()}" if plan else ""),
                text_color=("gray10", "gray90"),
            )
            self.login_btn.configure(text="重新登入", state="normal")
            self.logout_btn.configure(state="normal")
        else:
            self.status_icon.configure(image=self.logged_out_image, text="")
            self.status_label.configure(text=f"尚未登入 ({self.provider.upper()})", text_color="gray")
            self.login_btn.configure(text=f"登入 {self.provider.upper()} 帳號", state="normal")
            self.logout_btn.configure(state="normal")

    def on_show(self):
        self._refresh()

    def _login(self):
        if getattr(self, "_login_cancel", None) is not None:
            self._login_cancel.set()

        self._login_cancel = threading.Event()
        cancel = self._login_cancel

        self.login_btn.configure(state="disabled", text="登入中…")
        self.status_icon.configure(text="⏳")
        self.status_label.configure(
            text="正在開啟瀏覽器…",
            text_color="gray",
        )

        def _on_auth_url(url: str, opened: bool):
            self.app.after(0, lambda: self._show_auth_url(url, opened))

        def _do():
            try:
                from agent.auth import get_access_token

                get_access_token(
                    provider=self.provider,
                    on_auth_url=_on_auth_url,
                    cancel_event=cancel,
                )
                self.app.after(0, self._on_login_ok)
            except Exception as e:
                err_msg = str(e)
                self.app.after(0, lambda m=err_msg: self.show_error(m))

        threading.Thread(target=_do, daemon=True).start()

    def _show_auth_url(self, url: str, opened: bool):
        self._auth_url = url
        self.url_box.configure(state="normal")
        self.url_box.delete("1.0", "end")
        self.url_box.insert("1.0", url)
        self.url_box.configure(state="disabled")
        self.url_frame.grid()

        copied = self._copy_auth_url(silent=True)

        if opened:
            self.status_icon.configure(text="⏳")
            self.status_label.configure(
                text="瀏覽器已開啟，請完成授權後回到此視窗…\n沒反應或開錯瀏覽器的話，用下面的網址自己貼。",
                text_color="gray",
            )
        else:
            self.status_icon.configure(text="⚠")
            self.status_label.configure(
                text="無法自動開啟瀏覽器。\n請複製下面的網址，貼到瀏覽器完成登入。",
                text_color="orange",
            )

        if not copied:
            log.warning("無法寫入剪貼簿，使用者需自行從輸入框選取")

    def _copy_auth_url(self, silent: bool = False) -> bool:
        if not getattr(self, "_auth_url", ""):
            return False
        try:
            self.clipboard_clear()
            self.clipboard_append(self._auth_url)
            self.update_idletasks()
        except Exception:
            log.exception("複製授權網址到剪貼簿失敗")
            if not silent:
                self.copy_btn.configure(text="失敗")
                self.after(1500, lambda: self.copy_btn.configure(text="複製"))
            return False

        if not silent:
            self.copy_btn.configure(text="已複製")
            self.after(1500, lambda: self.copy_btn.configure(text="複製"))
        return True

    def _hide_auth_url(self):
        self._auth_url = ""
        self.url_frame.grid_remove()

    def _on_login_ok(self):
        self._hide_auth_url()
        self._refresh()
        self.login_btn.configure(state="normal")
        self.app.reinit()

    def show_error(self, msg: str):
        self.login_btn.configure(state="normal", text="重新登入")
        self.status_icon.configure(text="❌")
        self.status_label.configure(text=f"登入失敗：\n{msg}", text_color="red")

    def _logout(self):
        from agent.auth import logout

        if getattr(self, "_login_cancel", None) is not None:
            self._login_cancel.set()
            self._login_cancel = None

        logout(self.provider)
        self._hide_auth_url()
        self.app.agent = None
        self.app.mcp = None
        self.app.is_ready = False
        self._refresh()
