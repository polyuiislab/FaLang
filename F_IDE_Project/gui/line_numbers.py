import tkinter as tk

class LineNumbers(tk.Canvas):
    """Draw line numbers for the visible portion of the editor."""
    def __init__(self, master, text_widget, **kwargs):
        super().__init__(master, **kwargs)
        self.text_widget = text_widget
        self.text_widget.bind("<<Change>>", self.redraw)
        self.text_widget.bind("<Configure>", self.redraw)
        self.text_widget.bind("<KeyRelease>", self.redraw)
        self.text_widget.bind("<MouseWheel>", self.redraw)
        self.text_widget.bind("<Button-4>", self.redraw)  # Linux wheel up
        self.text_widget.bind("<Button-5>", self.redraw)  # Linux wheel down

        self.configure(bg="#313335", highlightthickness=0)

    def redraw(self, *args):
        self.delete("all")

        i = self.text_widget.index("@0,0")

        while True:
            dline = self.text_widget.dlineinfo(i)

            # dlineinfo returns None below the visible viewport.
            if dline is None:
                break

            y = dline[1]
            linenum = str(i).split(".", 1)[0]

            self.create_text(
                self.winfo_width() - 5,
                y, 
                anchor="ne",
                text=linenum, 
                fill="#606366",
                font=self.text_widget.cget("font")
            )

            i = self.text_widget.index(f"{i}+1line")
