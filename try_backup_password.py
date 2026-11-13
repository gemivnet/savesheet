#!/usr/bin/env python
"""Interactively try passwords against an existing encrypted iOS backup.

For the one case the backup job can't solve on its own: a device that already
had a backup password set some other time (iTunes, Finder, a work MDM policy),
which nobody wrote down. The backup on disk is real but can't be opened, and
the job needs that old password - as IOS_BACKUP_OLD_PASSWORD - before it can
change the device over to savesheet's shared password.

This script lets you sit and type candidate passwords until one opens the
backup. It checks the same way the backup job does: it unwraps the backup's
keybag and manifest key (a few seconds of key derivation, no device required),
which is exactly the step a wrong password fails at. Nothing is ever sent
anywhere and no password is printed or written to a log - you type it hidden,
and when one works the script offers to save it to .env for you.

Defaults to George's Work Phone backup. Run it with the app's own Python so
pyiosbackup is available, from the savesheet folder, e.g. on Windows:

    .venv\\Scripts\\python.exe try_backup_password.py

Options:
    --dest PATH       Backup folder to open (default: the work phone's).
    --udid UDID       Which <UDID> subfolder to test, if --dest isn't bound
                      or holds more than one.
    --env-file PATH   .env to save a found password into (default: .env here).
    --from-file PATH  Try each line of this file first (a wordlist), then drop
                      into the interactive prompt. Blank lines and #comments
                      are skipped.
    --no-save         Don't offer to write the found password to .env.
"""
from __future__ import annotations

import argparse
import getpass
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

# Import the real verifier so this agrees exactly with what the backup job checks.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backup_manager.ios_backup import (  # noqa: E402
    BINDING_FILE,
    OLD_PASSWORD_VAR,
    JobError,
    read_binding,
    verify_password,
)

# The device this is almost always wanted for. Override with --dest / --udid.
DEFAULT_DEST = r"G:\Files\Backups\iOS Full\George Work Phone"


def find_device_dir(dest: Path, udid: str | None) -> Path:
    """Locate the <UDID> backup folder inside dest that holds a Manifest.plist."""
    if udid:
        d = dest / udid
        if not (d / "Manifest.plist").is_file():
            raise JobError(f"No backup (Manifest.plist) in {d}.")
        return d

    binding = None
    try:
        binding = read_binding(dest)
    except JobError:
        binding = None  # a broken binding shouldn't stop a recovery attempt
    if binding and (dest / binding["udid"] / "Manifest.plist").is_file():
        return dest / binding["udid"]

    # No usable binding: find the backup folder(s) ourselves. Ignore the
    # .old-password-* folders the job parks aside - those are the unopenable ones.
    candidates = [
        p for p in dest.iterdir()
        if p.is_dir() and (p / "Manifest.plist").is_file()
        and ".old-password-" not in p.name
    ] if dest.is_dir() else []
    if not candidates:
        raise JobError(
            f"No backup found under {dest}.\n"
            f"  Expected a <UDID> subfolder with a Manifest.plist in it. Point --dest at the\n"
            f"  right folder, or give --udid."
        )
    if len(candidates) > 1:
        names = "\n".join(f"    {p.name}" for p in candidates)
        raise JobError(f"{dest} holds more than one backup; pick one with --udid:\n{names}")
    return candidates[0]


def try_password(device_dir: Path, password: str) -> bool:
    try:
        verify_password(device_dir, password)
        return True
    except JobError:
        return False
    except Exception as e:  # malformed input shouldn't end the session
        print(f"  (couldn't test that one: {type(e).__name__}: {e})")
        return False


def _atomic_write(path: Path, content: str) -> None:
    """Write content to path without the in-place truncate an SMB share can deny.

    The crash this is here for: over a mapped network drive, opening the file
    'w' (truncate-then-write) was refused with PermissionError. Writing a sibling
    temp file and renaming it over the target needs write on the *directory*, not
    the existing file, which the authenticated share allows; and if the target
    still carries a read-only attribute, clear it and write in place.
    """
    directory = path.parent if str(path.parent) else Path(".")
    try:
        fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=".env-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
            os.replace(tmp, path)
            return
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except PermissionError:
        if path.exists():
            try:
                os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            except OSError:
                pass
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)


