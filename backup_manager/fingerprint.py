"""Which jobs have changed since they last succeeded.

George, 2026-11-12: "if you update a job then we should mark it as never run so I know to
re-run it". A job's fingerprint is a hash of everything that decides what it does:

* its settings (everything in backup_config.yaml except name, group and depends_on, which
  don't change what it copies);
* JOB_LOGIC_VERSION for its type - bump that when changing how the executor builds that
  type's commands, or a helper module the type runs (adb_verify for adbsync,
  card_conflicts for ffs, newest_copy for the HA script, ...);
* the contents of the files it runs that live in this repo (script_path, post_script,
  ffs_batch), plus same-named siblings of a script in a subfolder
  (tools/immich-sync/immich_sync.ps1 -> immich_sync.py).

The fingerprint of each job's last SUCCESSFUL run is kept in backup_fingerprints.yaml -
deliberately not in backup_history.yaml, so an older copy of the app that is still open
can't drop it when it rewrites the history. A job whose current fingerprint differs from
that one (or has none recorded) shows as needing a run.
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path, PureWindowsPath
from typing import Any

import yaml

from .config import Job

# Bump a type's number whenever its command builder (executor.py) or a helper it runs changes
# behaviour, so every job of that type shows as needing a run.
JOB_LOGIC_VERSION = {
    "adbsync": 3,     # 2026-11-12: adb_verify size check after the pull; v3 lists via xargs (Retroid overflowed find -exec)
    "mtp": 1,
    "ios_backup": 1,
    "rclone": 1,
    "ffs": 2,         # 2026-11-12: card_conflicts after the batch
    "script": 1,
}

IGNORED_FIELDS = {"name", "group", "depends_on"}


def _is_absolute(p: str) -> bool:
    """Absolute on Windows terms (G:\\..., \\\\server\\...), whatever OS computes this, so the
    same job hashes the same on the PC and on the server."""
    return PureWindowsPath(p).is_absolute() or p.startswith("/")


def _file_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "missing"


def job_fingerprint(job: Job, base_dir: Path) -> str:
    settings = {k: v for k, v in job.to_dict().items() if k not in IGNORED_FIELDS}
    files: dict[str, str] = {}
    for ref in (job.script_path, job.post_script, job.ffs_batch):
        if not ref or _is_absolute(ref):
            continue  # outside the repo: the path itself is part of the settings
        rel = ref.replace("\\", "/")
        path = base_dir / rel
        files[rel] = _file_digest(path)
        parent = Path(rel).parent
        if ref == job.script_path and str(parent) not in ("", "."):
            for sib in sorted((base_dir / parent).glob(Path(rel).stem + ".*")):
                if sib.suffix.lower() in (".py", ".ps1") and sib.name != Path(rel).name:
                    files[(parent / sib.name).as_posix()] = _file_digest(sib)
    blob = json.dumps({"settings": settings, "logic": JOB_LOGIC_VERSION.get(job.type, 1),
                       "files": files}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


class FingerprintStore:
    """job id -> fingerprint of its last successful run."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, str] = {}

    def load(self) -> None:
        with self._lock:
            try:
                raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            except FileNotFoundError:
                raw = {}
            self._data = {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}

    def get(self, job_id: str) -> str | None:
        with self._lock:
            return self._data.get(job_id)

    def set(self, job_id: str, fingerprint: str) -> None:
        with self._lock:
            self._data[job_id] = fingerprint
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(yaml.safe_dump(dict(sorted(self._data.items())), sort_keys=False),
                           encoding="utf-8")
            tmp.replace(self.path)


def changed_since_success(job: Job, base_dir: Path, store: FingerprintStore, has_success: bool) -> bool:
    """True when the job has succeeded before but not in its current form."""
    return has_success and store.get(job.id) != job_fingerprint(job, base_dir)
