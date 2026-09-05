"""MCP 工具頁面：顯示、新增、刪除、編輯 inject 參數。"""

from pathlib import Path

import yaml
import customtkinter as ctk

from agent.paths import mcp_config_path, mcp_example_path

MCP_CONFIG = mcp_config_path()  # 個人設定（可寫，不 commit）
MCP_EXAMPLE = mcp_example_path()  # 範本（隨程式發佈，唯讀）


def _load_servers() -> dict:
    src = MCP_CONFIG if MCP_CONFIG.exists() else MCP_EXAMPLE
    if src.exists():
        cfg = yaml.safe_load(src.read_text(encoding="utf-8")) or {}
        return {k: v for k, v in (cfg.get("servers") or {}).items() if v}
    return {}


def _save_servers(servers: dict):
    # 永遠寫到 mcp_config.yaml（個人檔，不 commit）
    cfg = yaml.safe_load(MCP_CONFIG.read_text(encoding="utf-8")) if MCP_CONFIG.exists() else {}
    cfg = cfg or {}
    cfg["servers"] = servers
    MCP_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    MCP_CONFIG.write_text(yaml.dump(cfg, allow_unicode=True, default_flow_style=False), encoding="utf-8")


def _inject_to_str(inject: dict) -> str:
    """inject dict → 'key=value, key2=value2' 字串。"""
    return ", ".join(f"{k}={v}" for k, v in inject.items())


def _str_to_inject(s: str) -> dict:
    """'key=value, key2=value2' → dict。"""
    result = {}
    for pair in s.split(","):
        pair = pair.strip()
        if "=" in pair:
            k, _, v = pair.partition("=")
            k, v = k.strip(), v.strip()
            if k:
                result[k] = v
    return result


