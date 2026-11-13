from __future__ import annotations

import os
import re
import tkinter as tk
from tkinter import ttk, messagebox
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..app import App


# CSI sequences and two-character escapes. adb, rclone and RCU all colour their output
# and move the cursor around; none of that means anything to a Text widget.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")

# Long runs stream tens of thousands of lines. Past a few thousand the widget starts to
# crawl, and nobody scrolls back that far anyway - the log file has the full record.
MAX_LOG_LINES = 5000


class MainFrame(ttk.Frame):
    def __init__(self, parent: tk.Tk, app: App):
        super().__init__(parent)
        self.app = app
        self.pack(fill=tk.BOTH, expand=True)

        self._job_items: dict[str, str] = {}  # job_id -> treeview item id
        self._session_completed: set[str] = set()  # jobs completed this session
        # Run queue (George, 2026-11-13: "one useful tool would be able to add a job to a
        # queue"). Jobs run one at a time, in order, each starting when the one before ends.
        self._queue: list[str] = []
        # Per queued job: the unhealthy dependencies the user said to queue it anyway with. Any
        # OTHER dependency problem found when its turn comes (typically one queued ahead of it
        # that then failed) means it is skipped, not run.
        self._queue_accepted: dict[str, set[str]] = {}
        self._skipped: set[str] = set()  # skipped from the queue this session, until run again
        self._running_id: str | None = None
        self._next_pending = False  # a retry of _start_next_queued is scheduled

        self._build_toolbar()
        self._build_job_list()
        self._build_log_panel()

    # --- Toolbar ---
    def _build_toolbar(self) -> None:
        toolbar = ttk.Frame(self)
        toolbar.pack(fill=tk.X, padx=8, pady=(8, 4))

        ttk.Button(toolbar, text="+ Add Device", command=self._on_add_device).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(toolbar, text="Debug Tools", command=self._on_debug_tools).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(toolbar, text="Open Logs", command=self._on_open_logs).pack(side=tk.LEFT, padx=(0, 4))

        self._stop_btn = ttk.Button(toolbar, text="Stop", command=self._on_stop, state=tk.DISABLED)
        self._stop_btn.pack(side=tk.RIGHT)
        self._clear_queue_btn = ttk.Button(toolbar, text="Clear Queue", command=self._on_clear_queue,
                                           state=tk.DISABLED)
        self._clear_queue_btn.pack(side=tk.RIGHT, padx=(0, 4))
        self._queue_label = ttk.Label(toolbar, text="")
        self._queue_label.pack(side=tk.RIGHT, padx=(0, 8))

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
        self.tree.tag_configure("changed", foreground="#cc7a00")
        self.tree.tag_configure("queued", foreground="#7a3db8")

        # Context menu
        self._context_menu = tk.Menu(self.tree, tearoff=0)
        self._context_menu.add_command(label="Run", command=self._on_run_selected)
        self._context_menu.add_command(label="Add to Queue", command=self._on_queue_selected)
        self._context_menu.add_command(label="Remove from Queue", command=self._on_unqueue_selected)
        self._context_menu.add_separator()
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
        # Rebuilding the tree must not lose what the user was looking at.
        selected = self._get_selected_job_id()
        was_open = {self.tree.item(g, "text"): bool(self.tree.item(g, "open"))
                    for g in self.tree.get_children()}
        self.tree.delete(*self.tree.get_children())
        self._job_items.clear()
        self._build_job_rows()
        for g in self.tree.get_children():
            if self.tree.item(g, "text") in was_open:
                self.tree.item(g, open=was_open[self.tree.item(g, "text")])
        self._show_live_status()
        if selected and selected in self._job_items:
            self.tree.selection_set(self._job_items[selected])

    def _show_live_status(self) -> None:
        """Overlay what only this session knows (running, queued, skipped) on the rows."""
        for job_id in self._skipped:
            item = self._job_items.get(job_id)
            if item:
                self.tree.item(item, values=("Skipped", self.tree.item(item, "values")[1]), tags=("failed",))
        for pos, job_id in enumerate(self._queue, 1):
            item = self._job_items.get(job_id)
            if item:
                self.tree.item(item, values=(f"Queued #{pos}", self.tree.item(item, "values")[1]),
                               tags=("queued",))
        item = self._job_items.get(self._running_id) if self._running_id else None
        if item:
            self.tree.item(item, values=("Running...", self.tree.item(item, "values")[1]), tags=("running",))
        n = len(self._queue)
        self._queue_label.configure(text=f"Queue: {n}" if n else "")
        self._clear_queue_btn.configure(state=tk.NORMAL if n else tk.DISABLED)

    def _build_job_rows(self) -> None:

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
                from ..utils import is_fresh, relative_time
                last = relative_time(history.last_success)
                status = "Idle"
                tag = "idle"
                # A run from this session, or a recent enough one from any session:
                # restarting the app does not make a 13-minute-old backup stale.
                if self.app.job_changed(job):
                    # Edited since it last succeeded (settings, code or scripts): it counts
                    # as never run until it succeeds in its new form (George, 2026-11-12).
                    status = "Re-run"
                    tag = "changed"
                    last = "Never (changed)"
                elif job.id in self._session_completed or is_fresh(history.last_attempt):
                    if history.last_status == "success":
                        status = "Done"
                        tag = "success"
                    elif history.last_status == "failed":
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
            # Archived jobs start collapsed: they are kept for the record, not run (George 2026-11-12).
            group_node = self.tree.insert("", tk.END, text=group_key.upper(), tags=("group",),
                                          open=group_key != "archived")
            for job in jobs:
                history = self.app.history_mgr.get(job.id)
                from ..utils import relative_time
                last = relative_time(history.last_success)
                status, tag = "Idle", "idle"
                if self.app.job_changed(job):
                    status, tag, last = "Re-run", "changed", "Never (changed)"
                item_id = self.tree.insert(
                    group_node, tk.END, text=f"  {job.name}",
                    values=(status, last), tags=(tag,)
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
        text = _ANSI.sub("", text).replace("\x07", "")
        if not text:
            return
        # CRLF is a line ending, not an erase. Normalise it first so the split below
        # only ever sees a bare CR, which really does mean "redraw this line".
        text = text.replace("\r\n", "\n")
        self.log_text.configure(state=tk.NORMAL)
        # A carriage return means "redraw this line", so honour it instead of appending:
        # otherwise a progress line stacks up hundreds of half-finished copies of itself.
        for i, chunk in enumerate(text.split("\r")):
            if i:
                self.log_text.delete("end-1c linestart", "end-1c")
            if chunk:
                self.log_text.insert(tk.END, chunk)
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            self.log_text.delete("1.0", f"{lines - MAX_LOG_LINES}.0")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _clear_log(self) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

    # --- Actions ---
    def _dependency_problems(self, job, assume_ok: set[str] = frozenset()) -> list[tuple[str, str]]:
        """(dep_id, reason) for each dependency that isn't satisfied.

        One is satisfied by a success this session, or by a last run that succeeded within 24
        hours of a job that hasn't been edited since - restarting the app doesn't make an
        hour-old phone backup stale (George, 2026-11-12). `assume_ok` are jobs that will run
        first (the running one and those queued ahead), so they count as satisfied for now.
        """
        from ..utils import is_fresh, relative_time
        problems = []
        for dep_id in job.depends_on or []:
            dep = self.app.config_mgr.jobs.get(dep_id)
            if not dep or dep_id in self._session_completed or dep_id in assume_ok:
                continue
            h = self.app.history_mgr.get(dep_id)
            if self.app.job_changed(dep):
                problems.append((dep_id, f"{dep.name}: changed since it last ran"))
            elif h.last_status != "success":
                problems.append((dep_id, f"{dep.name}: last run {h.last_status}" if h.last_attempt
                                 else f"{dep.name}: never run"))
            elif not is_fresh(h.last_success):
                problems.append((dep_id, f"{dep.name}: last succeeded {relative_time(h.last_success)}"))
        return problems

    def _confirm_dependencies(self, job, problems: list[tuple[str, str]], verb: str) -> bool:
        if not problems:
            return True
        msg = (f"{job.name} depends on jobs that haven't succeeded in the last 24 hours:\n\n"
               + "\n".join(f"  - {p}" for _, p in problems) + f"\n\n{verb} anyway?")
        return messagebox.askyesno("Dependency Warning", msg)

    def _on_run_selected(self) -> None:
        job_id = self._get_selected_job_id()
        if not job_id:
            messagebox.showinfo("Run", "Select a job first.")
            return
        job = self.app.config_mgr.jobs.get(job_id)
        if not job:
            return
        # Something already running (or waiting): "Run" joins the queue instead of refusing.
        if self._running_id or self.app.executor.is_running or self._queue:
            self._enqueue(job_id)
            return
        if not self._confirm_dependencies(job, self._dependency_problems(job), "Run"):
            return
        self._run_job(job_id)

    # --- Queue ---
    def _on_queue_selected(self) -> None:
        job_id = self._get_selected_job_id()
        if job_id:
            self._enqueue(job_id)

    def _enqueue(self, job_id: str) -> None:
        job = self.app.config_mgr.jobs.get(job_id)
        if not job:
            return
        if job_id == self._running_id or job_id in self._queue:
            messagebox.showinfo("Queue", f"{job.name} is already {'running' if job_id == self._running_id else 'queued'}.")
            return
        ahead = set(self._queue) | ({self._running_id} if self._running_id else set())
        problems = self._dependency_problems(job, assume_ok=ahead)
        if not self._confirm_dependencies(job, problems, "Queue"):
            return
        self._queue.append(job_id)
        # Only the problems the user just said yes to. A dependency queued ahead isn't in here:
        # if it then fails, this job is skipped when its turn comes.
        self._queue_accepted[job_id] = {d for d, _ in problems}
        self._skipped.discard(job_id)
        self.append_log(f"[QUEUE] {job.name} queued (#{len(self._queue)})\n")
        self.refresh_job_list()
        self._start_next_queued()  # no-op while a job is running

    def _on_unqueue_selected(self) -> None:
        job_id = self._get_selected_job_id()
        if job_id in self._queue:
            self._queue.remove(job_id)
            self._queue_accepted.pop(job_id, None)
            job = self.app.config_mgr.jobs.get(job_id)
            self.append_log(f"[QUEUE] {job.name if job else job_id} removed from the queue\n")
            self.refresh_job_list()

    def _on_clear_queue(self) -> None:
        if not self._queue:
            return
        n = len(self._queue)
        self._queue.clear()
        self._queue_accepted.clear()
        self.append_log(f"[QUEUE] cleared ({n} job{'s' if n != 1 else ''} not run)\n")
        self.refresh_job_list()

    def _start_next_queued(self) -> None:
        if self._running_id:
            return  # a job is running; its on_job_complete starts the next one
        # on_complete fires from inside the executor's thread, so it may not have exited yet.
        # Only one retry may be pending, or two could each pop a job and one would be lost.
        if self.app.executor.is_running:
            if not self._next_pending:
                self._next_pending = True
                self.after(250, self._retry_start_next)
            return
        while self._queue:
            job_id = self._queue.pop(0)
            accepted = self._queue_accepted.pop(job_id, set())
            job = self.app.config_mgr.jobs.get(job_id)
            if not job:
                continue  # deleted while it waited
            # A dependency that was queued ahead and then failed is a reason to skip, not to
            # ask: the queue is for running unattended. Problems accepted when it was queued
            # still run, as the user chose.
            new_problems = [p for d, p in self._dependency_problems(job) if d not in accepted]
            if new_problems:
                self._skipped.add(job_id)
                self.append_log(f"[QUEUE] Skipped {job.name}: " + "; ".join(new_problems) + "\n")
                continue
            self._run_job(job_id)
            break
        self.refresh_job_list()

    def _retry_start_next(self) -> None:
        self._next_pending = False
        self._start_next_queued()

    def _run_job(self, job_id: str) -> None:
        job = self.app.config_mgr.jobs.get(job_id)
        if not job:
            return
        if self.app.executor.is_running:
            messagebox.showwarning("Busy", "Another job is already running.")
            return

        self._running_id = job_id
        self._skipped.discard(job_id)
        item = self._job_items.get(job_id)
        if item:
            self.tree.item(item, values=("Running...", self.tree.item(item, "values")[1]), tags=("running",))
        self._stop_btn.configure(state=tk.NORMAL)

        self.app.run_job(job)

    def on_job_complete(self, job_id: str, status: str, duration: int) -> None:
        # Only a success satisfies depends_on. Counting every finished job let a mirror run
        # without warning after the phone job it depends on had failed.
        if status == "success":
            self._session_completed.add(job_id)
        else:
            self._session_completed.discard(job_id)
        self._running_id = None
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

        if self._queue:
            self._start_next_queued()

    def _on_open_logs(self) -> None:
        log_dir = self.app.log_dir
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            os.startfile(str(log_dir))  # Windows-only; so is the rest of this app
        except AttributeError:
            messagebox.showinfo("Logs", str(log_dir))
        except OSError as e:
            messagebox.showerror("Logs", f"Could not open {log_dir}:\n{e}")

    def _on_stop(self) -> None:
        # Ask before cancelling, so the next queued job can't start in between.
        if self._queue:
            n = len(self._queue)
            if messagebox.askyesno("Stop", f"Also clear the {n} queued job{'s' if n != 1 else ''}?\n\n"
                                           "No stops only the running job; the queue then carries on."):
                self._on_clear_queue()
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
        if job_id == self._running_id:
            messagebox.showwarning("Delete Job", f"'{job.name}' is running. Stop it first.")
            return
        if messagebox.askyesno("Delete Job", f"Delete '{job.name}'?"):
            if job_id in self._queue:
                self._queue.remove(job_id)
                self._queue_accepted.pop(job_id, None)
            self.app.config_mgr.delete_job(job_id)
            self.refresh_job_list()

    def _on_right_click(self, event: tk.Event) -> None:
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            # Only show context menu for job items, not group headers
            job_id = self._get_selected_job_id()
            if job_id:
                busy = job_id == self._running_id
                queued = job_id in self._queue
                self._context_menu.entryconfigure(
                    "Run", state=tk.DISABLED if busy or queued else tk.NORMAL)
                self._context_menu.entryconfigure(
                    "Add to Queue", state=tk.DISABLED if busy or queued else tk.NORMAL)
                self._context_menu.entryconfigure(
                    "Remove from Queue", state=tk.NORMAL if queued else tk.DISABLED)
                self._context_menu.post(event.x_root, event.y_root)
