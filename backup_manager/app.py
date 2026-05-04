from __future__ import annotations

import tkinter as tk
from pathlib import Path

from .config import ConfigManager, Job
from .executor import JobExecutor
from .history import HistoryManager


class App:
    def __init__(self, config_path: Path):
        self.base_dir = config_path.parent
        self.config_mgr = ConfigManager(config_path)
        self.config_mgr.load()

        self.history_mgr = HistoryManager(self.base_dir / "backup_history.yaml")
        self.history_mgr.load()

        self.root = tk.Tk()
        self.root.title("Backup Manager")
        self.root.geometry("750x650")
        self.root.minsize(600, 450)

        self.executor = JobExecutor(
            tools=self.config_mgr.tools,
            base_dir=self.base_dir,
            on_output=self._on_output,
            on_complete=self._on_complete,
        )

        from .ui.main_frame import MainFrame
        self.main_frame = MainFrame(self.root, self)

    def _on_output(self, text: str) -> None:
        self.root.after(0, self.main_frame.append_log, text)

    def _on_complete(self, job_id: str, status: str, duration: int) -> None:
        self.history_mgr.record(job_id, status, duration)
        self.root.after(0, self.main_frame.on_job_complete, job_id, status, duration)

    def run_job(self, job: Job) -> None:
        self.executor.run_job(job)

    def run(self) -> None:
        self.root.mainloop()
