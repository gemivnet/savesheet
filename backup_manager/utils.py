from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


def console_python() -> str:
    """The interpreter this app is running under, in its console form.

    run.pyw starts the app with pythonw.exe, which has no stdout to stream;
    python.exe beside it is the same environment with one.
    """
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        console = exe.with_name("python.exe")
        if console.exists():
            exe = console
    return str(exe)


def _on_network_drive(path: Path) -> bool:
    """True if Windows would page this file in over the network (a mapped drive or UNC path)."""
    if os.name != "nt":
        return False
    anchor = path.anchor
    if anchor.startswith("\\\\"):
        return True
    import ctypes
    DRIVE_REMOTE = 4
    return ctypes.windll.kernel32.GetDriveTypeW(anchor) == DRIVE_REMOTE


def local_exe(path: Path) -> Path:
    """A copy of a network-hosted executable on the PC's own disk, refreshed when it changes.

    Windows does not load an .exe into memory when it starts: it maps the file and reads each
    page of code the first time that code runs. For rclone at N:\\workspace\\savesheet that means
    reading from the server's SMB share hours into a run, and if the share stalls the read
    fails and the process dies at once with STATUS_IN_PAGE_ERROR (0xc0000006), which rclone's
    Go runtime reports as "unexpected fault address ... fatal error: fault". That killed
    Files to Google Drive on 2026-11-13 after 2h23m, while the server was swapping. A copy
    on C: cannot go away mid-run. Falls back to the original if the copy fails.
    """
    try:
        if not _on_network_drive(path) or not path.is_file():
            return path
        local_dir = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "savesheet" / "bin"
        local = local_dir / path.name
        src = path.stat()
        if local.is_file():
            dst = local.stat()
            if dst.st_size == src.st_size and int(dst.st_mtime) == int(src.st_mtime):
                return local
        local_dir.mkdir(parents=True, exist_ok=True)
        tmp = local.with_name(local.name + ".tmp")
        shutil.copy2(path, tmp)
        os.replace(tmp, local)
        return local
    except OSError:
        return path


def resolve_path(path: str, base_dir: Path) -> Path:
    """Resolve a path that may be relative to base_dir, or absolute."""
    p = Path(path)
    if p.is_absolute():
        return p
    return base_dir / p


# Freshness is a rolling 24-hour window: a job stays green for a full day after its last
# run, whatever the calendar date (George, 2026-11-12). An earlier version keyed on the
# local calendar date, but that flipped a backup to non-green at midnight - a run at 23:00
# looked stale by 00:30, half an hour later. Closing and reopening the app does not make a
# recent backup stale either; the colour comes from the timestamp, not the session.


def _coerce(dt: datetime | str | None) -> datetime | None:
    """Normalise a history timestamp to an aware datetime, or None if unusable."""
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def age_seconds(dt: datetime | str | None) -> int | None:
    """Seconds since dt, or None if there is no usable timestamp."""
    parsed = _coerce(dt)
    if parsed is None:
        return None
    return int((datetime.now(timezone.utc) - parsed).total_seconds())


def is_fresh(dt: datetime | str | None) -> bool:
    """True if dt is within the last 24 hours - a rolling window, not the calendar date.
    A future timestamp (clock skew) counts as fresh: it has effectively just happened."""
    seconds = age_seconds(dt)
    if seconds is None:
        return False
    return seconds < 24 * 3600


def relative_time(dt: datetime | str | None) -> str:
    """Return a human-readable relative time string like '2 days ago'."""
    if dt is None:
        return "Never"
    seconds = age_seconds(dt)
    if seconds is None:
        return "Unknown"
    if seconds < 3600:
        return "Just now"  # under an hour, or a future timestamp (clock skew)
    hours = seconds // 3600
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 30:
        return f"{days}d ago"
    months = days // 30
    # Gate on days, not months: 360-364 days gives months == 12, which fell through to
    # years == 360 // 365 == 0 and reported "0y ago".
    if days < 365:
        return f"{months}mo ago"
    years = days // 365
    return f"{years}y ago"


def sanitize_job_id(name: str) -> str:
    """Convert a display name into a valid YAML key / job ID."""
    return name.lower().replace(" ", "_").replace("'", "").replace("(", "").replace(")", "").replace("-", "_")
