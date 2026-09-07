import logging

log = logging.getLogger("pocket-agent")


class ScrollHelperMixin:
    """Mixin for handling mouse wheel scrolling for outer container and inner textboxes."""

    @staticmethod
    def _wheel_direction(event) -> int:
        delta = getattr(event, "delta", 0)
        if delta:
            return -1 if delta > 0 else 1

        button_number = getattr(event, "num", None)
        if button_number == 4:
            return -1
        if button_number == 5:
            return 1
        return 0

    def _scroll_outer_messages(self, direction: int):
        try:
            canvas = self.msg_frame._parent_canvas
            canvas.yview_scroll(direction * 40, "units")
        except Exception:
            log.exception("無法捲動外層對話訊息區")

    def _bind_outer_scroll(self, widget):
        def on_mousewheel(event):
            direction = self._wheel_direction(event)
            if direction:
                self._scroll_outer_messages(direction)
            return "break"

        targets = [widget]
        for attribute in ("_canvas", "_text_label", "_textbox"):
            internal_widget = getattr(widget, attribute, None)
            if internal_widget is not None:
                targets.append(internal_widget)

        for target in targets:
            try:
                target.bind("<MouseWheel>", on_mousewheel, add="+")
                target.bind("<Button-4>", on_mousewheel, add="+")
                target.bind("<Button-5>", on_mousewheel, add="+")
            except Exception:
                pass

    def _bind_bubble_scroll(self, textbox):
        def on_mousewheel(event):
            direction = self._wheel_direction(event)
            if not direction:
                return "break"

            try:
                first, last = textbox.yview()
                can_scroll_up = direction < 0 and first > 0.0001
                can_scroll_down = direction > 0 and last < 0.9999

                if can_scroll_up or can_scroll_down:
                    textbox.yview_scroll(direction * 3, "units")
                else:
                    self._scroll_outer_messages(direction)
            except Exception:
                log.exception("無法捲動訊息泡泡")
                self._scroll_outer_messages(direction)

            return "break"

        targets = [textbox]
        internal_textbox = getattr(textbox, "_textbox", None)
        if internal_textbox is not None:
            targets.append(internal_textbox)

        for target in targets:
            try:
                target.bind("<MouseWheel>", on_mousewheel, add="+")
                target.bind("<Button-4>", on_mousewheel, add="+")
                target.bind("<Button-5>", on_mousewheel, add="+")
            except Exception:
                pass
