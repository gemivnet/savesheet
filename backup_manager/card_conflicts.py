"""After a FreeFileSync card import: save card files whose name is already taken on G: by
a different file.

The camera batch (Sony RX1000.ffs_batch) only ever copies files that are missing on G:
(`<Differences LeftOnly="right" LeftNewer="none" RightNewer="none" .../>`). That is right
for never overwriting a backup. But a camera that restarts its numbering after a format
writes a NEW C0001.MP4, which FFS classes as "left newer": no action, exit 0, job green.
Format the card again and that clip is gone. This pass finds every card file whose
destination path exists with different contents and copies it in beside the old one,
under the same name plus its own timestamp, e.g. "C0001 (2026-11-12 143005).MP4".

Different = a different size, or the same size with modified times more than 2 s apart
and different contents (compared by hashing the first and last MiB - enough to tell two
photos or clips apart, and cheap, since after a DST change FAT reports every card file an
hour off). Same-size photos are common: the NAS copy already holds different JPGs of
identical size. Nothing is ever overwritten: if the timestamped name is taken by a
different file, a counter is added. Re-runs are idempotent. Exits 1 if any copy fails.

    python -m backup_manager.card_conflicts "Sony RX1000.ffs_batch" [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

# The batch's own exclude list (FFS filter syntax); these are card housekeeping, not media.
SKIP_DIRS = {"system volume information", "$recycle.bin", "recovery"}
SKIP_FILES = {"thumbs.db"}


def copies_new_only(batch: Path) -> bool:
    """True for a batch that never updates an existing file (LeftNewer="none") - the only
    kind where a changed same-name file can be silently left behind."""
    d = ET.parse(batch).getroot().find("./Synchronize/Differences")
    return d is not None and d.get("LeftNewer") == "none"


def folder_pairs(batch: Path) -> list[tuple[Path, Path]]:
    root = ET.parse(batch).getroot()
    pairs = []
    for pair in root.iter("Pair"):
        left, right = pair.findtext("Left"), pair.findtext("Right")
        if left and right:
            pairs.append((Path(left), Path(right)))
    return pairs


MIB = 1 << 20


def fingerprint(path: Path) -> str:
    """SHA-1 of the size, the first MiB and the last MiB."""
    size = path.stat().st_size
    h = hashlib.sha1(str(size).encode())
    with open(path, "rb") as fh:
        h.update(fh.read(MIB))
        if size > 2 * MIB:
            fh.seek(size - MIB)
            h.update(fh.read(MIB))
    return h.hexdigest()


def same_file(a: Path, b: Path) -> bool:
    sa, sb = a.stat(), b.stat()
    if sa.st_size != sb.st_size:
        return False
    if abs(sa.st_mtime - sb.st_mtime) <= 2:   # FAT keeps 2-second times
        return True
    return fingerprint(a) == fingerprint(b)


def target_for(dst: Path, src: Path) -> Path | None:
    """The timestamped name to save src under; None if src is already saved there."""
    when = time.strftime("%Y-%m-%d %H%M%S", time.localtime(src.stat().st_mtime))
    for n in range(1, 100):
        extra = "" if n == 1 else f" {n}"
        cand = dst.with_name(f"{dst.stem} ({when}{extra}){dst.suffix}")
        if not cand.exists():
            return cand
        if same_file(src, cand):
            return None
    raise OSError(f"no free name for {src.name} beside {dst}")


STALE_PART = re.compile(r" \(\d{4}-\d\d-\d\d \d{6}( \d+)?\)\.[^.]+\.part$")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("batch", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    if not copies_new_only(a.batch):
        print(f"[card-conflicts] {a.batch.name} updates changed files itself; nothing to check")
        return 0
    checked = saved = failed = 0
    for _, right in folder_pairs(a.batch):
        # A copy cut off by Stop leaves a .part that every mirror would keep forever.
        for dirpath, _, names in os.walk(right):
            for n in names:
                if STALE_PART.search(n):
                    try:
                        os.remove(os.path.join(dirpath, n))
                    except OSError:
                        pass
    for left, right in folder_pairs(a.batch):
        if not left.is_dir():
            print(f"[card-conflicts] {left} not found; skipped")
            continue
        for dirpath, dirs, files in os.walk(left):
            dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS and not d.lower().startswith("recycle")]
            for name in files:
                if name.lower() in SKIP_FILES:
                    continue
                src = Path(dirpath) / name
                dst = right / src.relative_to(left)
                if not dst.exists():
                    continue  # FFS copies new names itself
                checked += 1
                try:
                    if same_file(src, dst):
                        continue
                    target = target_for(dst, src)
                    if target is None:
                        continue  # saved by an earlier run
                    print(f"[card-conflicts] {src} is a different file from {dst}; saving as {target.name}")
                    if a.dry_run:
                        continue
                    part = target.with_name(target.name + ".part")
                    shutil.copy2(src, part)
                    if part.stat().st_size != src.stat().st_size:
                        raise OSError("copy came out the wrong size")
                    os.replace(part, target)
                    saved += 1
                except OSError as e:
                    failed += 1
                    print(f"[card-conflicts] FAILED {src}: {e}")
    print(f"[card-conflicts] {checked} card files already on G: checked; "
          f"{saved} same-name new files saved; {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
