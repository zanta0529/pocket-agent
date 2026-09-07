import customtkinter as ctk


class MessageList(ctk.CTkScrollableFrame):
    """
    MessageList component extracted from ChatView for rendering chat bubbles and system messages.
    """

    def __init__(self, master, app_ref, **kwargs):
        super().__init__(master, **kwargs)
        self.app = app_ref

    def add_bubble(self, text: str, role: str):
        pass

    def add_stream_bubble(self):
        pass

    def add_system(self, text: str):
        pass

    def add_image(self, path):
        pass

    def add_file_link(self, path):
        pass

    def add_reasoning(self, text: str, before=None):
        pass

    def add_collapsible(self, title: str, expanded: bool = False, max_lines: int = 15):
        pass

    def add_spinner(self):
        pass
