from __future__ import annotations

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from typing import TYPE_CHECKING, Any, Callable

from ..config import Job

if TYPE_CHECKING:
    from ..app import App


class JobEditor(tk.Toplevel):
    """General-purpose job editor for any job type."""

    def __init__(self, parent: tk.Tk, app: App, job: Job, on_save: Callable[[], None]):
        super().__init__(parent)
        self.app = app
        self.job = job
        self.on_save_cb = on_save

        self.title(f"Edit: {job.name}")
        self.geometry("550x600")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        # For device types, delegate to DeviceWizard
        if job.type in ("adbsync", "mtp", "ffs"):
            self.destroy()
            from .device_wizard import DeviceWizard
            DeviceWizard(parent, app, on_save=on_save, edit_job=job)
            return

        self._build_ui()

    def _build_ui(self) -> None:
        canvas = tk.Canvas(self)
        scrollbar = ttk.Scrollbar(self, orient=tk.VERTICAL, command=canvas.yview)
        scroll_frame = ttk.Frame(canvas)

        scroll_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        pad = {"padx": 10, "pady": 4}
        job = self.job

        # Name
        ttk.Label(scroll_frame, text="Name:").pack(anchor=tk.W, **pad)
        self._name_var = tk.StringVar(value=job.name)
        ttk.Entry(scroll_frame, textvariable=self._name_var, width=50).pack(fill=tk.X, **pad)

        # Type (read-only display)
        ttk.Label(scroll_frame, text=f"Type: {job.type}").pack(anchor=tk.W, **pad)

        # Group
        ttk.Label(scroll_frame, text="Group:").pack(anchor=tk.W, **pad)
        self._group_var = tk.StringVar(value=job.group)
        group_frame = ttk.Frame(scroll_frame)
        group_frame.pack(fill=tk.X, **pad)
        ttk.Radiobutton(group_frame, text="Device", variable=self._group_var, value="device").pack(side=tk.LEFT)
        ttk.Radiobutton(group_frame, text="Sync", variable=self._group_var, value="sync").pack(side=tk.LEFT, padx=10)

        if job.type == "rclone":
            # Rclone command
            ttk.Label(scroll_frame, text="Rclone Command:").pack(anchor=tk.W, **pad)
            self._rclone_cmd_var = tk.StringVar(value=job.rclone_command)
            cmd_frame = ttk.Frame(scroll_frame)
            cmd_frame.pack(fill=tk.X, **pad)
            for cmd in ("copy", "sync", "move"):
                ttk.Radiobutton(cmd_frame, text=cmd, variable=self._rclone_cmd_var, value=cmd).pack(side=tk.LEFT, padx=(0, 10))

            # Source
            ttk.Label(scroll_frame, text="Source:").pack(anchor=tk.W, **pad)
            self._source_var = tk.StringVar(value=job.source)
            ttk.Entry(scroll_frame, textvariable=self._source_var, width=50).pack(fill=tk.X, **pad)

            # Destination
            ttk.Label(scroll_frame, text="Destination:").pack(anchor=tk.W, **pad)
            self._dest_var = tk.StringVar(value=job.destination)
            dest_frame = ttk.Frame(scroll_frame)
            dest_frame.pack(fill=tk.X, **pad)
            ttk.Entry(dest_frame, textvariable=self._dest_var, width=40).pack(side=tk.LEFT, fill=tk.X, expand=True)
            ttk.Button(dest_frame, text="Browse...", command=self._browse_dest).pack(side=tk.RIGHT, padx=(4, 0))

            # Excludes
            ttk.Label(scroll_frame, text="Exclude Patterns (one per line):").pack(anchor=tk.W, **pad)
            self._exclude_text = tk.Text(scroll_frame, height=3, font=("Consolas", 9), width=50)
            self._exclude_text.pack(fill=tk.X, **pad)
            if job.exclude:
                self._exclude_text.insert("1.0", "\n".join(job.exclude))

            # Rclone config create
            ttk.Label(scroll_frame, text="Rclone Config Create (optional):").pack(anchor=tk.W, **pad)
            self._rclone_config_var = tk.StringVar(value=job.rclone_config_create)
            ttk.Entry(scroll_frame, textvariable=self._rclone_config_var, width=50).pack(fill=tk.X, **pad)

            # Flags
            ttk.Label(scroll_frame, text="Flags (key=value, one per line):").pack(anchor=tk.W, **pad)
            self._flags_text = tk.Text(scroll_frame, height=4, font=("Consolas", 9), width=50)
            self._flags_text.pack(fill=tk.X, **pad)
            if job.flags:
                lines = []
                for k, v in job.flags.items():
                    if isinstance(v, bool):
                        lines.append(f"{k}={'true' if v else 'false'}")
                    else:
                        lines.append(f"{k}={v}")
                self._flags_text.insert("1.0", "\n".join(lines))

        elif job.type == "script":
            ttk.Label(scroll_frame, text="Script Path:").pack(anchor=tk.W, **pad)
            self._script_var = tk.StringVar(value=job.script_path)
            script_frame = ttk.Frame(scroll_frame)
            script_frame.pack(fill=tk.X, **pad)
            ttk.Entry(script_frame, textvariable=self._script_var, width=40).pack(side=tk.LEFT, fill=tk.X, expand=True)
            ttk.Button(script_frame, text="Browse...", command=self._browse_script).pack(side=tk.RIGHT, padx=(4, 0))

        # Dependencies
        ttk.Label(scroll_frame, text="Depends On (select jobs):").pack(anchor=tk.W, **pad)
        deps_frame = ttk.Frame(scroll_frame)
        deps_frame.pack(fill=tk.X, **pad)
        self._dep_vars: dict[str, tk.BooleanVar] = {}
        for jid, j in self.app.config_mgr.jobs.items():
            if jid == job.id:
                continue
            var = tk.BooleanVar(value=jid in job.depends_on)
            self._dep_vars[jid] = var
            ttk.Checkbutton(deps_frame, text=j.name, variable=var).pack(anchor=tk.W)

        # Buttons
        btn_frame = ttk.Frame(scroll_frame)
        btn_frame.pack(fill=tk.X, padx=10, pady=10)
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(btn_frame, text="Save", command=self._on_save).pack(side=tk.RIGHT, padx=(0, 8))

    def _browse_dest(self) -> None:
        path = filedialog.askdirectory(title="Select Destination")
        if path:
            self._dest_var.set(path)

    def _browse_script(self) -> None:
        path = filedialog.askopenfilename(
            title="Select Script",
            filetypes=[("PowerShell", "*.ps1"), ("Batch", "*.bat"), ("All files", "*.*")]
        )
        if path:
            self._script_var.set(path)

    def _parse_flags(self) -> dict[str, Any]:
        flags: dict[str, Any] = {}
        raw = self._flags_text.get("1.0", tk.END).strip()
        for line in raw.splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if value.lower() == "true":
                flags[key] = True
            elif value.lower() == "false":
                flags[key] = False
            else:
                try:
                    flags[key] = int(value)
                except ValueError:
                    flags[key] = value
        return flags

    def _on_save(self) -> None:
        name = self._name_var.get().strip()
        if not name:
            messagebox.showerror("Error", "Name is required.", parent=self)
            return

        depends_on = [jid for jid, var in self._dep_vars.items() if var.get()]

        job = Job(
            id=self.job.id,
            name=name,
            type=self.job.type,
            group=self._group_var.get(),
            depends_on=depends_on,
        )

        if self.job.type == "rclone":
            job.rclone_command = self._rclone_cmd_var.get()
            job.source = self._source_var.get().strip()
            job.destination = self._dest_var.get().strip()
            job.rclone_config_create = self._rclone_config_var.get().strip()
            job.flags = self._parse_flags()
            raw_exc = self._exclude_text.get("1.0", tk.END).strip()
            if raw_exc:
                job.exclude = [l.strip() for l in raw_exc.splitlines() if l.strip()]
        elif self.job.type == "script":
            job.script_path = self._script_var.get().strip()

        self.app.config_mgr.update_job(job)
        self.on_save_cb()
        self.destroy()
