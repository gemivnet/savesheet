from __future__ import annotations

import tkinter as tk
from pathlib import Path

from .config import ConfigManager, Job
from .executor import JobExecutor
from .fingerprint import FingerprintStore, job_fingerprint
from .history import HistoryManager


class App:
    def __init__(self, config_path: Path):
        self.base_dir = config_path.parent
        self.config_mgr = ConfigManager(config_path)
        self.config_mgr.load()

        self.history_mgr = HistoryManager(self.base_dir / "backup_history.yaml")
        self.history_mgr.load()
        # Fingerprint of each job's last successful run; a job edited since shows "Re-run".
        self.fingerprints = FingerprintStore(self.base_dir / "backup_fingerprints.yaml")
        self.fingerprints.load()
        self._run_fingerprints: dict[str, str] = {}

        self.log_dir = self.base_dir / "logs"

        self.root = tk.Tk()
        self.root.title("Backup Manager")
        self.root.geometry("750x650")
        self.root.minsize(600, 450)

        self.executor = JobExecutor(
            tools=self.config_mgr.tools,
            base_dir=self.base_dir,
            on_output=self._on_output,
            on_complete=self._on_complete,
            log_dir=self.log_dir,
            # A lambda, not .jobs.get: ConfigManager.load() replaces the dict.
            job_lookup=lambda job_id: self.config_mgr.jobs.get(job_id),
        )

        from .ui.main_frame import MainFrame
        self.main_frame = MainFrame(self.root, self)

    def _on_output(self, text: str) -> None:
        self.root.after(0, self.main_frame.append_log, text)

    def _on_complete(self, job_id: str, status: str, duration: int) -> None:
        self.history_mgr.record(job_id, status, duration)
        # The fingerprint taken when the run started: the job as it actually ran.
        fp = self._run_fingerprints.pop(job_id, None)
        if status == "success" and fp:
            try:
                self.fingerprints.set(job_id, fp)
            except OSError as e:
                self._on_output(f"[WARN] Could not record the job's fingerprint: {e}\n")
        self.root.after(0, self.main_frame.on_job_complete, job_id, status, duration)

    def run_job(self, job: Job) -> None:
        self._run_fingerprints[job.id] = job_fingerprint(job, self.base_dir)
        self.executor.run_job(job)

    def job_changed(self, job: Job) -> bool:
        """Succeeded before, but not in its current form (settings, code or scripts)."""
        if not self.history_mgr.get(job.id).last_success:
            return False
        return self.fingerprints.get(job.id) != job_fingerprint(job, self.base_dir)

    def run(self) -> None:
        self.root.mainloop()
