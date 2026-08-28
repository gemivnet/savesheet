from __future__ import annotations

import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TextIO

from .config import Job, ToolPaths
from .utils import resolve_path


class JobExecutor:
    """Builds and runs backup commands in a background thread."""

    def __init__(
        self,
        tools: ToolPaths,
        base_dir: Path,
        on_output: Callable[[str], None],
        on_complete: Callable[[str, str, int], None],  # job_id, status, duration
        log_dir: Path | None = None,
        log_retention_days: int = 30,
    ):
        self.tools = tools
        self.base_dir = base_dir
        self.on_output = on_output
        self.on_complete = on_complete
        self.log_dir = log_dir or (base_dir / "logs")
        self.log_retention_days = log_retention_days
        self._process: subprocess.Popen[str] | None = None
        self._cancel_flag = threading.Event()
        self._thread: threading.Thread | None = None
        self._log_file: TextIO | None = None
        self._log_path: Path | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def run_job(self, job: Job) -> None:
        if self.is_running:
            self.on_output("[ERROR] Another job is already running.\n")
            return
        self._cancel_flag.clear()
        self._thread = threading.Thread(target=self._execute, args=(job,), daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel_flag.set()
        proc = self._process
        if proc and proc.poll() is None:
            proc.terminate()

    def _execute(self, job: Job) -> None:
        self._open_log(job)
        try:
            self._execute_job(job)
        finally:
            self._close_log()

    # --- Run logging ---------------------------------------------------------
    # The GUI log panel is ephemeral: it is cleared by the Clear button and lost
    # on exit, so a job that failed while nobody was watching left nothing behind
    # to diagnose. Every run now also streams to its own file under log_dir.

    def _emit(self, text: str) -> None:
        """Fan one chunk of output out to the GUI and to this run's log file."""
        self.on_output(text)
        f = self._log_file
        if f is None:
            return
        try:
            f.write(text)
            # Flushed per write: a run that is cancelled, crashes, or takes the
            # whole app down with it must still leave a readable log.
            f.flush()
        except (OSError, ValueError):
            self._log_file = None

    def _open_log(self, job: Job) -> None:
        self._log_file = None
        self._log_path = None
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            self._prune_logs()
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            path = self.log_dir / f"{stamp}_{job.id}.log"
            self._log_file = open(path, "w", encoding="utf-8", errors="replace")
            self._log_path = path
        except OSError as e:
            # Losing the log must never cost you the backup.
            self.on_output(f"[WARN] Could not open log file: {e}\n")
            self._log_file = None
            return
        self._emit(f"[LOG] {self._log_path}\n")

    def _close_log(self) -> None:
        f = self._log_file
        self._log_file = None
        if f is not None:
            try:
                f.close()
            except OSError:
                pass

    def _prune_logs(self) -> None:
        """Drop logs older than log_retention_days so the directory stays bounded."""
        if self.log_retention_days <= 0:
            return
        cutoff = time.time() - self.log_retention_days * 86400
        for old in self.log_dir.glob("*.log"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass

    def _execute_job(self, job: Job) -> None:
        start = time.time()
        self._emit(f"--- Starting: {job.name} ---\n")
        try:
            # Pre-run check: verify test_path exists
            if job.test_path:
                test = resolve_path(job.test_path, self.base_dir)
                if not test.exists():
                    self._emit(f"[ERROR] Device not connected: {job.test_path} not found\n")
                    self.on_complete(job.id, "failed", 0)
                    return
                self._emit(f"[OK] Found: {job.test_path}\n")

            commands = self._build_commands(job)
            if not commands:
                self._emit(f"[ERROR] Don't know how to run job type '{job.type}'\n")
                self.on_complete(job.id, "failed", 0)
                return

            for cmd in commands:
                if self._cancel_flag.is_set():
                    self._emit("\n[CANCELLED]\n")
                    self.on_complete(job.id, "failed", int(time.time() - start))
                    return
                self._emit(f"> {cmd}\n")
                exit_code = self._run_command(cmd)
                if exit_code != 0:
                    self._emit(f"\n[FAILED] Exit code: {exit_code}\n")
                    self.on_complete(job.id, "failed", int(time.time() - start))
                    return

            # Post-step: run after the main job succeeds (e.g. immich upload after camera sync)
            if job.post_script:
                if self._cancel_flag.is_set():
                    self._emit("\n[CANCELLED]\n")
                    self.on_complete(job.id, "failed", int(time.time() - start))
                    return
                post = self._resolve(job.post_script)
                post_cmd = f'powershell -ExecutionPolicy Bypass -File "{post}"'
                self._emit(f"\n--- Post-step ---\n> {post_cmd}\n")
                exit_code = self._run_command(post_cmd)
                if exit_code != 0:
                    self._emit(f"\n[FAILED] Post-step exit code: {exit_code}\n")
                    self.on_complete(job.id, "failed", int(time.time() - start))
                    return

            duration = int(time.time() - start)
            self._emit(f"\n--- Done: {job.name} ({duration}s) ---\n")
            self.on_complete(job.id, "success", duration)
        except Exception as e:
            self._emit(f"\n[ERROR] {e}\n")
            self.on_complete(job.id, "failed", int(time.time() - start))

    def _run_command(self, cmd: str) -> int:
        try:
            self._process = subprocess.Popen(
                cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=str(self.base_dir),
                encoding="utf-8",
                errors="replace",
            )
            assert self._process.stdout is not None
            for line in self._process.stdout:
                if self._cancel_flag.is_set():
                    self._process.terminate()
                    return -1
                self._emit(line)
            self._process.wait()
            return self._process.returncode
        except FileNotFoundError as e:
            self._emit(f"[ERROR] Command not found: {e}\n")
            return -1
        finally:
            self._process = None

    def _build_commands(self, job: Job) -> list[str]:
        builders = {
            "adbsync": self._build_adbsync,
            "mtp": self._build_mtp,
            "rclone": self._build_rclone,
            "ffs": self._build_ffs,
            "script": self._build_script,
        }
        builder = builders.get(job.type)
        if not builder:
            return []
        return builder(job)

    def _resolve(self, path: str) -> str:
        return str(resolve_path(path, self.base_dir))

    def _build_adbsync(self, job: Job) -> list[str]:
        adbsync = self._resolve(self.tools.adbsync)
        adb = self._resolve(self.tools.adb)
        parts = [f'"{adbsync}" --adb-bin "{adb}"']
        if job.adb_encoding:
            parts.append(f'--adb-encoding "{job.adb_encoding}"')
        for exc in job.exclude:
            parts.append(f'--exclude "{exc}"')
        parts.append(f'pull "{job.source}" "{job.destination}"')
        return [" ".join(parts)]

    def _build_mtp(self, job: Job) -> list[str]:
        mdc = self._resolve(self.tools.media_device_copier)
        parts = [f'"{mdc}" download-files']
        parts.append(f'-n "{job.device_name}"')
        parts.append(f'-s "{job.source}"')
        parts.append(f'-t "{job.destination}"')
        if job.recursive:
            parts.append("-r")
        return [" ".join(parts)]

    def _build_rclone(self, job: Job) -> list[str]:
        rclone = self._resolve(self.tools.rclone)
        commands = []
        if job.rclone_config_create:
            commands.append(f'"{rclone}" config create {job.rclone_config_create}')

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        # Strip trailing backslashes: a quoted Windows path ending in "\" (e.g. "G:\Files\")
        # makes the closing quote escape, which corrupts command-line argument parsing.
        source = job.source.rstrip("\\") or job.source
        dest = job.destination.rstrip("\\") or job.destination

        parts = [f'"{rclone}" {job.rclone_command} "{source}" "{dest}"']
        # Not --progress: it redraws in place with carriage returns and cursor moves,
        # which the log panel can only render as a wall of half-finished lines. One-line
        # stats carry the same information and are newline-terminated.
        if "stats" not in job.flags:
            parts.append("--stats 5s")
        if "stats-one-line" not in job.flags:
            parts.append("--stats-one-line")
        # --stats prints at INFO, but rclone's default log level is NOTICE, so without
        # this the periodic line never appears at all and the panel sits silent for the
        # whole run. -v would also work, at the cost of a line per file.
        if "stats-log-level" not in job.flags:
            parts.append("--stats-log-level NOTICE")
        # --suffix-keep-extension puts the timestamp before the extension
        # (noco.20260105-162122.db) instead of after it (noco.db.20260105-162122).
        # Without it every superseded version loses its file type: nothing will open
        # them, and since G:\Files\Server Backup is itself an rclone source they get
        # re-copied out to the NAS, D:\Mirror, Google Drive and Glacier. That is where
        # the current 6,365 files / ~15 GB of unopenable shadow copies came from.
        # Note: this does not retro-fix the existing ones, which still need a sweep.
        parts.append(f'--suffix ".{timestamp}"')
        parts.append("--suffix-keep-extension")
        for exc in job.exclude:
            parts.append(f'--exclude "{exc}"')
        for key, value in job.flags.items():
            if isinstance(value, bool) and value:
                parts.append(f"--{key}")
            elif not isinstance(value, bool):
                parts.append(f"--{key} {value}")
        commands.append(" ".join(parts))
        return commands

    def _build_ffs(self, job: Job) -> list[str]:
        ffs = self._resolve(self.tools.freefile_sync)
        batch_file = self._resolve(job.ffs_batch)
        return [f'"{ffs}" "{batch_file}"']

    def _build_script(self, job: Job) -> list[str]:
        script = self._resolve(job.script_path)
        cmd = f'powershell -ExecutionPolicy Bypass -File "{script}"'
        if job.script_args:
            cmd += f" {job.script_args}"
        return [cmd]
