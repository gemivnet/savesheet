from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


@dataclass
class JobHistory:
    last_success: str | None = None
    last_attempt: str | None = None
    last_status: str = "never"  # success | failed | never
    last_duration_seconds: int = 0


class HistoryManager:
    def __init__(self, history_path: Path):
        self.history_path = history_path
        self._lock = threading.Lock()
        self._data: dict[str, JobHistory] = {}

    def load(self) -> None:
        with self._lock:
            if not self.history_path.exists():
                self._data = {}
                return
            with open(self.history_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            self._data = {}
            for job_id, entry in raw.items():
                self._data[job_id] = JobHistory(
                    last_success=entry.get("last_success"),
                    last_attempt=entry.get("last_attempt"),
                    last_status=entry.get("last_status", "never"),
                    last_duration_seconds=entry.get("last_duration_seconds", 0),
                )

    def get(self, job_id: str) -> JobHistory:
        with self._lock:
            return self._data.get(job_id, JobHistory())

    def record(self, job_id: str, status: str, duration_seconds: int) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            entry = self._data.get(job_id, JobHistory())
            entry.last_attempt = now
            entry.last_status = status
            entry.last_duration_seconds = duration_seconds
            if status == "success":
                entry.last_success = now
            self._data[job_id] = entry
            self._save()

    def _save(self) -> None:
        out: dict[str, Any] = {}
        for job_id, entry in self._data.items():
            d: dict[str, Any] = {}
            if entry.last_success:
                d["last_success"] = entry.last_success
            if entry.last_attempt:
                d["last_attempt"] = entry.last_attempt
            d["last_status"] = entry.last_status
            d["last_duration_seconds"] = entry.last_duration_seconds
            out[job_id] = d
        with open(self.history_path, "w", encoding="utf-8") as f:
            yaml.dump(out, f, default_flow_style=False, sort_keys=False)
