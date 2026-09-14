import sys

from .base import FrontendStrategy


class GuiFrontend(FrontendStrategy):
    """圖形介面 (GUI) 執行策略"""

    def run(self, system_prompt: str = "You are a helpful assistant.", resume: bool = False):
        try:
            from gui.app import App
        except ModuleNotFoundError as e:
            if "tkinter" in str(e):
                print("\n" + "=" * 60)
                print("[警告] 系統缺少 GUI 必備的 'tkinter' 函式庫。")
                print("這通常是因為目前的 Python 為 Embeddable（可攜版），未包含 Tcl/Tk 元件。")
                print("=" * 60)
                try:
                    choice = input("是否直接切換為終端機模式 (CLI) 啟動？[Y/n]: ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    choice = "n"
                if choice in ("", "y", "yes"):
                    from .cli import CliFrontend

                    CliFrontend().run(system_prompt, resume=resume)
                    return
                sys.exit(1)
            raise
        app = App()
        app.mainloop()
