from __future__ import annotations

import sys
import tkinter as tk
from tkinter import messagebox
from pathlib import Path


def main() -> None:
    # Look for config in the current directory, or parent of backup_manager package
    config_path = Path.cwd() / "backup_config.yaml"
    if not config_path.exists():
        pkg_dir = Path(__file__).resolve().parent.parent
        config_path = pkg_dir / "backup_config.yaml"

    if not config_path.exists():
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Backup Manager",
            f"No backup_config.yaml found.\n\nCopy backup_config.example.yaml to backup_config.yaml and edit it.\n\nLooked in:\n{Path.cwd()}"
        )
        sys.exit(1)

    try:
        from .app import App
        app = App(config_path)
        app.run()
    except Exception as e:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Backup Manager - Error", f"Failed to start:\n\n{e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
