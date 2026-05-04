from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..app import App


class MainFrame(ttk.Frame):
    def __init__(self, parent: tk.Tk, app: App):
        super().__init__(parent)
        self.app = app
        self.pack(fill=tk.BOTH, expand=True)

        self._job_items: dict[str, str] = {}  # job_id -> treeview item id
        self._session_completed: set[str] = set()  # jobs completed this session

        self._build_toolbar()
        self._build_job_list()
        self._build_log_panel()

    # --- Toolbar ---
    def _build_toolbar(self) -> None:
        toolbar = ttk.Frame(self)
        toolbar.pack(fill=tk.X, padx=8, pady=(8, 4))

        ttk.Button(toolbar, text="+ Add Device", command=self._on_add_device).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(toolbar, text="Debug Tools", command=self._on_debug_tools).pack(side=tk.LEFT, padx=(0, 4))

        self._stop_btn = ttk.Button(toolbar, text="Stop", command=self._on_stop, state=tk.DISABLED)
        self._stop_btn.pack(side=tk.RIGHT)

    # --- Job List ---
    def _build_job_list(self) -> None:
        list_frame = ttk.Frame(self)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        columns = ("status", "last_backup")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="Job", anchor=tk.W)
        self.tree.heading("status", text="Status")
        self.tree.heading("last_backup", text="Last Backup")

        self.tree.column("#0", width=300, minwidth=200)
        self.tree.column("status", width=100, minwidth=80, anchor=tk.CENTER)
        self.tree.column("last_backup", width=140, minwidth=100, anchor=tk.CENTER)

        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Tag styles
        self.tree.tag_configure("group", font=("Segoe UI", 10, "bold"))
        self.tree.tag_configure("idle", foreground="#555555")
        self.tree.tag_configure("running", foreground="#0066cc")
        self.tree.tag_configure("success", foreground="#228B22")
        self.tree.tag_configure("failed", foreground="#cc0000")

        # Context menu
        self._context_menu = tk.Menu(self.tree, tearoff=0)
        self._context_menu.add_command(label="Run", command=self._on_run_selected)
        self._context_menu.add_command(label="Edit", command=self._on_edit_selected)
        self._context_menu.add_separator()
        self._context_menu.add_command(label="Delete", command=self._on_delete_selected)

        self.tree.bind("<Button-3>", self._on_right_click)
        self.tree.bind("<Double-1>", lambda e: self._on_edit_selected())

        # Button column via treeview isn't great in ttk - we'll use context menu + toolbar buttons
        # But we can bind single click on specific areas
        self.tree.bind("<Return>", lambda e: self._on_run_selected())

        self.refresh_job_list()

    def refresh_job_list(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self._job_items.clear()

        groups = self.app.config_mgr.get_jobs_by_group()
        group_labels = {"device": "DEVICES", "sync": "SYNC"}

        for group_key in ["device", "sync"]:
            jobs = groups.get(group_key, [])
            if not jobs:
                continue
            label = group_labels.get(group_key, group_key.upper())
            group_node = self.tree.insert("", tk.END, text=label, tags=("group",), open=True)

            for job in jobs:
                history = self.app.history_mgr.get(job.id)
                from ..utils import relative_time
                last = relative_time(history.last_success)
                status = "Idle"
                tag = "idle"
                if job.id in self._session_completed:
                    h = self.app.history_mgr.get(job.id)
                    if h.last_status == "success":
                        status = "Done"
                        tag = "success"
                    elif h.last_status == "failed":
                        status = "Failed"
                        tag = "failed"

                item_id = self.tree.insert(
                    group_node, tk.END, text=f"  {job.name}",
                    values=(status, last), tags=(tag,)
                )
                self._job_items[job.id] = item_id

        # Also handle any unknown groups
        for group_key, jobs in groups.items():
            if group_key in ("device", "sync"):
                continue
            group_node = self.tree.insert("", tk.END, text=group_key.upper(), tags=("group",), open=True)
            for job in jobs:
                history = self.app.history_mgr.get(job.id)
                from ..utils import relative_time
                last = relative_time(history.last_success)
                item_id = self.tree.insert(
                    group_node, tk.END, text=f"  {job.name}",
                    values=("Idle", last), tags=("idle",)
                )
                self._job_items[job.id] = item_id

    def _get_selected_job_id(self) -> str | None:
        selection = self.tree.selection()
        if not selection:
            return None
        item = selection[0]
        for job_id, item_id in self._job_items.items():
            if item_id == item:
                return job_id
        return None

    # --- Log Panel ---
    def _build_log_panel(self) -> None:
        log_frame = ttk.LabelFrame(self, text="Log Output")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 8))

        btn_frame = ttk.Frame(log_frame)
        btn_frame.pack(fill=tk.X)
        ttk.Button(btn_frame, text="Clear", command=self._clear_log).pack(side=tk.RIGHT, padx=4, pady=2)

        self.log_text = tk.Text(log_frame, height=12, wrap=tk.WORD, state=tk.DISABLED,
                                font=("Consolas", 9), bg="#1e1e1e", fg="#cccccc",
                                insertbackground="#cccccc", selectbackground="#264f78")
        log_scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(4, 0), pady=(0, 4))
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y, pady=(0, 4))

        self.append_log("Ready.\n")

    def append_log(self, text: str) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, text)
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _clear_log(self) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

    # --- Actions ---
    def _on_run_selected(self) -> None:
        job_id = self._get_selected_job_id()
        if not job_id:
            messagebox.showinfo("Run", "Select a job first.")
            return
        job = self.app.config_mgr.jobs.get(job_id)
        if not job:
            return

        # Check dependencies
        if job.depends_on:
            missing = [d for d in job.depends_on if d not in self._session_completed]
            if missing:
                names = [self.app.config_mgr.jobs[d].name for d in missing if d in self.app.config_mgr.jobs]
                msg = f"{job.name} depends on jobs that haven't run this session:\n\n" + "\n".join(f"  - {n}" for n in names) + "\n\nRun anyway?"
                if not messagebox.askyesno("Dependency Warning", msg):
                    return

        self._run_job(job_id)

    def _run_job(self, job_id: str) -> None:
        job = self.app.config_mgr.jobs.get(job_id)
        if not job:
            return
        if self.app.executor.is_running:
            messagebox.showwarning("Busy", "Another job is already running.")
            return

        # Update UI
        item = self._job_items.get(job_id)
        if item:
            self.tree.item(item, values=("Running...", self.tree.item(item, "values")[1]), tags=("running",))
        self._stop_btn.configure(state=tk.NORMAL)

        self.app.run_job(job)

    def on_job_complete(self, job_id: str, status: str, duration: int) -> None:
        self._session_completed.add(job_id)
        self._stop_btn.configure(state=tk.DISABLED)

        item = self._job_items.get(job_id)
        if item:
            from ..utils import relative_time
            history = self.app.history_mgr.get(job_id)
            last = relative_time(history.last_success)
            if status == "success":
                self.tree.item(item, values=("Done", last), tags=("success",))
            else:
                self.tree.item(item, values=("Failed", last), tags=("failed",))

    def _on_stop(self) -> None:
        self.app.executor.cancel()

    def _on_add_device(self) -> None:
        from .device_wizard import DeviceWizard
        DeviceWizard(self.winfo_toplevel(), self.app, on_save=self.refresh_job_list)

    def _on_debug_tools(self) -> None:
        from .debug_dialog import DebugDialog
        DebugDialog(self.winfo_toplevel(), self.app)

    def _on_edit_selected(self) -> None:
        job_id = self._get_selected_job_id()
        if not job_id:
            return
        job = self.app.config_mgr.jobs.get(job_id)
        if not job:
            return
        from .job_editor import JobEditor
        JobEditor(self.winfo_toplevel(), self.app, job=job, on_save=self.refresh_job_list)

    def _on_delete_selected(self) -> None:
        job_id = self._get_selected_job_id()
        if not job_id:
            return
        job = self.app.config_mgr.jobs.get(job_id)
        if not job:
            return
        if messagebox.askyesno("Delete Job", f"Delete '{job.name}'?"):
            self.app.config_mgr.delete_job(job_id)
            self.refresh_job_list()

    def _on_right_click(self, event: tk.Event) -> None:
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            # Only show context menu for job items, not group headers
            if self._get_selected_job_id():
                self._context_menu.post(event.x_root, event.y_root)
