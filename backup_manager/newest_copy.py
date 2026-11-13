"""Copy only the newest file matching a pattern - for sources that hold a rolling set of
large full backups, where copying them all would be wasteful.

Made for Home Assistant (2026-11-12): /nas/appdata/homeassistant holds HA's last 7 daily
automatic backups at ~7 GB each. Each one is a complete restore point, so a mirror only
needs the newest. Copying all seven every run would push ~50 GB through G: and, since every
mirror is an rclone `copy`, keep every one of them forever on D:, Drive and Glacier.

The newest match is copied to DEST (via a .part file, size-checked) unless it is already
there. Older matches in DEST beyond the newest --keep are deleted, from G: only: the mirrors
keep what they already have. Fails (exit 1) when the newest source file is older than
--max-age-days, because that means the backups themselves have stopped.

    python -m backup_manager.newest_copy --src \\\\server\\nas\\appdata\\homeassistant
        --glob "automatic_backup_*.tar" --dest "G:\\Files\\Server Backup\\homeassistant"
        [--keep 2] [--max-age-days 3] [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", required=True, type=Path)
    ap.add_argument("--glob", required=True)
    ap.add_argument("--dest", required=True, type=Path)
    ap.add_argument("--keep", type=int, default=2)
    ap.add_argument("--max-age-days", type=float, default=3)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    try:
        found = sorted((p for p in a.src.glob(a.glob) if p.is_file()), key=lambda p: p.stat().st_mtime)
    except OSError as e:
        print(f"[newest-copy] ERROR: cannot list {a.src}: {e}")
        return 1
    if not found:
        print(f"[newest-copy] ERROR: nothing matching {a.glob} in {a.src}")
        return 1
    # HA takes ~10 minutes to write a backup; a file modified in the last 20 is probably
    # still being written, so the one before it is the newest complete backup.
    if len(found) > 1 and time.time() - found[-1].stat().st_mtime < 20 * 60:
        print(f"[newest-copy] {found[-1].name} is still being written; using the one before it")
        found = found[:-1]
    newest = found[-1]
    st = newest.stat()
    age_days = (time.time() - st.st_mtime) / 86400
    print(f"[newest-copy] newest: {newest.name} ({st.st_size / 1e9:.1f} GB, {age_days:.1f} days old)")

    dest = a.dest / newest.name
    for stale in (a.dest.glob(a.glob + ".part") if a.dest.is_dir() else []):   # cut off by Stop
        if not a.dry_run:
            stale.unlink()
    if dest.exists() and dest.stat().st_size == st.st_size:
        print(f"[newest-copy] already in {a.dest}")
    elif a.dry_run:
        print(f"[newest-copy] would copy to {dest}")
    else:
        a.dest.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        # 7 GB over SMB: a dropped connection ("semaphore timeout") is common, so retry.
        for attempt in range(1, 4):
            try:
                shutil.copy2(newest, part)
                if part.stat().st_size != st.st_size:
                    raise OSError(f"copied {part.stat().st_size} of {st.st_size} bytes")
                os.replace(part, dest)
                break
            except OSError as e:
                print(f"[newest-copy] attempt {attempt} failed copying {newest.name}: {e}")
                try:
                    part.unlink()
                except OSError:
                    pass
                if attempt == 3:
                    print("[newest-copy] FAILED after 3 attempts")
                    return 1
                time.sleep(30 * attempt)
        print(f"[newest-copy] copied to {dest}")

    kept = sorted((p for p in a.dest.glob(a.glob) if p.is_file()), key=lambda p: p.stat().st_mtime) \
        if a.dest.is_dir() else []
    for old in kept[:-a.keep] if a.keep > 0 else []:
        print(f"[newest-copy] {'would remove' if a.dry_run else 'removing'} older copy {old.name}")
        if not a.dry_run:
            old.unlink()

    if age_days > a.max_age_days:
        print(f"[newest-copy] FAILED: the newest backup is {age_days:.1f} days old "
              f"(limit {a.max_age_days:g}); the source has stopped producing new ones")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
