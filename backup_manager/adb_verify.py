"""Second pass after an adbsync pull: re-pull every local copy whose size differs from the
phone's.

BetterADBSync decides what to copy by modification time alone (source mtime > destination
mtime) and never compares sizes. A pull that dies part-way - cable bump, phone locks, adb
drops - leaves a short file whose mtime is "now", newer than the phone's, so every later run
skips it and the photo stays truncated for good. That is how four Pixel photos from
2026-02-06 sat in G:\\Files\\Backups\\George at 0-4 MB of their real 4-5 MB until Immich's
copies were noticed (2026-11-12).

This pass lists every file on the phone with its size and mtime (one `find ... -exec stat`)
and looks for exactly that signature: a local copy that is SMALLER than the phone's and
NEWER than it. Only then is it pulled again (to a temporary name, then moved over the short
copy with the phone's mtime). Any other difference is left alone on purpose: a larger or
older local copy can be the better one (a phone restored from a recompressed cloud copy),
and adbsync already replaces local copies the phone has a newer version of. Excludes use
adbsync's own rule: each pattern is joined onto the destination and matched with fnmatch
against the file and every folder above it. Files missing locally are adbsync's business.

Exits 1 - job red - if a re-pull fails, or if the phone listing comes back empty or broken,
so a pass that checked nothing can't look green.

    python -m backup_manager.adb_verify --adb ADB --source /storage/emulated/0/ --dest G:\\Files\\Backups\\George
        [--exclude PATTERN ...] [--adb-encoding utf-8] [--dry-run]
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import posixpath
import shlex
import subprocess
import sys
from pathlib import Path


def remote_files(adb: str, source: str, encoding: str) -> dict[str, tuple[int, int]]:
    """{remote path: (size, mtime)} for every regular file under source."""
    # stat's %n is last so a name with spaces survives a split(" ", 2). Errors from folders
    # Android won't let adb read (Android/data, Android/obb) go to stderr and are ignored:
    # adbsync could not read those either.
    # The trailing slash makes find follow a symlinked starting point (/sdcard -> ...).
    # Not `find -exec stat {} +`: toybox's find packs that batch past the kernel limit on a
    # big tree ("exec stat: Argument list too long", the Retroid on 2026-11-12, cut off at
    # 14,391 files). xargs with a fixed -n keeps every stat call small.
    cmd = (f"find {shlex.quote(source.rstrip('/') + '/')} -type f -print0 2>/dev/null"
           f" | xargs -0 -n 100 stat -c '%s %Y %n'")
    proc = subprocess.run([adb, "shell", cmd], capture_output=True)
    out = proc.stdout.decode(encoding, errors="replace")
    files = {}
    for line in out.splitlines():
        parts = line.rstrip("\r").split(" ", 2)
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].lstrip("-").isdigit():
            continue
        files[parts[2]] = (int(parts[0]), int(parts[1]))
    # xargs exits 123 when some stat failed (a file vanished mid-listing, an unreadable
    # entry), which is normal on a live device; anything else (126/127 a broken command,
    # 255 adb lost the device) means the list can't be trusted.
    if proc.returncode not in (0, 1, 123) or not files:
        raise RuntimeError(f"adb shell find exit {proc.returncode}, {len(files)} files listed: "
                           f"{proc.stderr.decode(encoding, errors='replace').strip()[:300]}")
    return files


def local_root(source: str, dest: str) -> Path:
    """Where adbsync puts source's contents: into dest itself when source ends in '/',
    otherwise into dest/<source's last component> - unless dest did not exist on the first
    run, in which case adbsync used dest itself."""
    nested = Path(dest) / posixpath.basename(source.rstrip("/"))
    if source.endswith("/") or not nested.is_dir():
        return Path(dest)
    return nested


def excluded(local: Path, root: Path, patterns: list[str]) -> bool:
    """adbsync's rule: a node is excluded when its path matches a pattern joined onto the
    destination, and excluding a folder excludes everything in it."""
    node = Path(os.path.normpath(str(local)))
    top = Path(os.path.normpath(str(root)))
    while True:
        if any(fnmatch.fnmatch(str(node), p) for p in patterns):
            return True
        if node == top or node.parent == node:
            return False
        node = node.parent


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--adb", required=True)
    ap.add_argument("--source", required=True)
    ap.add_argument("--dest", required=True)
    ap.add_argument("--exclude", action="append", default=[])
    ap.add_argument("--adb-encoding", default="utf-8")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    try:
        remote = remote_files(a.adb, a.source, a.adb_encoding)
    except (OSError, RuntimeError) as e:
        print(f"[adb-verify] ERROR: could not list the phone: {e}")
        return 1
    root = local_root(a.source, a.dest)
    patterns = [os.path.normpath(os.path.join(str(root), p)) for p in a.exclude]
    prefix = a.source.rstrip("/") + "/"
    # A .part left by a run that was stopped would otherwise ride every mirror forever.
    for dirpath, _, names in os.walk(root):
        for n in names:
            if n.endswith(".adbverify.part"):
                try:
                    os.remove(os.path.join(dirpath, n))
                except OSError:
                    pass
    checked = other = 0
    bad: list[tuple[str, Path, int, int, int]] = []
    for rpath, (size, mtime) in remote.items():
        if not rpath.startswith(prefix):
            continue
        rel = rpath[len(prefix):].lstrip("/")
        local = root.joinpath(*rel.split("/"))
        try:
            st = local.stat()
        except OSError:
            continue  # not copied (new, excluded, or a name the local disk can't hold)
        if excluded(local, root, patterns):
            continue
        checked += 1
        if st.st_size == size:
            continue
        # The cut-short signature: smaller here, and dated after the phone's copy (adbsync
        # sets the phone's mtime only once a pull succeeds). Anything else is left alone.
        if st.st_size < size and st.st_mtime > mtime + 2:
            bad.append((rpath, local, size, st.st_size, mtime))
        else:
            other += 1

    print(f"[adb-verify] {checked} copied files compared with the phone; {len(bad)} cut short"
          + (f"; {other} differ in size but are not cut short (left alone)" if other else ""))
    failed = 0
    for rpath, local, size, lsize, mtime in bad:
        print(f"[adb-verify] {local}: {lsize} bytes here, {size} on the phone - pulling again")
        if a.dry_run:
            continue
        part = local.with_name(local.name + ".adbverify.part")
        try:
            r = subprocess.run([a.adb, "pull", rpath, str(part)], capture_output=True)
            got = part.stat().st_size if part.exists() else -1
            # adb pull exits 0 only for a complete copy. The size can differ from the listing
            # if the file changed on the phone meanwhile; it must at least beat the short one.
            if r.returncode != 0 or got <= lsize:
                raise OSError(f"adb pull exit {r.returncode}, got {got} bytes (phone listed {size}): "
                              f"{(r.stderr or r.stdout).decode(a.adb_encoding, errors='replace').strip()[:200]}")
            os.replace(part, local)
            os.utime(local, (mtime, mtime))
        except OSError as e:
            failed += 1
            print(f"[adb-verify] FAILED {local}: {e}")
            try:
                part.unlink()
            except OSError:
                pass
    if bad and not a.dry_run:
        print(f"[adb-verify] repaired {len(bad) - failed}, failed {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
