"""主視窗：導覽列 + 背景 asyncio loop + 共用 agent 狀態。"""

import asyncio
import logging
import queue
import threading
from pathlib import Path

import customtkinter as ctk
from PIL import Image
from tkinterdnd2 import TkinterDnD


# ── 外觀設定 ──────────────────────────────────────

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


# ── 圖片路徑設定 ──────────────────────────────────
# 走 bundle_dir() 建立 assets 絕對路徑，避免從不同工作目錄啟動時
# 找不到圖片；打包成 exe 後也能指到隨附的資源目錄。

from agent.paths import bundle_dir

log = logging.getLogger("pocket-agent")

BASE_DIR = bundle_dir() / "gui"
ASSET_DIR = BASE_DIR / "assets"


class DnDCTk(TkinterDnD.DnDWrapper, ctk.CTk):
    """同時支援 CustomTkinter 與檔案拖放的主視窗。"""

    def __init__(self, *args, **kwargs):
        ctk.CTk.__init__(self, *args, **kwargs)
        self.TkdndVersion = TkinterDnD._require(self)


class App(DnDCTk):
    def __init__(self):
        super().__init__()

        # Tk 預設把回呼裡的例外印到 stderr。可攜版用 pythonw.exe 啟動，
        # stderr 是 None，這些例外等於憑空消失；dev 模式也只進 console，
        # 事後查不到。導進 log 檔，才不會出現「畫面沒反應但毫無線索」。
        self.report_callback_exception = self._log_tk_exception

        self.title("My Agent")

        self.geometry("980x660")
        self.minsize(720, 480)

        # ── 背景 asyncio loop（給 agent / MCP 使用）──
        self._loop = asyncio.new_event_loop()

        threading.Thread(
            target=self._loop.run_forever,
            daemon=True,
        ).start()

        self._queue: queue.Queue = queue.Queue()

        # ── 共用資源 ──────────────────────────────
        self.mcp = None
        self.agent = None
        self.is_ready = False
        self.current_session_id: str | None = None

        # ── 圖片資源 ──────────────────────────────
        # 將 CTkImage 儲存在物件屬性中，
        # 避免圖片物件被 Python 垃圾回收後消失。
        self._app_icon: ctk.CTkImage | None = None
        self._nav_icons: dict[str, ctk.CTkImage] = {}

        # ── UI 元件參照 ───────────────────────────
        self._nav_btns: dict[str, ctk.CTkButton] = {}
        self._views: dict[str, ctk.CTkFrame] = {}

        self._build()
        self._init_agent()
        self._poll()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── 例外記錄 ──────────────────────────────────

    def _log_tk_exception(self, exc_type, exc_value, exc_tb) -> None:
        """把 Tk 回呼裡的未捕捉例外寫進 log 而不是丟掉。

        這類例外最麻煩的地方是它會靜默中斷 after 排程鏈：某個回呼炸掉
        之後就不會再排下一次，畫面停在半途卻沒有任何錯誤訊息。
        """
        log.error(
            "Tk 回呼未捕捉例外",
            exc_info=(exc_type, exc_value, exc_tb),
        )

    # ── 圖片載入工具 ──────────────────────────────

    @staticmethod
    def _open_image(image_path: Path) -> Image.Image:
        """
        載入 PNG 圖片並轉成 RGBA 格式。

        若圖片不存在，直接提供明確錯誤訊息。
        """

        if not image_path.exists():
            raise FileNotFoundError(f"找不到圖片：{image_path}\n請確認圖片已放入 assets 資料夾。")

        return Image.open(image_path).convert("RGBA")

    def _load_images(self) -> None:
        """
        載入標題圖片及側邊導覽列 icon。

        圖片位置：
        assets/icon_agent.png
        assets/icon_chat.png
        assets/icon_mcp.png
        assets/icon_settings.png
        assets/icon_account.png
        """

        # ── My Agent 標題圖片 ─────────────────────
        # 取代原本 text="🤖 My Agent" 中的機器人 Emoji。
        agent_image = self._open_image(ASSET_DIR / "icon_agent.png")

        self._app_icon = ctk.CTkImage(
            light_image=agent_image,
            dark_image=agent_image,
            size=(64, 64),
        )

        # ── 導覽按鈕圖片 ─────────────────────────
        nav_icon_files = {
            "chat": ASSET_DIR / "icon_chat.png",
            "mcp": ASSET_DIR / "icon_mcp.png",
            "skills": ASSET_DIR / "icon_skill.png",
            "automation": ASSET_DIR / "icon_auto.png",
            "plugins": ASSET_DIR / "icon_plugins.png",
            "settings": ASSET_DIR / "icon_settings.png",
            "account": ASSET_DIR / "icon_account.png",
        }

        # PNG 原始尺寸可為 128×128，
        # CTkImage 會將其縮小為 24×24 顯示。
        nav_icon_size = (24, 24)

        for key, image_path in nav_icon_files.items():
            image = self._open_image(image_path)

            self._nav_icons[key] = ctk.CTkImage(
                light_image=image,
                dark_image=image,
                size=nav_icon_size,
            )

    # ── Layout ────────────────────────────────────

    def _build(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # 建立畫面前先載入所有圖片。
        self._load_images()

        # ── Sidebar ───────────────────────────────
        sidebar = ctk.CTkFrame(
            self,
            width=200,
            corner_radius=0,
        )
        sidebar.grid(
            row=0,
            column=0,
            sticky="nsew",
        )
        sidebar.grid_propagate(False)
        sidebar.grid_columnconfigure(0, weight=1)

        # 對話紀錄「列表」（row 11）占用剩餘垂直空間；
        # row 10 是標題列，不能給 weight，否則放大視窗時標題格會被撐開。
        sidebar.grid_rowconfigure(11, weight=1)

        # ── My Agent 標題 ─────────────────────────
        # 原本：
        #
        # ctk.CTkLabel(
        #     sidebar,
        #     text="🤖 My Agent",
        # )
        #
        # 修改為使用 agent.png 取代 Emoji。
        # compound="left" 表示圖片顯示於文字左側。
        ctk.CTkLabel(
            sidebar,
            text="  My Agent",
            image=self._app_icon,
            compound="left",
            font=ctk.CTkFont(
                size=18,
                weight="bold",
            ),
        ).grid(
            row=0,
            column=0,
            padx=20,
            pady=(24, 20),
        )

        # ── 導覽按鈕 ──────────────────────────────
        # 按鈕文字已移除 Emoji，改由 PNG 圖片顯示。
        nav_items = [
            (" Chat", "chat"),
            (" MCP 工具", "mcp"),
            (" Skills", "skills"),
            (" 自動化", "automation"),
            (" Plugins", "plugins"),
            (" 設定", "settings"),
            (" 帳號", "account"),
        ]

        for row_index, (label, key) in enumerate(
            nav_items,
            start=1,
        ):
            btn = ctk.CTkButton(
                sidebar,
                # 純文字標籤，不再放 Emoji。
                text=label,
                font=ctk.CTkFont(
                    size=14,
                ),
                # 使用對應的透明背景 PNG。
                image=self._nav_icons[key],
                # 圖片顯示於文字左側。
                compound="left",
                # 讓圖片與文字靠按鈕左側排列。
                anchor="w",
                fg_color="transparent",
                text_color=("gray10", "gray90"),
                hover_color=("gray70", "gray30"),
                height=42,
                corner_radius=8,
                command=lambda k=key: self.show(k),
            )

            btn.grid(
                row=row_index,
                column=0,
                padx=10,
                pady=3,
                sticky="ew",
            )

            self._nav_btns[key] = btn

        separator = ctk.CTkFrame(
            sidebar,
            height=2,
            fg_color=("gray20", "gray50"),
            corner_radius=0,
        )

        separator.grid(
            row=8,
            column=0,
            padx=10,
            pady=(8, 4),
            sticky="ew",
        )

        separator.grid_propagate(False)

        # 新對話
        icon_path = Path(__file__).parent / "assets" / "icon_new_chat.png"

        self.new_chat_icon = ctk.CTkImage(
            light_image=Image.open(icon_path),
            dark_image=Image.open(icon_path),
            size=(32, 32),
        )

        ctk.CTkButton(
            sidebar,
            text="  新對話",
            font=ctk.CTkFont(
                size=14,
            ),
            image=self.new_chat_icon,
            anchor="w",
            fg_color="transparent",
            text_color=("gray10", "gray90"),
            hover_color=("gray70", "gray30"),
            height=34,
            corner_radius=8,
            command=self.new_chat,
        ).grid(
            row=9,
            column=0,
            padx=10,
            pady=(16, 2),
            sticky="ew",
        )

        # 對話紀錄
        icon_path = Path(__file__).parent / "assets" / "icon_chat_history.png"

        self.chat_history_icon = ctk.CTkImage(
            light_image=Image.open(icon_path),
            dark_image=Image.open(icon_path),
            size=(32, 32),
        )

        ctk.CTkLabel(
            sidebar,
            text="     對話紀錄",
            font=ctk.CTkFont(size=14),
            text_color="gray",
            image=self.chat_history_icon,
            anchor="w",
            compound="left",
        ).grid(
            row=10,
            column=0,
            padx=20,
            pady=(6, 0),
            sticky="ew",
        )

        self._session_frame = ctk.CTkScrollableFrame(
            sidebar,
            fg_color="transparent",
        )
        self._session_frame.grid(
            row=11,
            column=0,
            padx=4,
            pady=(2, 8),
            sticky="nsew",
        )
        self._session_frame.grid_columnconfigure(
            0,
            weight=1,
        )

        # ── Content ───────────────────────────────
        content = ctk.CTkFrame(
            self,
            corner_radius=0,
            fg_color=("gray96", "gray11"),
        )
        content.grid(
            row=0,
            column=1,
            sticky="nsew",
        )
        content.grid_rowconfigure(0, weight=1)
        content.grid_columnconfigure(0, weight=1)

        from gui.chat import ChatView
        from gui.mcp import MCPView
        from gui.skills import SkillsView
        from gui.automation import AutomationView
        from gui.plugins import PluginsView
        from gui.settings import SettingsView
        from gui.account import AccountView

        self._views = {
            "chat": ChatView(content, self),
            "mcp": MCPView(content, self),
            "skills": SkillsView(content, self),
            "automation": AutomationView(content, self),
            "plugins": PluginsView(content, self),
            "settings": SettingsView(content, self),
            "account": AccountView(content, self),
        }

        for view in self._views.values():
            view.grid(
                row=0,
                column=0,
                sticky="nsew",
            )

        self.show("chat")
        self.refresh_sessions()

    def show(self, key: str):
        """切換主要內容頁面並更新導覽按鈕狀態。"""

        for nav_key, btn in self._nav_btns.items():
            btn.configure(fg_color=(("gray75", "gray25") if nav_key == key else "transparent"))

        self._views[key].tkraise()

        if hasattr(self._views[key], "on_show"):
            self._views[key].on_show()

    # ── 對話 session 管理 ─────────────────────────

    def new_chat(self):
        """建立新對話並清除目前的 agent 歷史紀錄。"""

        if self.agent:
            self.agent.clear_history()

        self.current_session_id = None
        self._views["chat"].reset()
        self.refresh_sessions()
        self.show("chat")

    def load_chat(self, session_id: str):
        """載入指定對話紀錄。"""

        if not self.is_ready:
            return

        from agent import sessions

        data = sessions.load_session(session_id)

        if not data:
            self.refresh_sessions()
            return

        self.agent.clear_history()
        self.agent.history.extend(data.get("history", []))

        self.current_session_id = session_id

        self._views["chat"].render_history(self.agent.history)

        self.refresh_sessions()
        self.show("chat")

    def save_current_chat(self):
        """每輪對話後自動存檔，由 ChatView 呼叫。"""

        from agent import sessions

        if not self.agent or not self.agent.history:
            return

        if not self.current_session_id:
            self.current_session_id = sessions.new_session_id()

        sessions.save_session(
            self.current_session_id,
            self.agent.history,
        )

        self.refresh_sessions()

    def delete_chat(self, session_id: str):
        """刪除指定對話紀錄。"""

        from agent import sessions

        sessions.delete_session(session_id)

        if session_id == self.current_session_id:
            self.new_chat()
        else:
            self.refresh_sessions()

    def refresh_sessions(self):
        """重新建立側邊欄的對話紀錄列表。"""

        from agent import sessions

        for widget in self._session_frame.winfo_children():
            widget.destroy()

        self._session_frame.grid_columnconfigure(
            0,
            weight=1,
        )

        session_list = sessions.list_sessions()

        for row_index, meta in enumerate(session_list):
            session_id = meta["id"]

            # 不要在這裡使用 lstrip()，避免標題開頭被移除
            title = str(meta.get("title", "未命名對話"))

            if not title:
                title = "未命名對話"

            active = session_id == self.current_session_id

            display_title = title[:18] + "…" if len(title) > 18 else title

            row_frame = ctk.CTkFrame(
                self._session_frame,
                height=32,
                corner_radius=6,
                fg_color=(("gray75", "gray25") if active else "transparent"),
            )
            row_frame.grid(
                row=row_index,
                column=0,
                sticky="ew",
                padx=(6, 4),
                pady=1,
            )

            row_frame.grid_columnconfigure(
                0,
                weight=1,
                minsize=0,
            )
            row_frame.grid_columnconfigure(
                1,
                weight=0,
                minsize=28,
            )

            # 使用 Label 顯示標題，避免 CTkButton 文字被裁切
            title_label = ctk.CTkLabel(
                row_frame,
                text=display_title,
                height=30,
                anchor="w",
                justify="left",
                font=ctk.CTkFont(size=12),
                text_color=("gray10", "gray90"),
            )
            title_label.grid(
                row=0,
                column=0,
                sticky="ew",
                padx=(10, 4),
            )

            delete_button = ctk.CTkButton(
                row_frame,
                text="✕",
                width=26,
                height=26,
                corner_radius=6,
                fg_color="transparent",
                text_color="gray",
                hover_color=("gray70", "gray30"),
                command=(lambda s=session_id: self.delete_chat(s)),
            )
            delete_button.grid(
                row=0,
                column=1,
                padx=(0, 2),
                pady=2,
            )

            def open_session(
                event=None,
                sid=session_id,
            ):
                self.load_chat(sid)

            def enter_row(
                event=None,
                frame=row_frame,
                is_active=active,
            ):
                if not is_active:
                    frame.configure(fg_color=("gray85", "gray20"))

            def leave_row(
                event=None,
                frame=row_frame,
                is_active=active,
            ):
                if not is_active:
                    frame.configure(fg_color="transparent")

            # 點擊標題或空白列都可以切換對話
            row_frame.bind(
                "<Button-1>",
                open_session,
            )
            title_label.bind(
                "<Button-1>",
                open_session,
            )

            row_frame.bind(
                "<Enter>",
                enter_row,
            )
            title_label.bind(
                "<Enter>",
                enter_row,
            )

            row_frame.bind(
                "<Leave>",
                leave_row,
            )
            title_label.bind(
                "<Leave>",
                leave_row,
            )

    # ── Agent init ────────────────────────────────

    def _init_agent(self):
        """初始化 OpenAI client、MCP manager 與 Agent。"""

        from agent.auth import (
            has_valid_token,
            get_openai_client,
            get_model,
            load_config,
        )

        if not has_valid_token():
            self._queue.put(("need_login", None))
            return

        async def _init():
            try:
                from agent.core import Agent
                from agent.mcp_manager import MCPManager

                client = get_openai_client()

                mcp = MCPManager()
                await mcp.start()

                cfg = load_config()

                agent = Agent(
                    client=client,
                    mcp=mcp,
                    model=get_model(),
                    reasoning_effort=cfg.get(
                        "reasoning_effort",
                        "medium",
                    ),
                    max_tool_rounds=cfg.get("max_tool_rounds"),
                )

                self._queue.put(("ready", (mcp, agent)))

            except Exception as error:
                self._queue.put(("error", str(error)))

        asyncio.run_coroutine_threadsafe(
            _init(),
            self._loop,
        )

    def reinit(self):
        """登入後重新初始化 agent。"""

        self._init_agent()

    # ── Queue 輪詢 ────────────────────────────────

    def _poll(self):
        """定期檢查背景執行緒回傳事件。"""

        try:
            while True:
                kind, data = self._queue.get_nowait()

                if kind == "ready":
                    self.mcp, self.agent = data
                    self.is_ready = True

                    for view in self._views.values():
                        if hasattr(
                            view,
                            "on_agent_ready",
                        ):
                            view.on_agent_ready()

                elif kind in (
                    "error",
                    "need_login",
                ):
                    self.show("account")

        except queue.Empty:
            pass

        self.after(100, self._poll)

    # ── Async helper ──────────────────────────────

    def run_async(self, coro):
        """將 coroutine 送至背景 asyncio loop 執行。"""

        return asyncio.run_coroutine_threadsafe(
            coro,
            self._loop,
        )

    # ── Cleanup ───────────────────────────────────

    def _on_close(self):
        """關閉程式前停止 MCP。"""

        if self.mcp:
            asyncio.run_coroutine_threadsafe(
                self.mcp.stop(),
                self._loop,
            )

        self.destroy()


if __name__ == "__main__":
    app = App()
    app.mainloop()
