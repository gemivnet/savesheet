from __future__ import annotations

import subprocess
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from typing import TYPE_CHECKING, Callable

from ..config import Job
from ..utils import resolve_path, sanitize_job_id

if TYPE_CHECKING:
    from ..app import App


DEVICE_TYPES = {
    "adbsync": "Android (ADB)",
    "mtp": "iPhone / iPad (MTP)",
    "ffs": "Camera / SD Card (FreeFileSync)",
}

DEFAULT_SOURCES = {
    "adbsync": "/storage/emulated/0/",
    "mtp": "Internal Storage\\DCIM",
    "ffs": "",
}

DEFAULT_EXCLUDES = {
    "adbsync": "*cache*\n*.db*",
    "mtp": "",
    "ffs": "",
}


class DeviceWizard(tk.Toplevel):
    def __init__(self, parent: tk.Tk, app: App, on_save: Callable[[], None], edit_job: Job | None = None):
        super().__init__(parent)
        self.app = app
        self.on_save = on_save
        self.edit_job = edit_job

        self.title("Edit Device" if edit_job else "Add Device")
        self.geometry("520x520")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self._build_ui()

        if edit_job:
            self._populate_from_job(edit_job)

    def _build_ui(self) -> None:
        pad = {"padx": 10, "pady": 4}

        # --- Type ---
        ttk.Label(self, text="Device Type:").pack(anchor=tk.W, **pad)
        self._type_var = tk.StringVar(value="adbsync")
        type_frame = ttk.Frame(self)
        type_frame.pack(fill=tk.X, **pad)
        for key, label in DEVICE_TYPES.items():
            ttk.Radiobutton(type_frame, text=label, variable=self._type_var, value=key,
                            command=self._on_type_change).pack(anchor=tk.W)

        # --- Test button ---
        test_frame = ttk.Frame(self)
        test_frame.pack(fill=tk.X, **pad)
        ttk.Button(test_frame, text="Test Connection", command=self._on_test).pack(side=tk.LEFT)
        self._test_status = ttk.Label(test_frame, text="")
        self._test_status.pack(side=tk.LEFT, padx=8)

        # --- Test output ---
        self._test_output = tk.Text(self, height=4, wrap=tk.WORD, font=("Consolas", 9),
                                    bg="#1e1e1e", fg="#cccccc", state=tk.DISABLED)
        self._test_output.pack(fill=tk.X, **pad)

        # --- Name ---
        ttk.Label(self, text="Device Name:").pack(anchor=tk.W, **pad)
        self._name_var = tk.StringVar()
        ttk.Entry(self, textvariable=self._name_var, width=50).pack(fill=tk.X, **pad)

        # --- MTP device name (only for mtp) ---
        self._mtp_frame = ttk.Frame(self)
        ttk.Label(self._mtp_frame, text="MTP Device Name (from test above):").pack(anchor=tk.W)
        self._mtp_device_var = tk.StringVar(value="Apple iPhone")
        ttk.Entry(self._mtp_frame, textvariable=self._mtp_device_var, width=50).pack(fill=tk.X)

        # --- FFS batch file (only for ffs) ---
        self._ffs_frame = ttk.Frame(self)
        ttk.Label(self._ffs_frame, text="FreeFileSync Batch File:").pack(anchor=tk.W)
        ffs_row = ttk.Frame(self._ffs_frame)
        ffs_row.pack(fill=tk.X)
        self._ffs_var = tk.StringVar()
        ttk.Entry(ffs_row, textvariable=self._ffs_var, width=40).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(ffs_row, text="Browse...", command=self._browse_ffs).pack(side=tk.RIGHT, padx=(4, 0))

        # --- Source ---
        self._source_frame = ttk.Frame(self)
        ttk.Label(self._source_frame, text="Source Path:").pack(anchor=tk.W)
        self._source_var = tk.StringVar(value=DEFAULT_SOURCES["adbsync"])
        ttk.Entry(self._source_frame, textvariable=self._source_var, width=50).pack(fill=tk.X)

        # --- Destination ---
        ttk.Label(self, text="Backup Destination:").pack(anchor=tk.W, **pad)
        dest_frame = ttk.Frame(self)
        dest_frame.pack(fill=tk.X, **pad)
        self._dest_var = tk.StringVar()
        ttk.Entry(dest_frame, textvariable=self._dest_var, width=40).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(dest_frame, text="Browse...", command=self._browse_dest).pack(side=tk.RIGHT, padx=(4, 0))

        # --- Excludes ---
        self._exclude_frame = ttk.Frame(self)
        ttk.Label(self._exclude_frame, text="Exclude Patterns (one per line):").pack(anchor=tk.W)
        self._exclude_text = tk.Text(self._exclude_frame, height=3, font=("Consolas", 9), width=50)
        self._exclude_text.pack(fill=tk.X)

        # --- Buttons ---
        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill=tk.X, padx=10, pady=10)
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(btn_frame, text="Save", command=self._on_save).pack(side=tk.RIGHT, padx=(0, 8))

        self._on_type_change()

    def _on_type_change(self) -> None:
        dtype = self._type_var.get()
        # Show/hide type-specific fields
        self._mtp_frame.pack_forget()
        self._ffs_frame.pack_forget()
        self._source_frame.pack_forget()
        self._exclude_frame.pack_forget()

        if dtype == "mtp":
            self._mtp_frame.pack(fill=tk.X, padx=10, pady=4)
            self._source_frame.pack(fill=tk.X, padx=10, pady=4)
            self._exclude_frame.pack(fill=tk.X, padx=10, pady=4)
            self._source_var.set(DEFAULT_SOURCES["mtp"])
        elif dtype == "ffs":
            self._ffs_frame.pack(fill=tk.X, padx=10, pady=4)
        elif dtype == "adbsync":
            self._source_frame.pack(fill=tk.X, padx=10, pady=4)
            self._exclude_frame.pack(fill=tk.X, padx=10, pady=4)
            self._source_var.set(DEFAULT_SOURCES["adbsync"])
            self._exclude_text.delete("1.0", tk.END)
            self._exclude_text.insert("1.0", DEFAULT_EXCLUDES["adbsync"])

    def _on_test(self) -> None:
        dtype = self._type_var.get()
        self._test_status.configure(text="Testing...")
        self._test_output.configure(state=tk.NORMAL)
        self._test_output.delete("1.0", tk.END)
        self._test_output.configure(state=tk.DISABLED)

        def run_test():
            try:
                if dtype == "adbsync":
                    adb = str(resolve_path(self.app.config_mgr.tools.adb, self.app.base_dir))
                    result = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=10)
                    output = result.stdout + result.stderr
                elif dtype == "mtp":
                    mdc = str(resolve_path(self.app.config_mgr.tools.media_device_copier, self.app.base_dir))
                    result = subprocess.run([mdc, "list-devices"], capture_output=True, text=True, timeout=10)
                    output = result.stdout + result.stderr
                elif dtype == "ffs":
                    output = "FreeFileSync uses batch files. Browse for your .ffs_batch file below."
                else:
                    output = "Unknown type"
            except FileNotFoundError as e:
                output = f"Tool not found: {e}"
            except subprocess.TimeoutExpired:
                output = "Test timed out after 10 seconds."
            except Exception as e:
                output = f"Error: {e}"

            self.after(0, self._show_test_result, output)

        threading.Thread(target=run_test, daemon=True).start()

    def _show_test_result(self, output: str) -> None:
        self._test_status.configure(text="Done")
        self._test_output.configure(state=tk.NORMAL)
        self._test_output.delete("1.0", tk.END)
        self._test_output.insert("1.0", output.strip())
        self._test_output.configure(state=tk.DISABLED)

    def _browse_dest(self) -> None:
        path = filedialog.askdirectory(title="Select Backup Destination")
        if path:
            self._dest_var.set(path)

    def _browse_ffs(self) -> None:
        path = filedialog.askopenfilename(
            title="Select FreeFileSync Batch File",
            filetypes=[("FFS Batch", "*.ffs_batch"), ("All files", "*.*")]
        )
        if path:
            self._ffs_var.set(path)

    def _populate_from_job(self, job: Job) -> None:
        self._type_var.set(job.type)
        self._on_type_change()
        self._name_var.set(job.name)
        self._source_var.set(job.source)
        self._dest_var.set(job.destination)
        if job.type == "mtp":
            self._mtp_device_var.set(job.device_name)
        if job.type == "ffs":
            self._ffs_var.set(job.ffs_batch)
        if job.exclude:
            self._exclude_text.delete("1.0", tk.END)
            self._exclude_text.insert("1.0", "\n".join(job.exclude))

    def _on_save(self) -> None:
        name = self._name_var.get().strip()
        if not name:
            messagebox.showerror("Error", "Device name is required.", parent=self)
            return

        dtype = self._type_var.get()
        job_id = self.edit_job.id if self.edit_job else sanitize_job_id(name)

        # Check for duplicate ID if new
        if not self.edit_job and job_id in self.app.config_mgr.jobs:
            messagebox.showerror("Error", f"A job with ID '{job_id}' already exists.", parent=self)
            return

        dest = self._dest_var.get().strip()
        if dtype != "ffs" and not dest:
            messagebox.showerror("Error", "Backup destination is required.", parent=self)
            return

        excludes = []
        if dtype in ("adbsync", "mtp"):
            raw = self._exclude_text.get("1.0", tk.END).strip()
            if raw:
                excludes = [line.strip() for line in raw.splitlines() if line.strip()]

        job = Job(
            id=job_id,
            name=name,
            type=dtype,
            group="device",
            source=self._source_var.get().strip(),
            destination=dest,
            exclude=excludes,
            device_name=self._mtp_device_var.get().strip() if dtype == "mtp" else "",
            recursive=True if dtype == "mtp" else False,
            ffs_batch=self._ffs_var.get().strip() if dtype == "ffs" else "",
        )

        if self.edit_job:
            self.app.config_mgr.update_job(job)
        else:
            self.app.config_mgr.add_job(job)

        self.on_save()
        self.destroy()
