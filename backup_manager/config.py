from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class Job:
    id: str
    name: str
    type: str  # adbsync | mtp | rclone | ffs | script
    group: str = "device"  # device | sync
    source: str = ""
    destination: str = ""
    exclude: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    # MTP-specific
    device_name: str = ""
    recursive: bool = False
    # FFS-specific
    ffs_batch: str = ""
    # Pre-run check: path that must exist before running (e.g. drive letter for camera)
    test_path: str = ""
    # Post-step: PowerShell script to run after the job succeeds (e.g. immich upload after camera sync)
    post_script: str = ""
    # adbsync-specific: encoding for adb shell output. Use "latin-1" for devices with
    # non-UTF-8 filenames (e.g. game handhelds) so directory listings don't crash.
    adb_encoding: str = ""
    # Rclone-specific
    rclone_command: str = "copy"
    rclone_config_create: str = ""
    flags: dict[str, Any] = field(default_factory=dict)
    # Script-specific
    script_path: str = ""
    # Extra arguments appended to the script command line (e.g. "-Bucket backup-2026-07-12")
    script_args: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dict suitable for YAML output."""
        d: dict[str, Any] = {"name": self.name, "type": self.type, "group": self.group}
        if self.source:
            d["source"] = self.source
        if self.destination:
            d["destination"] = self.destination
        if self.exclude:
            d["exclude"] = self.exclude
        if self.depends_on:
            d["depends_on"] = self.depends_on
        if self.device_name:
            d["device_name"] = self.device_name
        if self.recursive:
            d["recursive"] = True
        if self.ffs_batch:
            d["ffs_batch"] = self.ffs_batch
        if self.test_path:
            d["test_path"] = self.test_path
        if self.post_script:
            d["post_script"] = self.post_script
        if self.adb_encoding:
            d["adb_encoding"] = self.adb_encoding
        if self.type == "rclone":
            d["rclone_command"] = self.rclone_command
            if self.rclone_config_create:
                d["rclone_config_create"] = self.rclone_config_create
            if self.flags:
                d["flags"] = self.flags
        if self.script_path:
            d["script_path"] = self.script_path
        if self.script_args:
            d["script_args"] = self.script_args
        return d


@dataclass
class ToolPaths:
    adb: str = "platform-tools/adb.exe"
    adbsync: str = "adbsync.exe"
    rclone: str = "rclone/rclone.exe"
    media_device_copier: str = "MediaDeviceCopier.exe"
    freefile_sync: str = "FreeFileSync.exe"


class ConfigManager:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.base_dir = config_path.parent
        self.tools = ToolPaths()
        self.jobs: dict[str, Job] = {}
        self._raw: dict[str, Any] = {}

    def load(self) -> None:
        if not self.config_path.exists():
            self._raw = {"tools": {}, "jobs": {}}
            return
        with open(self.config_path, "r", encoding="utf-8") as f:
            self._raw = yaml.safe_load(f) or {"tools": {}, "jobs": {}}
        tools_data = self._raw.get("tools", {})
        if tools_data:
            self.tools = ToolPaths(
                adb=tools_data.get("adb", self.tools.adb),
                adbsync=tools_data.get("adbsync", self.tools.adbsync),
                rclone=tools_data.get("rclone", self.tools.rclone),
                media_device_copier=tools_data.get("media_device_copier", self.tools.media_device_copier),
                freefile_sync=tools_data.get("freefile_sync", self.tools.freefile_sync),
            )
        self.jobs = {}
        for job_id, job_data in self._raw.get("jobs", {}).items():
            self.jobs[job_id] = self._parse_job(job_id, job_data)

    def _parse_job(self, job_id: str, data: dict[str, Any]) -> Job:
        return Job(
            id=job_id,
            name=data.get("name", job_id),
            type=data.get("type", "script"),
            group=data.get("group", "device"),
            source=data.get("source", ""),
            destination=data.get("destination", ""),
            exclude=data.get("exclude", []),
            depends_on=data.get("depends_on", []),
            device_name=data.get("device_name", ""),
            recursive=data.get("recursive", False),
            ffs_batch=data.get("ffs_batch", ""),
            test_path=data.get("test_path", ""),
            post_script=data.get("post_script", ""),
            adb_encoding=data.get("adb_encoding", ""),
            rclone_command=data.get("rclone_command", "copy"),
            rclone_config_create=data.get("rclone_config_create", ""),
            flags=data.get("flags", {}),
            script_path=data.get("script_path", ""),
            script_args=data.get("script_args", ""),
        )

    def save(self) -> None:
        tools_dict = {
            "adb": self.tools.adb,
            "adbsync": self.tools.adbsync,
            "rclone": self.tools.rclone,
            "media_device_copier": self.tools.media_device_copier,
            "freefile_sync": self.tools.freefile_sync,
        }
        jobs_dict = {}
        for job_id, job in self.jobs.items():
            jobs_dict[job_id] = job.to_dict()
        output = {"tools": tools_dict, "jobs": jobs_dict}
        with open(self.config_path, "w", encoding="utf-8") as f:
            yaml.dump(output, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    def add_job(self, job: Job) -> None:
        self.jobs[job.id] = job
        self.save()

    def update_job(self, job: Job) -> None:
        self.jobs[job.id] = job
        self.save()

    def delete_job(self, job_id: str) -> None:
        self.jobs.pop(job_id, None)
        self.save()

    def get_jobs_by_group(self) -> dict[str, list[Job]]:
        groups: dict[str, list[Job]] = {}
        for job in self.jobs.values():
            groups.setdefault(job.group, []).append(job)
        return groups
