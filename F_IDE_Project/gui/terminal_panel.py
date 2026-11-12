import tkinter as tk
from tkinter import font

class TerminalPanel(tk.Frame):
    """Display compiler messages in a read-only, color-coded log."""
    def __init__(self, master=None, **kwargs):
        super().__init__(master, **kwargs)

        self.configure(bg="#1E1E1E")

        self.header_frame = tk.Frame(self, bg="#252526", height=25)
        self.header_frame.pack(side="top", fill="x")

        self.header_label = tk.Label(
            self.header_frame, 
            text="TERMINAL / OUTPUT", 
            bg="#252526", 
            fg="#CCCCCC", 
            font=("Segoe UI", 8, "bold"),
            anchor="w", 
            padx=10
        )
        self.header_label.pack(side="left", fill="y")

        self.text_container = tk.Frame(self, bg="#1E1E1E")
        self.text_container.pack(fill="both", expand=True)

        self.scrollbar = tk.Scrollbar(self.text_container)
        self.scrollbar.pack(side="right", fill="y")

        self.text_widget = tk.Text(
            self.text_container,
            height=8,
            bg="#1E1E1E",
            fg="#CCCCCC",
            font=("Consolas", 10),
            state="disabled",
            relief="flat",
            selectbackground="#264f78",
            yscrollcommand=self.scrollbar.set
        )
        self.text_widget.pack(side="left", fill="both", expand=True)

        self.scrollbar.config(command=self.text_widget.yview)

        self.text_widget.tag_config("INFO", foreground="#CCCCCC")
        self.text_widget.tag_config("SUCCESS", foreground="#89D185")
        self.text_widget.tag_config("WARNING", foreground="#DCDCAA")
        self.text_widget.tag_config("ERROR", foreground="#F48771")

    def log(self, message, level="INFO"):
        self.text_widget.config(state="normal")

        tag = level.upper()
        if tag not in self.text_widget.tag_names():
            tag = "INFO"

        if not message.endswith("\n"):
            message += "\n"

        self.text_widget.insert("end", message, tag)

        self.text_widget.see("end")

        self.text_widget.config(state="disabled")

    def clear(self):
        self.text_widget.config(state="normal")
        self.text_widget.delete("1.0", "end")
        self.text_widget.config(state="disabled")