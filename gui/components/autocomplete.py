import logging

import customtkinter as ctk

log = logging.getLogger("pocket-agent")


class AutocompleteMenu(ctk.CTkToplevel):
    def __init__(self, parent, textbox, on_select):
        super().__init__(parent)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.textbox = textbox
        self.on_select = on_select

        self.frame = ctk.CTkFrame(
            self,
            fg_color=("gray95", "gray15"),
            corner_radius=4,
            border_width=1,
            border_color=("gray80", "gray30"),
        )
        self.frame.pack(fill="both", expand=True, padx=0, pady=0)

        self.labels = []
        self.items = []
        self.selected_index = 0

        self.withdraw()

    def update_items(self, items, filter_text):
        self.items = items
        for lbl in self.labels:
            lbl.destroy()
        self.labels.clear()

        if not items:
            self.withdraw()
            return

        for i, item in enumerate(items):
            if isinstance(item, dict) and "description" in item:
                # Command format or Path format
                display_name = item.get("display", item.get("name", ""))
                desc = item.get("description", "")
                text = f"{display_name}"
                if desc:
                    text += f" - {desc}"
            else:
                text = str(item)

            lbl = ctk.CTkLabel(self.frame, text=text, anchor="w", padx=10, pady=2, font=ctk.CTkFont(size=12))
            lbl.pack(fill="x", expand=True)
            lbl.bind("<Button-1>", lambda e, idx=i: self._click_select(idx))
            lbl.bind("<Enter>", lambda e, idx=i: self.select_item(idx))
            self.labels.append(lbl)

        self.select_item(0)
        self.deiconify()

    def select_item(self, index):
        if not self.labels:
            return
        if 0 <= index < len(self.labels):
            self.selected_index = index
            for i, lbl in enumerate(self.labels):
                if i == index:
                    lbl.configure(fg_color=("gray85", "gray30"))
                else:
                    lbl.configure(fg_color="transparent")

    def move_selection(self, delta):
        if not self.labels:
            return
        new_index = (self.selected_index + delta) % len(self.labels)
        self.select_item(new_index)

    def get_selected(self):
        if 0 <= self.selected_index < len(self.items):
            return self.items[self.selected_index]
        return None

    def _click_select(self, index):
        self.select_item(index)
        cmd = self.get_selected()
        if cmd:
            self.on_select(cmd)

    def hide(self):
        self.withdraw()

    def show_at_cursor(self):
        try:
            bbox = self.textbox._textbox.bbox("insert")
            if bbox:
                x, y, _, h = bbox
                root_x = self.textbox._textbox.winfo_rootx() + x
                root_y = self.textbox._textbox.winfo_rooty() + y + h
                self.geometry(f"+{root_x}+{root_y + 2}")
                self.deiconify()
                self.lift()
        except Exception as e:
            log.warning(f"Autocomplete positioning error: {e}")
