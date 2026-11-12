import tkinter as tk
from tkinter import messagebox
import sys
import os
from gui.app_window import F_IDE_Window

def resource_path(relative_path):
    """Resolve assets from the working directory or the PyInstaller bundle."""
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")

    return os.path.join(base_path, relative_path)

def main():
    try:
        root = tk.Tk()
        root.title("F-IDE: Factory Computer Environment")
        root.geometry("1200x800")

        icon_path = resource_path(os.path.join("assets", "app_icon.ico"))
        if os.path.exists(icon_path):
            try:
                root.iconbitmap(icon_path)
            except:
                pass

        app = F_IDE_Window(root)
        app.pack(fill="both", expand=True)

        root.mainloop()

    except Exception as e:
        # Packaged builds have no console, so report startup errors in a dialog.
        tk.messagebox.showerror("Critical Error", f"Application failed to start:\n{str(e)}")

if __name__ == "__main__":
    main()