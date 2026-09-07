import logging
import time

log = logging.getLogger("pocket-agent")


class IMEHelperMixin:
    """Mixin for handling Windows IME issues with Tkinter Text."""

    def _ime_mark_cancel(self, event=None):
        self._ime_state["skip_until"] = time.time() + 0.4

    def _ime_poll(self):
        try:
            self._ime_tick()
        except Exception:
            pass
        finally:
            self.after(50, self._ime_poll)

    def _ime_tick(self):
        from gui.ime import get_composition

        state = self._ime_state
        focused = self.focus_get()

        if not focused or not str(focused).startswith(str(self.input)):
            state.update(comp="", pending=None)
            return

        composition = get_composition()
        text = self.input.get("1.0", "end-1c")

        if composition:
            state.update(comp=composition, base=text, pending=None)
            return

        if state["comp"]:
            log.debug("IME[compose-end] comp=%r base=%r text-now=%r", state["comp"], state["base"], text)
            state["pending"] = {"comp": state["comp"], "base": state["base"]}
            state["comp"] = ""
            return

        pending = state["pending"]
        if not pending:
            return

        state["pending"] = None
        if time.time() < state["skip_until"]:
            log.debug("IME rescue skipped (user cancelled) comp=%r", pending["comp"])
            return

        base = pending["base"]

        if text == base:
            log.debug("IME[rescue-insert] comp=%r base=%r cursor=%s", pending["comp"], base, self.input.index("insert"))
            self.input.insert("insert", pending["comp"])

        elif len(text) < len(base) and base.startswith(text):
            missing = base[len(text) :]
            log.debug("IME[rescue-truncated] missing=%r comp=%r base=%r text=%r", missing, pending["comp"], base, text)
            self.input.insert(f"1.0+{len(text)}c", missing + pending["comp"])

        elif (
            text.startswith(base)
            and pending["comp"].startswith(text[len(base) :])
            and len(text) - len(base) < len(pending["comp"])
        ):
            committed = text[len(base) :]
            missing = pending["comp"][len(committed) :]
            log.debug(
                "IME[rescue-partial] committed=%r missing=%r comp=%r base=%r", committed, missing, pending["comp"], base
            )
            self.input.insert(f"1.0+{len(text)}c", missing)

        else:
            index = self._diff_single_space(pending["base"], text)
            if index is not None:
                log.debug(
                    "IME[replace-space] index=%d removing=%r comp=%r base=%r text=%r",
                    index,
                    text[index],
                    pending["comp"],
                    pending["base"],
                    text,
                )
                self.input.delete(f"1.0+{index}c", f"1.0+{index + 1}c")
                self.input.insert("insert", pending["comp"])
            else:
                log.debug("IME[no-op] base=%r text=%r comp=%r", pending["base"], text, pending["comp"])

    @staticmethod
    def _diff_single_space(previous: str, current: str) -> int | None:
        if len(current) != len(previous) + 1:
            return None
        index = next((p for p in range(len(previous)) if previous[p] != current[p]), len(previous))
        if current[index] in (" ", "　") and previous == current[:index] + current[index + 1 :]:
            return index
        return None

    def _on_ctrl_ime_character(self, event):
        ime_punctuation = {"，", "。", "、", "；"}
        if event.char in ime_punctuation:
            event.widget.insert("insert", event.char)
            return "break"

    def _on_ctrl_space(self, event):
        from gui.ime import get_composition

        _comp_at_press = get_composition()
        if not _comp_at_press:
            snapshot = self.input.get("1.0", "end-1c")
            state = {"tries": 0}

            def _watch():
                now = self.input.get("1.0", "end-1c")
                index = self._diff_single_space(snapshot, now)

                if index is not None:
                    log.debug(
                        "IME[watch-delete] try=%d index=%d removing=%r snapshot=%r now=%r",
                        state["tries"],
                        index,
                        now[index],
                        snapshot,
                        now,
                    )
                    self.input.delete(f"1.0+{index}c", f"1.0+{index + 1}c")
                    return

                state["tries"] += 1
                if state["tries"] < 10 and now == snapshot:
                    self.after(20, _watch)

            self.after(10, _watch)

        return "break"
