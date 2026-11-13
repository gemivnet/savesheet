from __future__ import annotations

import subprocess
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from typing import TYPE_CHECKING, Callable

from ..config import Job
from ..utils import console_python, resolve_path, sanitize_job_id

if TYPE_CHECKING:
    from ..app import App


DEVICE_TYPES = {
    "adbsync": "Android (ADB)",
    "mtp": "iPhone / iPad photos (MTP)",
    "ios_backup": "iPhone / iPad full backup (encrypted)",
    "ffs": "Camera / SD Card (FreeFileSync)",
}

# Shown in the "same device as" picker for mtp jobs when nothing is chosen.
NO_TWIN = "(none - do not check)"

DEFAULT_SOURCES = {
    "adbsync": "/storage/emulated/0/",
    "mtp": "Internal Storage",
    "ios_backup": "",
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
        self.geometry("540x650")
        self.resizable(True, True)
        self.minsize(480, 500)
        self.transient(parent)
        self.grab_set()

        self._build_ui()

        if edit_job:
            self._populate_from_job(edit_job)

    def _build_ui(self) -> None:
        # Scrollable content
        canvas = tk.Canvas(self, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient=tk.VERTICAL, command=canvas.yview)
        self._content = ttk.Frame(canvas)
        self._content.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self._content, anchor="nw", tags="content")
        canvas.configure(yscrollcommand=scrollbar.set)

        # Make content frame fill canvas width
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure("content", width=e.width))

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Enable mousewheel scrolling
        def _on_mousewheel(e):
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        self.bind("<Destroy>", lambda e: canvas.unbind_all("<MouseWheel>") if e.widget is self else None)

        pad = {"padx": 10, "pady": 4}
        c = self._content

        # --- Type ---
        ttk.Label(c, text="Device Type:").pack(anchor=tk.W, **pad)
        self._type_var = tk.StringVar(value="adbsync")
        type_frame = ttk.Frame(c)
        type_frame.pack(fill=tk.X, **pad)
        for key, label in DEVICE_TYPES.items():
            ttk.Radiobutton(type_frame, text=label, variable=self._type_var, value=key,
                            command=self._on_type_change).pack(anchor=tk.W)

        # --- Test button ---
        test_frame = ttk.Frame(c)
        test_frame.pack(fill=tk.X, **pad)
        ttk.Button(test_frame, text="Test Connection", command=self._on_test).pack(side=tk.LEFT)
        self._test_status = ttk.Label(test_frame, text="")
        self._test_status.pack(side=tk.LEFT, padx=8)

        # --- Test output ---
        self._test_output = tk.Text(c, height=4, wrap=tk.WORD, font=("Consolas", 9),
                                    bg="#1e1e1e", fg="#cccccc", state=tk.DISABLED)
        self._test_output.pack(fill=tk.X, **pad)

        # --- Name ---
        ttk.Label(c, text="Device Name:").pack(anchor=tk.W, **pad)
        self._name_var = tk.StringVar()
        ttk.Entry(c, textvariable=self._name_var, width=50).pack(fill=tk.X, **pad)

        # --- MTP device name (only for mtp) ---
        self._mtp_frame = ttk.Frame(c)
        ttk.Label(self._mtp_frame, text="MTP Device Name (from test above):").pack(anchor=tk.W)
        self._mtp_device_var = tk.StringVar(value="Apple iPhone")
        ttk.Entry(self._mtp_frame, textvariable=self._mtp_device_var, width=50).pack(fill=tk.X)
        # Windows names every iPhone "Apple iPhone", so the name above cannot tell two
        # of them apart. Pairing the photo job with the device's full-backup job lets
        # the executor check the UDID before it copies anything.
        ttk.Label(self._mtp_frame, text="Same device as (full-backup job, checked before copying):").pack(anchor=tk.W, pady=(6, 0))
        self._twin_var = tk.StringVar(value=NO_TWIN)
        self._twin_ids: dict[str, str] = {NO_TWIN: ""}
        for jid, j in self.app.config_mgr.jobs.items():
            if j.type == "ios_backup":
                self._twin_ids[j.name] = jid
        ttk.Combobox(self._mtp_frame, textvariable=self._twin_var, values=list(self._twin_ids),
                     state="readonly", width=47).pack(fill=tk.X)

        # --- Device class (only for ios_backup) ---
        self._ios_frame = ttk.Frame(c)
        ttk.Label(self._ios_frame, text="Device kind (only used the first time, to bind the job to the right device):").pack(anchor=tk.W)
        self._device_class_var = tk.StringVar(value="iPhone")
        ios_row = ttk.Frame(self._ios_frame)
        ios_row.pack(fill=tk.X)
        for kind in ("iPhone", "iPad"):
            ttk.Radiobutton(ios_row, text=kind, variable=self._device_class_var, value=kind).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Label(
            self._ios_frame, wraplength=480, justify=tk.LEFT,
            text="The first run binds this job to the one device plugged in, turns on backup "
                 "encryption with IOS_BACKUP_PASSWORD from .env, and takes a full backup. "
                 "Later runs are incremental and refuse any other device.",
        ).pack(anchor=tk.W, pady=(4, 0))

        # --- FFS batch file (only for ffs) ---
        self._ffs_frame = ttk.Frame(c)
        ttk.Label(self._ffs_frame, text="FreeFileSync Batch File:").pack(anchor=tk.W)
        ffs_row = ttk.Frame(self._ffs_frame)
        ffs_row.pack(fill=tk.X)
        self._ffs_var = tk.StringVar()
        ttk.Entry(ffs_row, textvariable=self._ffs_var, width=40).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(ffs_row, text="Browse...", command=self._browse_ffs).pack(side=tk.RIGHT, padx=(4, 0))

        # --- Test path (for ffs - checks device is connected) ---
        self._test_path_frame = ttk.Frame(c)
        ttk.Label(self._test_path_frame, text="Test Path (must exist before running, e.g. G:\\DCIM):").pack(anchor=tk.W)
        tp_row = ttk.Frame(self._test_path_frame)
        tp_row.pack(fill=tk.X)
        self._test_path_var = tk.StringVar()
        ttk.Entry(tp_row, textvariable=self._test_path_var, width=40).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(tp_row, text="Browse...", command=self._browse_test_path).pack(side=tk.RIGHT, padx=(4, 0))

        # --- Source ---
        self._source_frame = ttk.Frame(c)
        ttk.Label(self._source_frame, text="Source Path:").pack(anchor=tk.W)
        self._source_var = tk.StringVar(value=DEFAULT_SOURCES["adbsync"])
        ttk.Entry(self._source_frame, textvariable=self._source_var, width=50).pack(fill=tk.X)

        # --- Destination ---
        ttk.Label(c, text="Backup Destination:").pack(anchor=tk.W, **pad)
        dest_frame = ttk.Frame(c)
        dest_frame.pack(fill=tk.X, **pad)
        self._dest_var = tk.StringVar()
        ttk.Entry(dest_frame, textvariable=self._dest_var, width=40).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(dest_frame, text="Browse...", command=self._browse_dest).pack(side=tk.RIGHT, padx=(4, 0))

        # --- Excludes ---
        self._exclude_frame = ttk.Frame(c)
        ttk.Label(self._exclude_frame, text="Exclude Patterns (one per line):").pack(anchor=tk.W)
        self._exclude_text = tk.Text(self._exclude_frame, height=3, font=("Consolas", 9), width=50)
        self._exclude_text.pack(fill=tk.X)

        # --- Dependencies ---
        self._deps_frame = ttk.LabelFrame(c, text="Depends On")
        self._dep_vars: dict[str, tk.BooleanVar] = {}

        # --- Buttons ---
        btn_frame = ttk.Frame(c)
        btn_frame.pack(fill=tk.X, padx=10, pady=10, side=tk.BOTTOM)
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(btn_frame, text="Save", command=self._on_save).pack(side=tk.RIGHT, padx=(0, 8))
        self._run_btn = ttk.Button(btn_frame, text="Save & Run", command=self._on_save_and_run)
        self._run_btn.pack(side=tk.RIGHT, padx=(0, 8))
        if not self.edit_job:
            self._run_btn.configure(state=tk.DISABLED)  # Can't run a job that doesn't exist yet

        self._on_type_change()

    def _on_type_change(self) -> None:
        dtype = self._type_var.get()
        # Hide all type-specific frames
        self._mtp_frame.pack_forget()
        self._ios_frame.pack_forget()
        self._ffs_frame.pack_forget()
        self._test_path_frame.pack_forget()
        self._source_frame.pack_forget()
        self._exclude_frame.pack_forget()
        self._deps_frame.pack_forget()

        if dtype == "mtp":
            self._mtp_frame.pack(fill=tk.X, padx=10, pady=4)
            self._source_frame.pack(fill=tk.X, padx=10, pady=4)
            self._exclude_frame.pack(fill=tk.X, padx=10, pady=4)
            self._source_var.set(DEFAULT_SOURCES["mtp"])
        elif dtype == "ios_backup":
            self._ios_frame.pack(fill=tk.X, padx=10, pady=4)
        elif dtype == "ffs":
            self._ffs_frame.pack(fill=tk.X, padx=10, pady=4)
            self._test_path_frame.pack(fill=tk.X, padx=10, pady=4)
        elif dtype == "adbsync":
            self._source_frame.pack(fill=tk.X, padx=10, pady=4)
            self._exclude_frame.pack(fill=tk.X, padx=10, pady=4)
            self._source_var.set(DEFAULT_SOURCES["adbsync"])
            if not self.edit_job:
                self._exclude_text.delete("1.0", tk.END)
                self._exclude_text.insert("1.0", DEFAULT_EXCLUDES["adbsync"])

        # Dependencies (always shown)
        self._build_deps()
        self._deps_frame.pack(fill=tk.X, padx=10, pady=4)

    def _build_deps(self) -> None:
        for w in self._deps_frame.winfo_children():
            w.destroy()
        self._dep_vars.clear()
        current_deps = self.edit_job.depends_on if self.edit_job else []
        current_id = self.edit_job.id if self.edit_job else None
        for jid, j in self.app.config_mgr.jobs.items():
            if jid == current_id:
                continue
            var = tk.BooleanVar(value=jid in current_deps)
            self._dep_vars[jid] = var
            ttk.Checkbutton(self._deps_frame, text=j.name, variable=var).pack(anchor=tk.W, padx=4)
        if not self._dep_vars:
            ttk.Label(self._deps_frame, text="(no other jobs)").pack(anchor=tk.W, padx=4)

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
                elif dtype == "ios_backup":
                    result = subprocess.run(
                        [console_python(), "-m", "backup_manager.ios_backup", "list"],
                        capture_output=True, text=True, timeout=10,
                        cwd=str(self.app.base_dir), encoding="utf-8", errors="replace",
                    )
                    output = result.stdout + result.stderr
                elif dtype == "ffs":
                    # Check test_path if provided
                    tp = self._test_path_var.get().strip()
                    if tp:
                        from pathlib import Path
                        if Path(tp).exists():
                            output = f"OK: {tp} exists - device is connected."
                        else:
                            output = f"NOT FOUND: {tp} - device may not be connected."
                    else:
                        output = "Set a Test Path (e.g. G:\\DCIM) to check if the device is connected."
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

    def _browse_test_path(self) -> None:
        path = filedialog.askdirectory(title="Select Test Path (e.g. camera drive root)")
        if path:
            self._test_path_var.set(path)

    def _populate_from_job(self, job: Job) -> None:
        self._type_var.set(job.type)
        self._on_type_change()
        self._name_var.set(job.name)
        self._source_var.set(job.source)
        self._dest_var.set(job.destination)
        self._test_path_var.set(job.test_path)
        if job.type == "mtp":
            self._mtp_device_var.set(job.device_name)
            for twin_name, twin_id in self._twin_ids.items():
                if twin_id and twin_id == job.same_device_as:
                    self._twin_var.set(twin_name)
        if job.type == "ios_backup" and job.device_class:
            self._device_class_var.set(job.device_class)
        if job.type == "ffs":
            self._ffs_var.set(job.ffs_batch)
        if job.exclude:
            self._exclude_text.delete("1.0", tk.END)
            self._exclude_text.insert("1.0", "\n".join(job.exclude))
        self._run_btn.configure(state=tk.NORMAL)

    def _build_job_from_form(self) -> Job | None:
        name = self._name_var.get().strip()
        if not name:
            messagebox.showerror("Error", "Device name is required.", parent=self)
            return None

        dtype = self._type_var.get()
        job_id = self.edit_job.id if self.edit_job else sanitize_job_id(name)

        if not self.edit_job and job_id in self.app.config_mgr.jobs:
            messagebox.showerror("Error", f"A job with ID '{job_id}' already exists.", parent=self)
            return None

        dest = self._dest_var.get().strip()
        if dtype != "ffs" and not dest:
            messagebox.showerror("Error", "Backup destination is required.", parent=self)
            return None

        excludes = []
        if dtype in ("adbsync", "mtp"):
            raw = self._exclude_text.get("1.0", tk.END).strip()
            if raw:
                excludes = [line.strip() for line in raw.splitlines() if line.strip()]

        depends_on = [jid for jid, var in self._dep_vars.items() if var.get()]

        return Job(
            id=job_id,
            name=name,
            type=dtype,
            # Keep the group on edit: an archived device stays archived.
            group=self.edit_job.group if self.edit_job else "device",
            source=self._source_var.get().strip(),
            destination=dest,
            exclude=excludes,
            depends_on=depends_on,
            device_name=self._mtp_device_var.get().strip() if dtype == "mtp" else "",
            recursive=True if dtype == "mtp" else False,
            device_class=self._device_class_var.get() if dtype == "ios_backup" else "",
            same_device_as=self._twin_ids.get(self._twin_var.get(), "") if dtype == "mtp" else "",
            ffs_batch=self._ffs_var.get().strip() if dtype == "ffs" else "",
            test_path=self._test_path_var.get().strip(),
            # Preserve fields not exposed in this wizard so editing a device
            # doesn't silently drop them (e.g. camera post_script, adbsync adb_encoding).
            post_script=self.edit_job.post_script if self.edit_job else "",
            adb_encoding=self.edit_job.adb_encoding if self.edit_job else "",
        )

    def _on_save(self) -> None:
        job = self._build_job_from_form()
        if not job:
            return
        if self.edit_job:
            self.app.config_mgr.update_job(job)
        else:
            self.app.config_mgr.add_job(job)
        self.on_save()
        self.destroy()

    def _on_save_and_run(self) -> None:
        job = self._build_job_from_form()
        if not job:
            return
        if self.edit_job:
            self.app.config_mgr.update_job(job)
        else:
            self.app.config_mgr.add_job(job)
        self.on_save()
        self.destroy()
        # Trigger run on the main frame
        self.app.main_frame._run_job(job.id)
