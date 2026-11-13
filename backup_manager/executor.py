from __future__ import annotations

import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, TextIO

from .config import Job, ToolPaths
from .utils import console_python, local_exe, resolve_path


class AdbsyncTreeFilter:
    """Drops the full-tree dumps adbsync logs at INFO before every sync.

    For each run BetterADBSync prints the whole source tree, the whole destination tree and
    three excluded/unaccounted trees: 188,000 lines for the Retroid on 2026-11-12, which the
    log panel drew one by one ("tree takes sooo long") and which made a 7.5 MB log. The
    "Delete tree" and "Copy tree" sections - what actually changes - and everything after
    SYNCING are kept; each hidden section is replaced by one line saying how big it was.
    """
    HIDE = {"Source tree:", "Destination tree:", "Source excluded tree:",
            "Destination unaccounted tree:", "Destination excluded tree:",
            "Non-excluded-supporting destination unaccounted tree:"}

    def __init__(self) -> None:
        self.section: str | None = None
        self.hidden = 0

    def __call__(self, line: str) -> str | None:
        text = line.rstrip("\r\n")
        body = text[len("[INFO]"):].strip() if text.startswith("[INFO]") else None
        if self.section is not None:
            if body == "":          # adbsync ends each section with an empty INFO line
                out = f"[INFO] {self.section} ({self.hidden:,} lines not shown)\n"
                self.section, self.hidden = None, 0
                return out
            self.hidden += 1
            return None
        if body in self.HIDE:
            self.section = body
            return None
        return line


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
        job_lookup: Callable[[str], Job | None] | None = None,
    ):
        self.tools = tools
        # Lets one job consult another's settings (mtp's same_device_as).
        self.job_lookup = job_lookup or (lambda _job_id: None)
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
            self._kill_tree(proc)

    @staticmethod
    def _kill_tree(proc: subprocess.Popen[str]) -> None:
        """Stop the command and everything it started. Commands run through shell=True, so
        the process we hold is cmd.exe; terminate() killed only that, and rclone, FFS,
        PowerShell or an iOS backup kept running and writing behind a job marked
        cancelled - with the next job free to start alongside it."""
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
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
        except (OSError, ValueError) as e:
            # log_dir is often a network share, and one blip during an eight-hour job
            # used to end logging silently: the file just stopped, which reads as the
            # job stopping rather than the logging stopping. Try once to pick it back
            # up, and say so either way - a gap nobody knows about is worse than none.
            self._log_file = None
            try:
                if self._log_path is None:
                    raise OSError("no log path")
                self._log_file = open(self._log_path, "a", encoding="utf-8", errors="replace")
                self._log_file.write(f"\n[WARN] Log write failed ({e}); reopened.\n")
                self._log_file.write(text)  # the chunk that lost the race
                self._log_file.flush()
                self.on_output(f"[WARN] Log write failed ({e}); reopened {self._log_path}\n")
            except OSError as reopen_error:
                self._log_file = None
                self.on_output(
                    f"[WARN] Log write failed ({e}) and could not reopen ({reopen_error}); "
                    f"this run is no longer being logged to disk.\n"
                )

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

            # A multi-folder rclone job keeps going past a failed folder, so one bad share
            # doesn't leave the others uncopied; the job still fails at the end.
            keep_going = job.type == "rclone" and len(job.parts) > 1
            failed_parts = 0
            for cmd in commands:
                if self._cancel_flag.is_set():
                    self._emit("\n[CANCELLED]\n")
                    self.on_complete(job.id, "failed", int(time.time() - start))
                    return
                self._emit(f"> {cmd}\n")
                line_filter = AdbsyncTreeFilter() if job.type == "adbsync" and "adbsync" in cmd.split('"')[1].lower() else None
                exit_code = self._run_command(cmd, line_filter)
                if exit_code != 0:
                    self._emit(f"\n[FAILED] Exit code: {exit_code}\n")
                    if keep_going and not self._cancel_flag.is_set():
                        failed_parts += 1
                        continue
                    self.on_complete(job.id, "failed", int(time.time() - start))
                    return
            if failed_parts:
                self._emit(f"\n[FAILED] {failed_parts} of {len(commands)} folders failed\n")
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

    def _run_command(self, cmd: str, line_filter: Callable[[str], str | None] | None = None) -> int:
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
                    self._kill_tree(self._process)
                    return -1
                if line_filter is not None:
                    line = line_filter(line)
                    if line is None:
                        continue
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
            "ios_backup": self._build_ios_backup,
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
        # adbsync compares mtimes only, so a pull cut short is never repaired: the short file
        # is newer than the phone's. adb_verify re-pulls any copy whose size is wrong.
        verify = [f'"{console_python()}" -m backup_manager.adb_verify --adb "{adb}"',
                  f'--source "{job.source}" --dest "{job.destination}"']
        if job.adb_encoding:
            verify.append(f'--adb-encoding "{job.adb_encoding}"')
        for exc in job.exclude:
            verify.append(f'--exclude "{exc}"')
        return [" ".join(parts), " ".join(verify)]

    def _build_mtp(self, job: Job) -> list[str]:
        mdc = self._resolve(self.tools.media_device_copier)
        parts = [f'"{mdc}" download-files']
        parts.append(f'-n "{job.device_name}"')
        parts.append(f'-s "{job.source}"')
        parts.append(f'-t "{job.destination}"')
        if job.recursive:
            parts.append("-r")
        commands = []
        if job.same_device_as:
            # MediaDeviceCopier can only pick a device by the name Windows gives it, and
            # Windows gives every iPhone the same one. Check the UDID first, so a second
            # iPhone on the cable fails the job instead of filling the wrong folder.
            twin = self.job_lookup(job.same_device_as)
            if twin is None or twin.type != "ios_backup" or not twin.destination:
                raise ValueError(
                    f"same_device_as: '{job.same_device_as}' is not an ios_backup job with a destination"
                )
            commands.append(
                f'{self._ios_module()} verify --dest "{self._win_arg(twin.destination)}" '
                f'--label "{self._win_arg(job.name)}"'
            )
        commands.append(" ".join(parts))
        return commands

    def _ios_module(self) -> str:
        # -u: unbuffered, so progress reaches the panel as it happens rather than in
        # 8 KB bursts. Runs from base_dir (see _run_command), where the package lives.
        return f'"{console_python()}" -u -m backup_manager.ios_backup'

    @staticmethod
    def _win_arg(value: str) -> str:
        """Make a value safe inside double quotes on a Windows command line: a trailing
        backslash would escape the closing quote, and a quote would end the argument."""
        return value.rstrip("\\").replace('"', "'")

    def _build_ios_backup(self, job: Job) -> list[str]:
        if not job.destination:
            raise ValueError("ios_backup jobs need a destination")
        parts = [f'{self._ios_module()} backup']
        parts.append(f'--dest "{self._win_arg(job.destination)}"')
        parts.append(f'--label "{self._win_arg(job.name)}"')
        if job.device_class:
            parts.append(f'--device-class "{self._win_arg(job.device_class)}"')
        # The backup password is read from .env by the module itself. It must never be
        # an argument: every command line is echoed to the panel and written to logs/.
        parts.append(f'--env-file "{self._resolve(".env")}"')
        return [" ".join(parts)]

    def _build_rclone(self, job: Job) -> list[str]:
        # Run rclone from a copy on C:, not from the share: see local_exe.
        rclone = str(local_exe(Path(self._resolve(self.tools.rclone))))
        commands = []
        if job.rclone_config_create:
            commands.append(f'"{rclone}" config create {job.rclone_config_create}')

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        # A job either has one source/destination or a list of parts (several folders in
        # one job); each part becomes its own rclone command with the job's flags.
        targets = job.parts or [{"source": job.source, "destination": job.destination,
                                 "exclude": job.exclude}]
        for target in targets:
            commands.append(self._rclone_command(job, rclone, timestamp, target["source"],
                                                 target["destination"], target.get("exclude") or []))
        return commands

    def _rclone_command(self, job: Job, rclone: str, timestamp: str, source: str, dest: str,
                        excludes: list[str]) -> str:
        # Strip trailing backslashes: a quoted Windows path ending in "\" (e.g. "G:\Files\")
        # makes the closing quote escape, which corrupts command-line argument parsing.
        source = source.rstrip("\\") or source
        dest = dest.rstrip("\\") or dest

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
        for exc in excludes:
            parts.append(f'--exclude "{exc}"')
        for key, value in job.flags.items():
            if isinstance(value, bool) and value:
                parts.append(f"--{key}")
            elif not isinstance(value, bool):
                parts.append(f"--{key} {value}")
        return " ".join(parts)

    def _build_ffs(self, job: Job) -> list[str]:
        ffs = self._resolve(self.tools.freefile_sync)
        batch_file = self._resolve(job.ffs_batch)
        # A copy-new-only batch silently skips a new file whose name is already taken (a
        # camera restarting C0001 after a format); card_conflicts saves those beside the old.
        return [f'"{ffs}" "{batch_file}"',
                f'"{console_python()}" -m backup_manager.card_conflicts "{batch_file}"']

    def _build_script(self, job: Job) -> list[str]:
        script = self._resolve(job.script_path)
        cmd = f'powershell -ExecutionPolicy Bypass -File "{script}"'
        if job.script_args:
            cmd += f" {job.script_args}"
        return [cmd]