class MCPView(ctk.CTkFrame):
    def __init__(self, parent, app):
        super().__init__(parent, fg_color="transparent")

        self.app = app

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        self._build()

    def _build(self):

        top = ctk.CTkFrame(self, fg_color="transparent")

        top.grid(row=0, column=0, padx=30, pady=(28, 0), sticky="ew")

        top.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(top, text="MCP 工具", font=ctk.CTkFont(size=20, weight="bold")).grid(row=0, column=0, sticky="w")

        ctk.CTkButton(top, text="＋ 新增 Server", width=140, command=lambda: self._open_server_dialog("add")).grid(
            row=0, column=1
        )

        ctk.CTkLabel(
            self, text="注意：重新啟動 agent 後 MCP 設定才會生效。", text_color="red", font=ctk.CTkFont(size=16)
        ).grid(row=1, column=0, padx=30, pady=(4, 0), sticky="w")

        self.list_frame = ctk.CTkScrollableFrame(self, label_text="")

        self.list_frame.grid(row=2, column=0, padx=30, pady=12, sticky="nsew")

        self.list_frame.grid_columnconfigure(0, weight=1)

        self._refresh()

    # =========================
    # MCP Server 列表
    # =========================

    def _refresh(self):

        for w in self.list_frame.winfo_children():
            w.destroy()

        servers = _load_servers()

        if not servers:
            ctk.CTkLabel(self.list_frame, text="尚未設定任何 MCP server。", text_color="gray").pack(pady=20)

            return

        for name, spec in servers.items():
            self._server_row(name, spec)

    def _server_row(self, name: str, spec: dict):

        card = ctk.CTkFrame(self.list_frame)

        card.pack(fill="x", pady=4)

        card.grid_columnconfigure(0, weight=1)

        transport = spec.get("transport", "stdio")

        if transport == "stdio":
            detail = f"{spec.get('command', '')} {' '.join(spec.get('args', []))}"

        else:
            detail = spec.get("url", "")

        # -------------------------
        # 標題列
        # -------------------------

        top_row = ctk.CTkFrame(card, fg_color="transparent")

        top_row.pack(fill="x", padx=10, pady=(10, 2))

        top_row.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(top_row, text=name, font=ctk.CTkFont(weight="bold"), anchor="w").grid(row=0, column=0, sticky="w")

        # 編輯按鈕

        ctk.CTkButton(top_row, text="編輯", width=60, command=lambda n=name: self._open_server_dialog("edit", n)).grid(
            row=0, column=1, padx=4
        )

        # 刪除按鈕

        ctk.CTkButton(
            top_row,
            text="刪除",
            width=60,
            fg_color=("gray60", "gray35"),
            hover_color=("gray50", "gray25"),
            command=lambda n=name: self._remove(n),
        ).grid(row=0, column=2)

        ctk.CTkLabel(
            card, text=f"[{transport}]  {detail}", text_color="gray", font=ctk.CTkFont(size=12), anchor="w"
        ).pack(fill="x", padx=12, pady=(0, 4))

        # -------------------------
        # inject
        # -------------------------

        inj_frame = ctk.CTkFrame(card, fg_color=("gray88", "gray20"), corner_radius=8)

        inj_frame.pack(fill="x", padx=10, pady=(0, 10))

        inj_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(inj_frame, text="inject", font=ctk.CTkFont(size=11, weight="bold"), text_color="gray").grid(
            row=0, column=0, padx=10, pady=6
        )

        inject_var = ctk.StringVar(value=_inject_to_str(spec.get("inject") or {}))

        inject_entry = ctk.CTkEntry(inj_frame, textvariable=inject_var, placeholder_text="key=value, key2=value2")

        inject_entry.grid(row=0, column=1, padx=(0, 6), pady=6, sticky="ew")

        def save_inject(n=name, var=inject_var):

            servers = _load_servers()

            if n in servers:
                new_inject = _str_to_inject(var.get())

                if new_inject:
                    servers[n]["inject"] = new_inject

                elif "inject" in servers[n]:
                    del servers[n]["inject"]

                _save_servers(servers)

        ctk.CTkButton(inj_frame, text="儲存", width=50, font=ctk.CTkFont(size=11), command=save_inject).grid(
            row=0, column=2, padx=(0, 8), pady=6
        )

    def _remove(self, name: str):

        servers = _load_servers()

        servers.pop(name, None)

        _save_servers(servers)

        self._refresh()

    # ── 新增對話框 ────────────────────────────────

    # ─────────────────────────────
    # 新增 / 編輯 MCP Server 共用 Dialog
    # ─────────────────────────────

    def _open_server_dialog(self, mode="add", server_name=None):

        servers = _load_servers()

        editing = mode == "edit" and server_name in servers

        old_name = server_name if editing else None

        spec = servers[server_name] if editing else {}

        dlg = ctk.CTkToplevel(self)

        dlg.title("修改 MCP Server" if editing else "新增 MCP Server")

        dlg.geometry("460x380")

        dlg.resizable(False, False)

        dlg.grab_set()

        dlg.grid_columnconfigure(1, weight=1)

        fields = {}

        def row(r, label, widget_cls, **kw):

            ctk.CTkLabel(dlg, text=label).grid(row=r, column=0, padx=16, pady=7, sticky="w")

            w = widget_cls(dlg, **kw)

            w.grid(row=r, column=1, padx=16, pady=7, sticky="ew")

            return w

        # -------------------------
        # 名稱
        # -------------------------

        fields["name"] = row(0, "名稱", ctk.CTkEntry)

        if editing:
            fields["name"].insert(0, old_name)

        # -------------------------
        # transport
        # -------------------------

        transport_var = ctk.StringVar(value=spec.get("transport", "stdio"))

        fields["transport"] = row(
            1,
            "傳輸方式",
            ctk.CTkOptionMenu,
            values=["stdio", "sse"],
            variable=transport_var,
            command=lambda v: toggle_transport(v),
        )

        # -------------------------
        # command
        # -------------------------

        fields["command"] = row(2, "指令", ctk.CTkEntry, placeholder_text="例：python")

        fields["command"].insert(0, spec.get("command", ""))

        # -------------------------
        # args
        # -------------------------

        fields["args"] = row(3, "參數", ctk.CTkEntry, placeholder_text="例：server.py")

        fields["args"].insert(0, " ".join(spec.get("args", [])))

        # -------------------------
        # SSE URL
        # -------------------------

        fields["url"] = row(4, "SSE URL", ctk.CTkEntry, placeholder_text="http://localhost:3001/sse")

        fields["url"].insert(0, spec.get("url", ""))

        # -------------------------
        # inject
        # -------------------------

        fields["inject"] = row(5, "inject", ctk.CTkEntry, placeholder_text="api_key=xxx, token=yyy")

        fields["inject"].insert(0, _inject_to_str(spec.get("inject", {})))

        ctk.CTkLabel(
            dlg, text="inject 參數會自動帶入工具呼叫，模型不會看到", text_color="gray", font=ctk.CTkFont(size=10)
        ).grid(row=6, column=1, padx=16, sticky="w")

        # -------------------------
        # stdio / sse 切換
        # -------------------------

        def toggle_transport(v):

            if v == "sse":
                fields["command"].grid_remove()
                fields["args"].grid_remove()

                fields["url"].grid()

            else:
                fields["url"].grid_remove()

                fields["command"].grid()
                fields["args"].grid()

        toggle_transport(transport_var.get())

        # -------------------------
        # 儲存
        # -------------------------

        def save():

            new_name = fields["name"].get().strip()

            if not new_name:
                return

            transport = transport_var.get()

            if transport == "stdio":
                new_spec = {
                    "transport": "stdio",
                    "command": fields["command"].get().strip(),
                    "args": fields["args"].get().strip().split(),
                }

            else:
                new_spec = {"transport": "sse", "url": fields["url"].get().strip()}

            inject = _str_to_inject(fields["inject"].get())

            if inject:
                new_spec["inject"] = inject

            # 編輯時移除舊名稱

            if editing:
                servers.pop(old_name, None)

            servers[new_name] = new_spec

            _save_servers(servers)

            dlg.destroy()

            self._refresh()

        ctk.CTkButton(dlg, text="儲存修改" if editing else "新增", command=save).grid(
            row=7, column=0, columnspan=2, padx=16, pady=16, sticky="ew"
        )

    def on_show(self):
        self._refresh()
