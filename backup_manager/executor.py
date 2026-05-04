from __future__ import annotations

import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

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
    ):
        self.tools = tools
        self.base_dir = base_dir
        self.on_output = on_output
        self.on_complete = on_complete
        self._process: subprocess.Popen[str] | None = None
        self._cancel_flag = threading.Event()
        self._thread: threading.Thread | None = None

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
        start = time.time()
        self.on_output(f"--- Starting: {job.name} ---\n")
        try:
            commands = self._build_commands(job)
            if not commands:
                self.on_output(f"[ERROR] Don't know how to run job type '{job.type}'\n")
                self.on_complete(job.id, "failed", 0)
                return

            for cmd in commands:
                if self._cancel_flag.is_set():
                    self.on_output("\n[CANCELLED]\n")
                    self.on_complete(job.id, "failed", int(time.time() - start))
                    return
                self.on_output(f"> {cmd}\n")
                exit_code = self._run_command(cmd)
                if exit_code != 0:
                    self.on_output(f"\n[FAILED] Exit code: {exit_code}\n")
                    self.on_complete(job.id, "failed", int(time.time() - start))
                    return

            duration = int(time.time() - start)
            self.on_output(f"\n--- Done: {job.name} ({duration}s) ---\n")
            self.on_complete(job.id, "success", duration)
        except Exception as e:
            self.on_output(f"\n[ERROR] {e}\n")
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
                self.on_output(line)
            self._process.wait()
            return self._process.returncode
        except FileNotFoundError as e:
            self.on_output(f"[ERROR] Command not found: {e}\n")
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
        source = job.source
        dest = job.destination

        parts = [f'"{rclone}" {job.rclone_command} "{source}" "{dest}"']
        parts.append("--progress")
        parts.append(f'--suffix ".{timestamp}"')
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
        return [f'powershell -ExecutionPolicy Bypass -File "{script}"']
