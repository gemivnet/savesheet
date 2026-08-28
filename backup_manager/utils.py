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


# A backup taken within this window is still the current state of the device, so
# the UI calls it "Just now" and keeps its status colour. Closing and reopening the app
# does not make a 13-minute-old backup stale, which is what the old session-scoped
# colouring implied.
FRESH_WINDOW_SECONDS = 2 * 60 * 60


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


def is_fresh(dt: datetime | str | None, window_seconds: int = FRESH_WINDOW_SECONDS) -> bool:
    """True if dt is recent enough to still count as the current state."""
    seconds = age_seconds(dt)
    return seconds is not None and 0 <= seconds < window_seconds


def relative_time(dt: datetime | str | None) -> str:
    """Return a human-readable relative time string like '2 days ago'."""
    if dt is None:
        return "Never"
    if _coerce(dt) is None:
        return "Unknown"
    seconds = age_seconds(dt)
    assert seconds is not None
    # Covers clock skew (negative) as well as genuinely recent runs.
    if seconds < FRESH_WINDOW_SECONDS:
        return "Just now"
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
