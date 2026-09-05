"""Plugin 管理頁面：列出已安裝 plugin、匯入 zip、移除。"""

from tkinter import filedialog, messagebox

import customtkinter as ctk

from agent.plugins import install_plugin_zip, list_plugins, uninstall_plugin


class PluginsView(ctk.CTkFrame):
    def __init__(self, parent, app):
        super().__init__(parent, fg_color="transparent")
        self.app = app

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        self._build()

    def _build(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=30, pady=(28, 6))
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            header,
            text="Plugins",
            font=ctk.CTkFont(size=20, weight="bold"),
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkButton(
            header,
            text="匯入 Plugin ZIP",
            width=140,
            command=self._import_zip,
        ).grid(row=0, column=1, sticky="e")

        ctk.CTkLabel(
            self,
            text="Plugin 一次打包 skills（知識）與 MCP servers（工具）。"
            "安裝含 MCP server 的 plugin 後需重啟程式才會生效。",
            text_color="gray",
            font=ctk.CTkFont(size=12),
            anchor="w",
            justify="left",
        ).grid(row=1, column=0, sticky="ew", padx=30, pady=(0, 8))

        self._list_frame = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self._list_frame.grid(row=2, column=0, sticky="nsew", padx=24, pady=(0, 16))
        self._list_frame.grid_columnconfigure(0, weight=1)

        self.refresh()

    def refresh(self):
        for widget in self._list_frame.winfo_children():
            widget.destroy()

        plugins = list_plugins()

        if not plugins:
            ctk.CTkLabel(
                self._list_frame,
                text="還沒有安裝任何 plugin。按右上角「匯入 Plugin ZIP」安裝。",
                text_color="gray",
            ).grid(row=0, column=0, pady=24)
            return

        for row_index, plugin in enumerate(plugins):
            card = ctk.CTkFrame(self._list_frame)
            card.grid(row=row_index, column=0, sticky="ew", padx=6, pady=5)
            card.grid_columnconfigure(0, weight=1)

            title = plugin["name"]
            if plugin.get("version"):
                title += f"  v{plugin['version']}"

            ctk.CTkLabel(
                card,
                text=title,
                font=ctk.CTkFont(size=14, weight="bold"),
                anchor="w",
            ).grid(row=0, column=0, padx=16, pady=(10, 0), sticky="w")

            ctk.CTkLabel(
                card,
                text=plugin.get("description") or "（沒有描述）",
                text_color="gray",
                font=ctk.CTkFont(size=12),
                anchor="w",
            ).grid(row=1, column=0, padx=16, pady=(0, 2), sticky="w")

            parts = []
            if plugin.get("skills"):
                parts.append("Skills: " + ", ".join(plugin["skills"]))
            if plugin.get("commands"):
                parts.append("指令: " + ", ".join("/" + c for c in plugin["commands"]))
            if plugin.get("subagents"):
                parts.append("子代理: " + ", ".join(plugin["subagents"]))
            if plugin.get("mcp_servers"):
                parts.append("MCP: " + ", ".join(plugin["mcp_servers"]))

            ctk.CTkLabel(
                card,
                text="　|　".join(parts) or "（空）",
                text_color=("gray55", "gray45"),
                font=ctk.CTkFont(size=11),
                anchor="w",
            ).grid(row=2, column=0, padx=16, pady=(0, 10), sticky="w")

            ctk.CTkButton(
                card,
                text="移除",
                width=64,
                fg_color=("gray60", "gray35"),
                hover_color=("#b3261e", "#8c1d18"),
                command=lambda p=plugin: self._remove(p),
            ).grid(row=0, column=1, rowspan=3, padx=(4, 12))

    def _import_zip(self):
        zip_path = filedialog.askopenfilename(
            title="選擇 plugin zip 檔",
            filetypes=[("Zip 檔", "*.zip"), ("所有檔案", "*.*")],
        )
        if not zip_path:
            return

        try:
            info = install_plugin_zip(zip_path)
        except FileExistsError as e:
            if not messagebox.askyesno(
                "內容衝突",
                f"以下項目已存在：{e}\n要覆蓋安裝嗎？",
            ):
                return
            try:
                info = install_plugin_zip(zip_path, overwrite=True)
            except Exception as e2:
                messagebox.showerror("安裝失敗", str(e2))
                return
        except Exception as e:
            messagebox.showerror("安裝失敗", str(e))
            return

        summary = f"已安裝 plugin「{info['name']}」"
        if info.get("skills"):
            summary += f"\nSkills：{'、'.join(info['skills'])}（立即生效）"
        if info.get("commands"):
            summary += f"\n指令：{'、'.join('/' + c for c in info['commands'])}（立即生效）"
        if info.get("subagents"):
            summary += f"\n子代理：{'、'.join(info['subagents'])}（立即生效）"
        if info.get("mcp_servers"):
            summary += f"\nMCP servers：{'、'.join(info['mcp_servers'])}（重啟程式後生效）"
        messagebox.showinfo("安裝完成", summary)
        self.refresh()

    def _remove(self, plugin: dict):
        if not messagebox.askyesno(
            "移除 Plugin",
            f"確定要移除「{plugin['name']}」嗎？\n其安裝的 skills 與 MCP server 設定會一併刪除。",
        ):
            return

        try:
            uninstall_plugin(plugin["name"])
        except Exception as e:
            messagebox.showerror("移除失敗", str(e))
            return

        self.refresh()

    def on_show(self):
        self.refresh()
