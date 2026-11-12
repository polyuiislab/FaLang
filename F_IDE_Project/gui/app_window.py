import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import os
from gui.editor_widget import EditorWidget
from gui.line_numbers import LineNumbers
from core.compiler_proxy import CompilerProxy
from gui.terminal_panel import TerminalPanel

class F_IDE_Window(tk.Frame):
    def __init__(self, master=None):
        super().__init__(master)
        self.master = master
        self.compiler = CompilerProxy()
        self.current_file = None

        self.style = ttk.Style()
        self.style.theme_use('clam')

        self.create_widgets()
        self.setup_layout()
        self.bind_events()

    def create_widgets(self):
        self.toolbar = tk.Frame(self, bg="#333333", height=40)

        btn_cfg = {"bg": "#444444", "fg": "white", "relief": "flat", "padx": 10, "pady": 5}

        self.btn_open = tk.Button(self.toolbar, text="📂 Open", command=self.open_file, **btn_cfg)
        self.btn_save = tk.Button(self.toolbar, text="💾 Save", command=self.save_file, **btn_cfg)
        self.btn_compile = tk.Button(self.toolbar, text="⚙️ Compile & Build", bg="#4CAF50", fg="white", 
                                     relief="flat", padx=10, pady=5, command=self.run_compilation)

        self.paned_window = tk.PanedWindow(self, orient=tk.VERTICAL, sashwidth=4, bg="#202020")

        self.editor_frame = tk.Frame(self.paned_window)
        self.scrollbar = tk.Scrollbar(self.editor_frame)
        self.editor = EditorWidget(self.editor_frame, yscrollcommand=self.scrollbar.set)
        self.scrollbar.config(command=self.on_scroll)
        self.line_numbers = LineNumbers(self.editor_frame, self.editor, width=40)

        self.terminal = TerminalPanel(self.paned_window)

    def setup_layout(self):
        self.toolbar.pack(side="top", fill="x")
        self.btn_open.pack(side="left", padx=2, pady=2)
        self.btn_save.pack(side="left", padx=2, pady=2)
        self.btn_compile.pack(side="left", padx=20, pady=2)

        self.paned_window.pack(fill="both", expand=True)

        self.paned_window.add(self.editor_frame, stretch="always")
        self.scrollbar.pack(side="right", fill="y")
        self.line_numbers.pack(side="left", fill="y")
        self.editor.pack(side="right", fill="both", expand=True)

        self.paned_window.add(self.terminal, stretch="never")

    def bind_events(self):
        self.editor.bind("<MouseWheel>", self.on_scroll_event)
        self.editor.bind("<Button-4>", self.on_scroll_event)
        self.editor.bind("<Button-5>", self.on_scroll_event)

    def on_scroll(self, *args):
        self.editor.yview(*args)
        self.line_numbers.redraw()

    def on_scroll_event(self, event):
        self.line_numbers.redraw()
        # Text handles the scroll event; update only the line-number view.

    def log_to_console(self, message):
        level = "ERROR" if "Error" in message else "SUCCESS" if "Success" in message else "INFO"
        self.terminal.log(message, level)

    def open_file(self):
        path = filedialog.askopenfilename(
            filetypes=[("FaLang Files", ("*.falang", "*.fl")), ("All Files", "*.*")])
        if path:
            self.current_file = path
            try:
                with open(path, "r", encoding='utf-8') as f:
                    content = f.read()
                self.editor.load_content(content)
                self.log_to_console(f"Loaded: {path}")
                self.master.title(f"F-IDE - {os.path.basename(path)}")
            except Exception as e:
                messagebox.showerror("Error", f"Failed to open file: {e}")

    def save_file(self):
        if not self.current_file:
            path = filedialog.asksaveasfilename(defaultextension=".falang",
                                              filetypes=[("FaLang Files", ("*.falang", "*.fl")), ("All Files", "*.*")])
            if not path: return
            self.current_file = path

        try:
            with open(self.current_file, "w", encoding='utf-8') as f:
                f.write(self.editor.get("1.0", "end-1c"))
            self.log_to_console(f"Saved: {self.current_file}")
            self.master.title(f"F-IDE - {os.path.basename(self.current_file)}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save file: {e}")

    def run_compilation(self):
        source = self.editor.get("1.0", "end-1c")
        self.terminal.clear()

        self.log_to_console("--- Starting F-Compiler Backend ---")

        result = self.compiler.compile_source(source)

        for msg in result["messages"]:
            self.log_to_console(msg)

        if result["success"]:
            self.log_to_console("Generating memory initialization vectors...")

            default_name = "output.coe"
            if self.current_file:
                default_name = os.path.splitext(os.path.basename(self.current_file))[0] + ".coe"

            save_path = filedialog.asksaveasfilename(initialfile=default_name,
                                                   defaultextension=".coe",
                                                   filetypes=[("Xilinx COE File", "*.coe")])
            if save_path:
                success, msg = self.compiler.generate_coe_file(save_path, result["coe_data"])
                self.log_to_console(msg)
                if success:
                    messagebox.showinfo("Success", "Build Complete! COE file generated.")
