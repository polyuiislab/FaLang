import tkinter as tk
from tkinter import font
from core.f_lang_syntax import FLangSyntax

class EditorWidget(tk.Text):
    """FaLang text editor with syntax highlighting and four-space indentation."""
    def __init__(self, master=None, **kwargs):
        self.syntax = FLangSyntax()

        cfg = {
            "background": self.syntax.get_background_color(),
            "foreground": self.syntax.get_foreground_color(),
            "insertbackground": self.syntax.get_insert_color(),
            "selectbackground": self.syntax.get_selection_color(),
            "undo": True,
            "wrap": "none",
            "font": font.Font(family="Consolas", size=11)
        }
        cfg.update(kwargs)

        super().__init__(master, **cfg)

        self.syntax.apply_tags(self)

        self.bind("<KeyRelease>", self.on_key_release)
        self.bind("<Control-a>", self.select_all)
        self.bind("<Tab>", self.insert_tab)

    def on_key_release(self, event=None):
        if event.keysym in ("Up", "Down", "Left", "Right", "Shift_L", "Control_L", "Alt_L"):
            return
        self.syntax.highlight(self)

    def select_all(self, event=None):
        self.tag_add("sel", "1.0", "end")
        return "break"

    def insert_tab(self, event=None):
        self.insert("insert", "    ")
        return "break"

    def load_content(self, text):
        self.delete("1.0", "end")
        self.insert("1.0", text)
        self.syntax.highlight(self)