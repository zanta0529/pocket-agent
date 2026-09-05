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

        # 狀態卡片
        card = ctk.CTkFrame(self)
        card.grid(row=1, column=0, padx=30, pady=10, sticky="ew")

        self.status_icon = ctk.CTkLabel(card, text="", font=ctk.CTkFont(size=32))
        self.status_icon.grid(row=0, column=0, padx=20, pady=(20, 4))

        self.status_label = ctk.CTkLabel(card, text="檢查中…", font=ctk.CTkFont(size=14))
        self.status_label.grid(row=1, column=0, padx=20, pady=(0, 20))

        # 按鈕列
        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.grid(row=2, column=0, padx=30, pady=6, sticky="w")

        self.login_btn = ctk.CTkButton(btn_row, text="登入 OpenAI 帳號", command=self._login, width=190, height=40)
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

        # 授權網址區塊：登入期間一直顯示，讓使用者能自己複製貼到瀏覽器。
        # 自動開啟瀏覽器不見得成功（開錯瀏覽器、開到沒登入的設定檔、
        # 公司環境的預設程式關聯異常），有網址在手就永遠有退路。
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

        # 用 Textbox 而非 Label：Label 的文字選不起來，也就複製不了
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

        # 說明
        ctk.CTkLabel(
            self,
            text="需要 ChatGPT Plus 或 Pro 訂閱才能使用。\n登入後 token 會安全存放在本機，重啟不需要再次登入。",
            text_color="gray",
            font=ctk.CTkFont(size=12),
            justify="left",
        ).grid(row=4, column=0, padx=30, pady=(10, 0), sticky="w")

        self._auth_url = ""
        self._refresh()

    # ── 狀態顯示 ─────────────────────────────────

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

        from agent.auth import get_user_info, has_valid_token

        if has_valid_token():
            info = get_user_info()
            email = info.get("email", "")
            plan = info.get("plan", "")

            self.status_icon.configure(image=self.logged_in_image, text="")

            self.status_label.configure(
                text=f"已登入\n{email}" + (f"\nChatGPT {plan.capitalize()}" if plan else ""),
                text_color=("gray10", "gray90"),
            )

            # 一律恢復可按：_refresh 是唯一反映目前帳號狀態的地方，
            # 只改文字不改 state 的話，登入中途失敗後按登出，
            # 按鈕會維持反白且再也按不下去，只能重開程式。
            self.login_btn.configure(text="重新登入", state="normal")

        else:
            self.status_icon.configure(image=self.logged_out_image, text="")

            self.status_label.configure(text="尚未登入", text_color="gray")

            self.login_btn.configure(
                text="登入 OpenAI 帳號",
                state="normal",
            )

    def on_show(self):
        self._refresh()

    # ── 登入 ─────────────────────────────────────

    def _login(self):
        # 取消上一次還沒結束的登入，否則它的 callback server 會一直
        # 占著 port 1455，下一次登入綁不上而整個流程走不下去。
        if getattr(self, "_login_cancel", None) is not None:
            self._login_cancel.set()

        self._login_cancel = threading.Event()
        cancel = self._login_cancel

        self.login_btn.configure(state="disabled", text="登入中…")
        self.status_icon.configure(text="⏳")
        # 這裡還沒真的開瀏覽器——實際結果由 on_auth_url 回報後才更新。
        # 先寫死「瀏覽器已開啟」的話，開不起來時使用者只會看到一句
        # 與事實不符的提示，也拿不到可以自己貼的網址。
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
                    on_auth_url=_on_auth_url,
                    cancel_event=cancel,
                )
                self.app.after(0, self._on_login_ok)
            except Exception as e:
                err_msg = str(e)
                self.app.after(0, lambda m=err_msg: self.show_error(m))

        threading.Thread(target=_do, daemon=True).start()

    def _show_auth_url(self, url: str, opened: bool):
        """把授權網址攤出來，不論瀏覽器有沒有自動開起來。

        自動開啟成功也照樣顯示：可能開到沒登入該帳號的瀏覽器設定檔，
        或使用者想改用無痕視窗。手上有網址就永遠有辦法完成登入。
        """
        self._auth_url = url

        self.url_box.configure(state="normal")
        self.url_box.delete("1.0", "end")
        self.url_box.insert("1.0", url)
        self.url_box.configure(state="disabled")  # 唯讀但仍可選取複製
        self.url_frame.grid()

        # 一併放進剪貼簿，多數情況直接貼上就好
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
        """複製授權網址到剪貼簿；回傳是否成功。"""
        if not getattr(self, "_auth_url", ""):
            return False
        try:
            self.clipboard_clear()
            self.clipboard_append(self._auth_url)
            self.update_idletasks()  # 沒有這行，剪貼簿在某些情況不會生效
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
        # 刻意不收起網址區塊：自動流程失敗時，手動貼網址往往還能成功
        self.login_btn.configure(state="normal", text="重新登入")
        self.status_icon.configure(text="❌")
        self.status_label.configure(text=f"登入失敗：\n{msg}", text_color="red")

    # ── 登出 ─────────────────────────────────────

    def _logout(self):
        from agent.auth import logout

        # 登入進行到一半就按登出：一併中止，讓 callback server 收掉
        if getattr(self, "_login_cancel", None) is not None:
            self._login_cancel.set()
            self._login_cancel = None

        logout()
        self._hide_auth_url()
        self.app.agent = None
        self.app.mcp = None
        self.app.is_ready = False
        self._refresh()
