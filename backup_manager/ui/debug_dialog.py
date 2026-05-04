from __future__ import annotations

import os
import subprocess
import threading
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

from ..utils import resolve_path

if TYPE_CHECKING:
    from ..app import App


class DebugDialog(tk.Toplevel):
    def __init__(self, parent: tk.Tk, app: App):
        super().__init__(parent)
        self.app = app
        self.title("Debug Tools")
        self.geometry("550x450")
        self.transient(parent)
        self.grab_set()

        self._build_ui()

    def _build_ui(self) -> None:
        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill=tk.X, padx=10, pady=10)

        buttons = [
            ("Test ADB", self._test_adb),
            ("List MTP Devices", self._test_mtp),
            ("Rclone Remotes", self._test_rclone),
            ("Ping NAS", self._test_nas),
            ("Open Rclone Config", self._open_rclone_config),
        ]

        for text, cmd in buttons:
            ttk.Button(btn_frame, text=text, command=cmd).pack(side=tk.LEFT, padx=(0, 6))

        ttk.Label(self, text="Output:").pack(anchor=tk.W, padx=10)
        self._output = tk.Text(self, wrap=tk.WORD, font=("Consolas", 9),
                               bg="#1e1e1e", fg="#cccccc", state=tk.DISABLED)
        scrollbar = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self._output.yview)
        self._output.configure(yscrollcommand=scrollbar.set)
        self._output.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0), pady=(4, 10))
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 10), pady=(4, 10))

    def _show(self, text: str) -> None:
        self._output.configure(state=tk.NORMAL)
        self._output.delete("1.0", tk.END)
        self._output.insert("1.0", text.strip())
        self._output.configure(state=tk.DISABLED)

    def _run_async(self, cmd: list[str], label: str) -> None:
        self._show(f"Running {label}...")

        def run():
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=15,
                    cwd=str(self.app.base_dir), encoding="utf-8", errors="replace"
                )
                output = (result.stdout + "\n" + result.stderr).strip()
                if not output:
                    output = "(no output)"
            except FileNotFoundError:
                output = f"Tool not found: {cmd[0]}"
            except subprocess.TimeoutExpired:
                output = "Timed out after 15 seconds."
            except Exception as e:
                output = f"Error: {e}"
            self.after(0, self._show, f"[{label}]\n\n{output}")

        threading.Thread(target=run, daemon=True).start()

    def _test_adb(self) -> None:
        adb = str(resolve_path(self.app.config_mgr.tools.adb, self.app.base_dir))
        self._run_async([adb, "devices"], "ADB Devices")

    def _test_mtp(self) -> None:
        mdc = str(resolve_path(self.app.config_mgr.tools.media_device_copier, self.app.base_dir))
        self._run_async([mdc, "list-devices"], "MTP Devices")

    def _test_rclone(self) -> None:
        rclone = str(resolve_path(self.app.config_mgr.tools.rclone, self.app.base_dir))
        self._run_async([rclone, "listremotes"], "Rclone Remotes")

    def _test_nas(self) -> None:
        self._run_async(["ping", "-n", "1", "server"], "Ping NAS")

    def _open_rclone_config(self) -> None:
        rclone = str(resolve_path(self.app.config_mgr.tools.rclone, self.app.base_dir))
        try:
            result = subprocess.run([rclone, "config", "file"], capture_output=True, text=True, timeout=5)
            # Output is like "Configuration file is stored at:\nC:\path\to\rclone.conf"
            lines = result.stdout.strip().splitlines()
            config_file = lines[-1].strip() if lines else ""
            if config_file and os.path.exists(config_file):
                os.startfile(config_file)
                self._show(f"Opened: {config_file}")
            else:
                self._show(f"Could not find config file.\nRclone output: {result.stdout}")
        except Exception as e:
            self._show(f"Error: {e}")
