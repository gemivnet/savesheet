from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path


def resolve_path(path: str, base_dir: Path) -> Path:
    """Resolve a path that may be relative to base_dir, or absolute."""
    p = Path(path)
    if p.is_absolute():
        return p
    return base_dir / p


def relative_time(dt: datetime | str | None) -> str:
    """Return a human-readable relative time string like '2 days ago'."""
    if dt is None:
        return "Never"
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except ValueError:
            return "Unknown"
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = now - dt
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "Just now"
    if seconds < 60:
        return "Just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
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