def save_old_password(env_file: Path, password: str) -> None:
    """Write password to env_file as IOS_BACKUP_OLD_PASSWORD, replacing any existing/commented line."""
    # Quote only if needed, matching how the job's reader strips a matching outer pair.
    needs_quote = password != password.strip() or "#" in password or " " in password
    if needs_quote:
        if '"' not in password:
            value = f'"{password}"'
        elif "'" not in password:
            value = f"'{password}'"
        else:
            raise JobError(
                "That password contains both kinds of quote, so it can't be written to .env "
                "safely. Add the line by hand instead."
            )
    else:
        value = password
    new_line = f"{OLD_PASSWORD_VAR}={value}"

    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.is_file() else []
    pat = re.compile(rf"^\s*#?\s*{re.escape(OLD_PASSWORD_VAR)}\s*=")
    replaced = False
    for i, line in enumerate(lines):
        if pat.match(line):
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        lines.append(new_line)
    _atomic_write(env_file, "\n".join(lines) + "\n")
    where = "updated" if replaced else "added to"
    print(f"\n[OK] {where} {env_file}: {OLD_PASSWORD_VAR} is set (value not shown).")
    print("     Now run \"George's Work Phone (Full)\" in the app: it will use this once to")
    print(f"     change the phone over to the shared password, then you can delete the {OLD_PASSWORD_VAR} line.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dest", default=DEFAULT_DEST, help="Backup folder to open.")
    ap.add_argument("--udid", default="", help="Which <UDID> subfolder to test.")
    # Not .resolve(): on Windows that turns a mapped drive (N:\) into its UNC
    # target (\\server\...), and a bare-UNC write can land on a read-only guest
    # session even when the mapped drive is authenticated read-write.
    ap.add_argument("--env-file", default=str(Path(os.path.abspath(__file__)).parent / ".env"),
                    help="Where to save a found password.")
    ap.add_argument("--from-file", default="", help="Wordlist to try before prompting.")
    ap.add_argument("--no-save", action="store_true", help="Don't offer to save to .env.")
    args = ap.parse_args(argv)

    dest = Path(args.dest)
    try:
        device_dir = find_device_dir(dest, args.udid or None)
    except JobError as e:
        print(f"[ERROR] {e}")
        return 1

    binding_name = ""
    bpath = dest / BINDING_FILE
    if bpath.is_file():
        import json
        try:
            binding_name = json.loads(bpath.read_text(encoding="utf-8")).get("device_name", "")
        except Exception:
            pass

    print("Trying passwords against the encrypted backup at:")
    print(f"  {device_dir}" + (f'   ("{binding_name}")' if binding_name else ""))
    print("Each guess takes a second or two to test. Nothing is sent anywhere.")
    print("Press Enter on an empty line (or Ctrl+C) to stop.\n")

    tried = 0

    def report_hit(pw: str) -> int:
        print(f"\n[FOUND] That password opens the backup. (after {tried} "
              f"attempt{'s' if tried != 1 else ''})")
        if not args.no_save:
            ans = input(f"Save it to {args.env_file} as {OLD_PASSWORD_VAR}? [Y/n] ").strip().lower()
            if ans in ("", "y", "yes"):
                try:
                    save_old_password(Path(args.env_file), pw)
                except (JobError, OSError) as e:
                    print(f"[WARN] Couldn't save to {args.env_file}: {e}")
                    print("       The password is correct, though. Re-run this script, type that same")
                    print(f"       password, and it will try saving again - or add {OLD_PASSWORD_VAR}=... by hand.")
            else:
                print(f"Not saved. Add it yourself as {OLD_PASSWORD_VAR}=... in {args.env_file} when ready.")
        return 0

    # Wordlist pass first, if given.
    if args.from_file:
        wl = Path(args.from_file)
        if not wl.is_file():
            print(f"[WARN] --from-file {wl} not found; skipping to the prompt.")
        else:
            print(f"Trying candidates from {wl} ...")
            for raw in wl.read_text(encoding="utf-8", errors="replace").splitlines():
                cand = raw.rstrip("\n")
                if not cand or cand.lstrip().startswith("#"):
                    continue
                tried += 1
                if try_password(device_dir, cand):
                    return report_hit(cand)
            print(f"None of the {tried} candidate(s) in the file worked. Over to you.\n")

    # Interactive loop.
    while True:
        try:
            pw = getpass.getpass("password> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if pw == "":
            break
        tried += 1
        if try_password(device_dir, pw):
            return report_hit(pw)
        print("  nope - not that one.")

    print(f"\nStopped after {tried} attempt{'s' if tried != 1 else ''}; none opened the backup.")
    print("If the old password really is lost, the other route is on the phone itself:")
    print("  Settings > General > Transfer or Reset > Reset > Reset All Settings")
    print("  (keeps photos, apps and data; forgets Wi-Fi passwords and the backup password).")
    print("Then run the full-backup job and it will set the shared password fresh.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
