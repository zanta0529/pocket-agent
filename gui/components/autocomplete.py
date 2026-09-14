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
        self.is_open = False
        self.page = 0
        self.all_items = []

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

    def update_items(self, items, filter_text=None, reset_page=True):
        if reset_page:
            self.page = 0
        self.all_items = items
        self.items = []

        for lbl in self.labels:
            lbl.destroy()
        self.labels.clear()

        if not self.all_items:
            self.hide()
            return

        PAGE_SIZE = 10
        total = len(self.all_items)

        start_idx = self.page * PAGE_SIZE
        if start_idx >= total:
            self.page = max(0, (total - 1) // PAGE_SIZE)
            start_idx = self.page * PAGE_SIZE

        end_idx = start_idx + PAGE_SIZE
        page_items = self.all_items[start_idx:end_idx]

        if self.page > 0:
            self.items.append({"is_pagination": True, "direction": -1, "display": f"↑ {start_idx} before"})

        self.items.extend(page_items)

        if end_idx < total:
            self.items.append({"is_pagination": True, "direction": 1, "display": f"↓ {total - end_idx} more"})

        for i, item in enumerate(self.items):
            if isinstance(item, dict) and "is_pagination" in item:
                text = f"  {item['display']}"
                font = ctk.CTkFont(size=12, slant="italic")
                text_color = "gray60"
            elif isinstance(item, dict) and "description" in item:
                # Command format or Path format
                display_name = item.get("display", item.get("name", ""))
                desc = item.get("description", "")
                text = f"{display_name}"
                if desc:
                    text += f" - {desc}"
                font = ctk.CTkFont(size=12)
                text_color = None
            else:
                text = str(item)
                font = ctk.CTkFont(size=12)
                text_color = None

            # Apply font explicitly if provided. CTkLabel handles text_color
            lbl_kwargs = {"text": text, "anchor": "w", "padx": 10, "pady": 2, "font": font}
            if text_color:
                lbl_kwargs["text_color"] = text_color

            lbl = ctk.CTkLabel(self.frame, **lbl_kwargs)
            lbl.pack(fill="x", expand=True)
            lbl.bind("<Button-1>", lambda e, idx=i: self._click_select(idx))
            lbl.bind("<Enter>", lambda e, idx=i: self.select_item(idx))
            self.labels.append(lbl)

        self.select_item(0)
        self.is_open = True
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
            item = self.items[self.selected_index]
            if isinstance(item, dict) and "is_pagination" in item:
                self.page += item["direction"]
                self.update_items(self.all_items, reset_page=False)
                return None
            return item
        return None

    def _click_select(self, index):
        self.select_item(index)
        cmd = self.get_selected()
        if cmd:
            self.on_select(cmd)

    def hide(self):
        self.is_open = False
        self.withdraw()

    def show_at_cursor(self):
        try:
            bbox = self.textbox._textbox.bbox("insert")
            if bbox:
                x, y, _, h = bbox
                root_x = self.textbox._textbox.winfo_rootx() + x
                root_y = self.textbox._textbox.winfo_rooty() + y + h
                self.geometry(f"+{root_x}+{root_y + 2}")
                self.is_open = True
                self.deiconify()
                self.lift()
        except Exception as e:
            log.warning(f"Autocomplete positioning error: {e}")
