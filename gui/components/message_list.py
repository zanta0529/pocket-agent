import customtkinter as ctk


class MessageList(ctk.CTkScrollableFrame):
    """
    MessageList component extracted from ChatView for rendering chat bubbles and system messages.
    """

    def __init__(self, master, app_ref, **kwargs):
        super().__init__(master, **kwargs)
        self.app = app_ref

    def add_bubble(self, text: str, role: str):
        pass  # To be fully implemented by moving methods from ChatView

    def add_system(self, text: str):
        pass  # To be fully implemented by moving methods from ChatView
