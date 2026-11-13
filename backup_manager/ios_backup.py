"""Full (Finder/iTunes-style) backups of iPhones and iPads, via pymobiledevice3.

The `mtp` job type only reaches what Windows exposes over MTP, which on an Apple
device is the camera roll and nothing else. This is the other kind: the device's
own backup - messages, app data, settings, home screen, and (because it is
encrypted) Health, the keychain, Wi-Fi networks and call history. It is the one
that can be restored onto a replacement device.

Run by the executor as `python -m backup_manager.ios_backup backup ...` so that
its output streams into the log panel like every other job. Three things here
are deliberate and easy to undo by accident:

  * Each job is bound to one device. Windows calls every iPhone "Apple iPhone",
    so nothing but the UDID tells Shelby's from George's. The first run writes
    the UDID to savesheet-device.json in the destination; after that the job
    refuses any other device, and a device already bound to a neighbouring
    folder is never offered to a new job. Without this, the wrong cable in the
    wrong port silently files one person's phone under another's name.
  * Backups are encrypted, and the password is checked against every backup
    after it is written. The device does the encrypting with whatever password
    it was last given - possibly years ago, in iTunes, by someone who has since
    forgotten it. A backup like that completes without complaint and can never
    be opened. Unwrapping its keybag with the password in .env is the only
    proof that this one can.
  * The password never appears on a command line. The executor logs every
    command it runs, to the panel and to logs/, so it is read from the
    environment or from .env instead.

Backups are incremental: the device sends what changed since the backup already
in the destination, so only the first run is slow.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import plistlib
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

BINDING_FILE = "savesheet-device.json"
PASSWORD_VAR = "IOS_BACKUP_PASSWORD"
# A device's previous backup password, when it had one before these jobs existed.
# Used once, to change that device over to PASSWORD_VAR; it can be removed afterwards.
OLD_PASSWORD_VAR = "IOS_BACKUP_OLD_PASSWORD"
# How long to wait for someone to unlock the device and tap Trust.
PAIR_WAIT_SECONDS = 180
APPLE_SERVICE = "Apple Mobile Device Service"
APPLE_WINGET_ID = "Apple.AppleMobileDeviceSupport"


def say(msg: str = "") -> None:
    print(msg, flush=True)


class JobError(Exception):
    """A failure with a message fit to show as-is; anything else is a bug and gets a traceback."""


# --- small helpers ---------------------------------------------------------------


def _norm(udid: str | None) -> str:
    """usbmuxd and lockdownd disagree about the dash in newer UDIDs, and about case."""
    return (udid or "").replace("-", "").casefold()


def read_password(env_file: Path | None, var: str = PASSWORD_VAR) -> str:
    """A backup password, from the environment or a KEY=VALUE .env file. Never logged."""
    value = os.environ.get(var, "")
    if value:
        return value
    if env_file is None or not env_file.is_file():
        return ""
    for raw in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key.strip() == var:
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            return val
    return ""


def read_binding(dest: Path) -> dict[str, Any] | None:
    path = dest / BINDING_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        # Guessing here would defeat the point of the file.
        raise JobError(f"{path} exists but cannot be read ({e}). Fix or delete it; "
                       f"deleting it lets the job bind to whichever device is connected next.")
    if not isinstance(data, dict) or not data.get("udid"):
        raise JobError(f"{path} has no 'udid'. Fix or delete it.")
    return data


def write_binding(dest: Path, info: dict[str, Any], label: str) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    data = {
        "udid": info["udid"],
        "device_name": info.get("name") or "",
        "device_class": info.get("device_class") or "",
        "product_type": info.get("product_type") or "",
        "job": label,
        "bound_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    (dest / BINDING_FILE).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def sibling_bindings(dest: Path) -> dict[str, Path]:
    """UDIDs already claimed by other backup folders next to this one -> that folder."""
    claimed: dict[str, Path] = {}
    try:
        candidates = list(dest.parent.glob(f"*/{BINDING_FILE}"))
    except OSError:
        return claimed
    for path in candidates:
        if path.parent == dest:
            continue
        try:
            udid = json.loads(path.read_text(encoding="utf-8")).get("udid")
        except (OSError, ValueError, AttributeError):
            continue
        if udid:
            claimed[_norm(udid)] = path.parent
    return claimed


def describe(info: dict[str, Any]) -> str:
    name = info.get("name") or "(name unavailable until trusted)"
    bits = [b for b in (info.get("product_type"), f"iOS {info['os']}" if info.get("os") else "") if b]
    return f"\"{name}\" [{', '.join(bits) or 'unknown model'}] {info['udid']}"


def choose_device(
    devices: list[dict[str, Any]],
    binding: dict[str, Any] | None,
    claimed: dict[str, Path],
    device_class: str,
    label: str,
) -> tuple[dict[str, Any], bool]:
    """Pick the device this job should back up. Returns (device, is_new_binding).

    Pure on purpose - this is the logic that decides whose phone goes in whose
    folder, so it can be exercised without a device attached.
    """
    listing = "\n".join(f"    {describe(d)}" for d in devices) or "    (none)"

    if binding is not None:
        for d in devices:
            if _norm(d["udid"]) == _norm(binding["udid"]):
                return d, False
        bound_name = binding.get("device_name") or binding["udid"]
        raise JobError(
            f"{label} is bound to \"{bound_name}\" ({binding['udid']}), which is not connected.\n"
            f"  Connected over USB:\n{listing}\n"
            f"  Plug that device in and unlock it. If it has been replaced, delete "
            f"{BINDING_FILE} from the destination to bind this job to the new one."
        )

    if not devices:
        raise JobError("No iPhone or iPad is connected over USB. Plug it in, unlock it, and run again.")

    free = [d for d in devices if _norm(d["udid"]) not in claimed]
    if device_class:
        # A device that would not say what it is (locked, never trusted) stays in:
        # dropping it would report "nothing connected" with a phone on the cable.
        free = [d for d in free if not d.get("device_class")
                or d["device_class"].casefold() == device_class.casefold()]
    if not free:
        owned = "\n".join(
            f"    {describe(d)} -> already backs up to {claimed[_norm(d['udid'])]}"
            for d in devices if _norm(d["udid"]) in claimed
        )
        raise JobError(
            f"{label} has not been bound to a device yet, and nothing connected fits it.\n"
            f"  Connected over USB:\n{listing}\n"
            + (f"  Belonging to other jobs:\n{owned}\n" if owned else "")
            + (f"  This job expects an {device_class}." if device_class else "")
        )
    if len(free) > 1:
        raise JobError(
            f"{label} has not been bound to a device yet and {len(free)} candidates are connected:\n"
            + "\n".join(f"    {describe(d)}" for d in free)
            + "\n  Leave only the one this job is for plugged in, and run again."
        )
    return free[0], True


# --- pymobiledevice3 -------------------------------------------------------------


def _pmd():
    """Import pymobiledevice3 late, so `--help` and the pure logic work without it."""
    try:
        import pymobiledevice3  # noqa: F401
        from pymobiledevice3 import exceptions, lockdown, usbmux
        from pymobiledevice3.services.mobilebackup2 import Mobilebackup2Service
    except ImportError as e:
        raise JobError(
            f"pymobiledevice3 is not installed in this environment ({e}).\n"
            f"  Run `uv sync` in the savesheet folder, then run the job again."
        )
    return exceptions, lockdown, usbmux, Mobilebackup2Service


def _run_quiet(cmd: list[str], timeout: int) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except FileNotFoundError:
        return 127, f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, f"{cmd[0]} timed out after {timeout}s"


def ensure_apple_service(install: bool) -> bool:
    """Windows only: get Apple's usbmuxd (the 'Apple Mobile Device Service') running.

    MTP needs none of Apple's software, so a machine that has only ever run the
    photo jobs may not have it. Returns True if a retry is worth making. Only a
    backup run installs it; listing and checking devices just report its absence.
    """
    if os.name != "nt":
        return False
    code, out = _run_quiet(["sc", "query", APPLE_SERVICE], 20)
    if code == 0 and "RUNNING" in out:
        return True
    if code == 0:
        say(f"[INFO] {APPLE_SERVICE} is installed but not running; starting it...")
        code, out = _run_quiet(["sc", "start", APPLE_SERVICE], 30)
        if code != 0:
            say(f"[WARN] Could not start it (needs an elevated prompt?): {out.strip().splitlines()[-1] if out.strip() else code}")
            return False
        time.sleep(4)
        return True
    if not install:
        return False
    say(f"[INFO] {APPLE_SERVICE} is not installed. It is what lets a PC talk to an iPhone beyond")
    say(f"[INFO] photos. Installing it with winget ({APPLE_WINGET_ID}) - approve the Windows prompt if one appears...")
    code, out = _run_quiet(
        ["winget", "install", "--id", APPLE_WINGET_ID, "-e", "--source", "winget",
         "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"],
        900,
    )
    tail = "\n".join(out.strip().splitlines()[-6:])
    if code != 0:
        say(f"[WARN] winget install failed (exit {code}):\n{tail}")
        return False
    say("[OK] Installed. Unplug and replug the device if it is not found below.")
    time.sleep(6)
    return True


async def list_usb_devices(install_service: bool = False) -> list[dict[str, Any]]:
    """Every Apple device on USB, with whatever it will say about itself before trust."""
    exceptions, lockdown, usbmux, _ = _pmd()
    try:
        mux_devices = await usbmux.list_devices()
    except (exceptions.ConnectionFailedToUsbmuxdError, ConnectionError, OSError) as e:
        if not ensure_apple_service(install_service):
            raise JobError(
                f"Cannot reach the Apple device service (usbmuxd): {e}\n"
                f"  On Windows this is the '{APPLE_SERVICE}', installed with iTunes, the Apple "
                f"Devices app, or `winget install {APPLE_WINGET_ID}`."
            )
        try:
            mux_devices = await usbmux.list_devices()
        except (exceptions.ConnectionFailedToUsbmuxdError, ConnectionError, OSError) as e2:
            raise JobError(f"The Apple device service is still unreachable after starting it: {e2}")

    found: list[dict[str, Any]] = []
    for mux in mux_devices:
        if not mux.is_usb:
            continue  # Wi-Fi sync peers: a multi-hour backup wants the cable
        info: dict[str, Any] = {"udid": mux.serial}
        try:
            client = await lockdown.create_using_usbmux(serial=mux.serial, autopair=False)
            try:
                values = client.all_values
                info.update(
                    udid=values.get("UniqueDeviceID") or mux.serial,
                    name=values.get("DeviceName"),
                    device_class=values.get("DeviceClass"),
                    product_type=values.get("ProductType"),
                    os=values.get("ProductVersion"),
                )
            finally:
                await client.close()
        except Exception as e:  # a device that will not talk yet is still a device
            info["error"] = f"{type(e).__name__}: {e}"
        info["serial"] = mux.serial
        found.append(info)
    return found


async def connect_trusted(serial: str):
    """A paired lockdown session, waiting for the Trust prompt if this PC is new to the device."""
    exceptions, lockdown, _, _ = _pmd()
    deadline = time.monotonic() + PAIR_WAIT_SECONDS
    told = False
    while True:
        try:
            return await lockdown.create_using_usbmux(serial=serial, autopair=True, pair_timeout=PAIR_WAIT_SECONDS)
        except exceptions.UserDeniedPairingError:
            raise JobError("\"Don't Trust\" was tapped on the device. Replug it, tap Trust, and run again.")
        except (exceptions.PasswordRequiredError, exceptions.PairingDialogResponsePendingError,
                exceptions.NotPairedError) as e:
            if time.monotonic() >= deadline:
                raise JobError(
                    f"The device never trusted this computer ({type(e).__name__}) within "
                    f"{PAIR_WAIT_SECONDS}s. Unlock it, tap Trust, enter the passcode, and run again."
                )
            if not told:
                say("[ACTION] Unlock the device and tap \"Trust\" when it asks, then enter its passcode. Waiting...")
                told = True
            await asyncio.sleep(3)


def verify_password(device_dir: Path, password: str) -> None:
    """Prove the backup in device_dir opens with `password`. Raises JobError if it does not.

    Unwraps the keybag and the manifest key only - seconds of key derivation, no
    need to read the backup's contents - which is exactly the step a wrong
    password fails at.
    """
    manifest_path = device_dir / "Manifest.plist"
    if not manifest_path.is_file() or manifest_path.stat().st_size == 0:
        raise JobError(f"{manifest_path} is missing or empty - the backup did not complete.")
    with open(manifest_path, "rb") as f:
        manifest_raw = plistlib.load(f)
    if not manifest_raw.get("IsEncrypted"):
        raise JobError(
            "The backup on disk is NOT encrypted, although the device reported encryption on. "
            "Not trusting it; run the job again."
        )
    try:
        from pyiosbackup.keybag import Keybag
        from pyiosbackup.manifest_plist import ManifestPlist
    except ImportError as e:
        raise JobError(f"Cannot verify the backup password: pyiosbackup is missing ({e}). Run `uv sync`.")
    try:
        from cryptography.hazmat.primitives.keywrap import aes_key_unwrap

        manifest = ManifestPlist.from_path(manifest_path)
        keybag = Keybag.from_manifest(manifest, password)
        # Go one step past the keybag and unwrap the key that Manifest.db - the index
        # of the whole backup - is encrypted with. ManifestKey is a little-endian
        # uint32 protection class followed by the wrapped key. A keybag can parse
        # "successfully" into no keys at all; this cannot pass by accident.
        wrapped = manifest.manifest_key
        aes_key_unwrap(keybag.get_key(int.from_bytes(wrapped[:4], "little")), wrapped[4:])
    except Exception as e:
        raise JobError(
            f"The backup was written, but the password in {PASSWORD_VAR} does NOT open it "
            f"({type(e).__name__}: {e}).\n"
            f"  This device already had a backup password, set some other time (iTunes, Finder, "
            f"another tool), and it is still using that one. Until this is fixed the backup "
            f"cannot be restored by anyone who does not know that older password.\n"
            f"  If the older password is known, add it to .env as {OLD_PASSWORD_VAR}=... and run "
            f"again: the job uses it once to change this device over to {PASSWORD_VAR}. (Do not "
            f"put it in {PASSWORD_VAR} itself - the other devices share that one.)\n"
            f"  If it is not known, clear it on the device: Settings > General > Transfer or Reset "
            f"> Reset > Reset All Settings (keeps data and apps; forgets Wi-Fi passwords, wallpaper "
            f"and the backup password), then run again."
        )


def _existing_backup_opens(device_dir: Path, password: str) -> bool | None:
    """Does the backup already on disk open with `password`? None if there is none to test."""
    manifest_path = device_dir / "Manifest.plist"
    try:
        if not manifest_path.is_file() or manifest_path.stat().st_size == 0:
            return None
        with open(manifest_path, "rb") as f:
            if not plistlib.load(f).get("IsEncrypted"):
                return None
    except Exception:
        return None
    try:
        verify_password(device_dir, password)
        return True
    except JobError:
        return False


def tree_size(path: Path) -> tuple[int, int]:
    files = total = 0
    for root, _dirs, names in os.walk(path):
        for n in names:
            try:
                total += os.path.getsize(os.path.join(root, n))
                files += 1
            except OSError:
                pass
    return files, total


def _gib(n: int) -> str:
    return f"{n / (1 << 30):.2f} GiB"


# --- commands --------------------------------------------------------------------


async def cmd_list(_args: argparse.Namespace) -> int:
    devices = await list_usb_devices()
    if not devices:
        say("No iPhone or iPad connected over USB.")
        return 0
    for d in devices:
        say(describe(d) + (f"   ({d['error']})" if d.get("error") else ""))
    return 0


async def cmd_verify(args: argparse.Namespace) -> int:
    """Guard for the photo (mtp) jobs: is the device on the cable the one this job is for?

    An unbound destination passes with a note: the photo jobs worked before any
    full backup existed, and must keep working until one has been taken.
    """
    dest = Path(args.dest)
    binding = read_binding(dest)
    if binding is None:
        say(f"[INFO] {dest} is not bound to a device yet (no full backup taken); not checking which device is connected.")
        return 0
    devices = await list_usb_devices()
    choose_device(devices, binding, {}, "", args.label)
    others = [d for d in devices if _norm(d["udid"]) != _norm(binding["udid"])]
    if others:
        # MediaDeviceCopier picks by Windows' name ("Apple iPhone"), which two
        # iPhones share - with both connected there is no telling which it reads.
        raise JobError(
            f"{args.label}: the right device is connected, but so is another Apple device:\n"
            + "\n".join(f"    {describe(d)}" for d in others)
            + "\n  Windows gives them the same name, so the photo copy could read either. Unplug the other one."
        )
    say(f"[OK] Connected device is \"{binding.get('device_name') or binding['udid']}\", as expected.")
    return 0


async def cmd_backup(args: argparse.Namespace) -> int:
    _, _, _, Mobilebackup2Service = _pmd()
    dest = Path(args.dest)
    label = args.label
    env_file = Path(args.env_file) if args.env_file else None
    password = read_password(env_file)

    if not password and not args.allow_unencrypted:
        raise JobError(
            f"No backup password: {PASSWORD_VAR} is not set in the environment"
            + (f" or in {env_file}" if env_file else "") + ".\n"
            "  Full backups here are encrypted, and this job will not take one it cannot later prove it can open."
        )

    binding = read_binding(dest)
    devices = await list_usb_devices(install_service=True)
    device, is_new = choose_device(devices, binding, sibling_bindings(dest), args.device_class, label)

    client = await connect_trusted(device["serial"])
    try:
        values = client.all_values
        info = {
            "udid": client.udid or device["udid"],
            "name": values.get("DeviceName"),
            "device_class": values.get("DeviceClass"),
            "product_type": values.get("ProductType"),
            "os": values.get("ProductVersion"),
        }
        say(f"[OK] Device: {describe(info)}")
        if args.device_class and (info["device_class"] or "").casefold() != args.device_class.casefold():
            raise JobError(f"{label} expects an {args.device_class}, but this is an {info['device_class']}.")
        if is_new:
            dest.mkdir(parents=True, exist_ok=True)
            write_binding(dest, info, label)
            say(f"[BOUND] {label} now backs up THIS device and no other: \"{info['name']}\".")
            say(f"        If that is the wrong device, cancel now and delete {dest / BINDING_FILE}")
            say(f"        and the {info['udid']} folder beside it.")
        device_dir = dest / info["udid"]

        # --- encryption ---
        encrypted = await Mobilebackup2Service(client).get_will_encrypt()
        existing_opens = _existing_backup_opens(device_dir, password) if password else None
        old_password = read_password(env_file, OLD_PASSWORD_VAR)
        if encrypted and existing_opens is False and old_password and old_password != password:
            say(f"[INFO] This device has an older backup password; changing it to {PASSWORD_VAR} using {OLD_PASSWORD_VAR}.")
            say("[ACTION] Enter the passcode on the device if it asks.")
            try:
                async with Mobilebackup2Service(client) as svc:
                    await svc.change_password(str(dest), old=old_password, new=password)
            except Exception as e:
                raise JobError(
                    f"The device refused the password change ({type(e).__name__}: {e}). "
                    f"{OLD_PASSWORD_VAR} is probably not this device's backup password. Nothing was changed."
                )
            parked = device_dir.with_name(f"{device_dir.name}.old-password-{datetime.now():%Y%m%d-%H%M%S}")
            device_dir.rename(parked)
            say(f"[OK] Password changed. The backup made with the old one was moved aside to {parked}")
            existing_opens = None
            await client.close()
            await asyncio.sleep(3)
            client = await connect_trusted(device["serial"])
        if encrypted and existing_opens is False:
            # Seen on the first night (2026-11-11): a phone that already had a backup
            # password spent seven minutes writing a backup the check then rejected.
            # Nothing has changed since, so say it now rather than after another pass.
            say("[INFO] Encryption is already on, and the backup on disk from a previous run")
            say("[INFO] does not open with the stored password - so the next one will not either.")
            verify_password(device_dir, password)
        if not encrypted and password:
            if existing_opens is False:
                # The old password was cleared on the device. What is on disk was made
                # with it; an incremental on top would mix two keys in one folder.
                parked = device_dir.with_name(f"{device_dir.name}.old-password-{datetime.now():%Y%m%d-%H%M%S}")
                device_dir.rename(parked)
                say(f"[INFO] The backup on disk was made with a different password; moved it aside to {parked}")
                say("[INFO] It is only openable with that older password. Delete it once you are sure it is not needed.")
            say("[INFO] Backup encryption is off on this device; turning it on.")
            say("[ACTION] Enter the passcode on the device when it asks.")
            async with Mobilebackup2Service(client) as svc:
                await svc.change_password(str(dest), new=password)
            encrypted = await Mobilebackup2Service(client).get_will_encrypt()
            if not encrypted:
                raise JobError(
                    "The device did not turn backup encryption on (was the passcode prompt dismissed?). "
                    "Nothing was backed up. Run again and enter the passcode when asked."
                )
            say(f"[OK] Encryption is on. The password is the one in {PASSWORD_VAR}.")
            # Start the backup on a fresh session. On the first night one of three
            # devices dropped the link ("Connection was terminated abruptly") at 0%
            # when the backup followed the password change on the same connection.
            await client.close()
            await asyncio.sleep(3)
            client = await connect_trusted(device["serial"])
        elif encrypted:
            say("[OK] Backup encryption is on.")
        else:
            say("[WARN] Taking an UNENCRYPTED backup (--allow-unencrypted): no Health data, keychain, "
                "Wi-Fi networks or call history, and everything in it is readable as-is.")

        # --- backup ---
        before_files, before_bytes = tree_size(device_dir) if device_dir.is_dir() else (0, 0)
        kind = "incremental" if Mobilebackup2Service._has_incremental_backup_metadata(device_dir) else "first (full)"
        say(f"[INFO] Starting {kind} backup into {device_dir}")
        if before_bytes:
            say(f"[INFO] Already on disk: {before_files:,} files, {_gib(before_bytes)}")
        say("[ACTION] Keep the device plugged in. If it shows a passcode prompt, enter it - the backup waits for it.")

        started = time.monotonic()
        last = {"pct": -1, "at": started}

        def on_progress(pct: float) -> None:
            # One line per whole percent, or per minute when a percent is slow
            # (the big ones are): newline-terminated, so the panel can render it.
            now = time.monotonic()
            whole = int(pct)
            if whole > last["pct"] or now - last["at"] >= 60:
                last["pct"], last["at"] = max(whole, last["pct"]), now
                mins = int((now - started) // 60)
                say(f"  {pct:5.1f}%   ({mins // 60}h{mins % 60:02d}m elapsed)")

        exceptions = _pmd()[0]
        for attempt in (1, 2):
            try:
                async with Mobilebackup2Service(client) as svc:
                    await svc.backup(full=False, backup_directory=str(dest), progress_callback=on_progress)
                break
            except exceptions.ConnectionTerminatedError:
                # Only a drop before anything was transferred is retried: that is the
                # device not being ready, not a cable pulled halfway through.
                if attempt == 2 or last["pct"] > 0:
                    raise
                say("[WARN] The device dropped the connection before the backup started; reconnecting and trying once more...")
                try:
                    await client.close()
                except Exception:
                    pass
                await asyncio.sleep(5)
                client = await connect_trusted(device["serial"])
    finally:
        try:
            await client.close()
        except Exception:
            pass

    # --- after ---
    status_path = device_dir / "Status.plist"
    state = "?"
    if status_path.is_file():
        with open(status_path, "rb") as f:
            status = plistlib.load(f)
        state = f"{status.get('SnapshotState', '?')}, {kind}, dated {status.get('Date')} UTC"
        if status.get("SnapshotState") != "finished":
            raise JobError(f"The device ended the session but the backup is not marked finished ({state}).")
    else:
        raise JobError(f"No Status.plist in {device_dir} - the backup did not complete.")

    if password and encrypted:
        say("[INFO] Checking that the backup opens with the stored password...")
        verify_password(device_dir, password)
        say("[OK] Verified: this backup can be opened with the password in .env.")

    files, total = tree_size(device_dir)
    mins = int((time.monotonic() - started) // 60)
    say(f"[OK] Backup complete: {state}")
    say(f"     {files:,} files, {_gib(total)} on disk ({_gib(total - before_bytes)} change), {mins // 60}h{mins % 60:02d}m")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m backup_manager.ios_backup", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("backup", help="Take an encrypted incremental backup of the bound device.")
    b.add_argument("--dest", required=True, help="Folder for this device's backup (a <UDID> subfolder is created in it).")
    b.add_argument("--label", default="This job", help="Job name, for messages.")
    b.add_argument("--device-class", default="", help="Only bind to this kind of device on first run: iPhone or iPad.")
    b.add_argument("--env-file", default="", help=f"File to read {PASSWORD_VAR} from if it is not in the environment.")
    b.add_argument("--allow-unencrypted", action="store_true", help="Back up without a password. Not recommended.")
    b.set_defaults(func=cmd_backup)

    v = sub.add_parser("verify", help="Check that the connected device is the one bound to --dest.")
    v.add_argument("--dest", required=True)
    v.add_argument("--label", default="This job")
    v.set_defaults(func=cmd_verify)

    l = sub.add_parser("list", help="List Apple devices connected over USB.")
    l.set_defaults(func=cmd_list)
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    # pymobiledevice3 reports the on-device passcode prompt as a log warning.
    logging.basicConfig(level=logging.WARNING, stream=sys.stdout, format="[%(levelname)s] %(message)s")
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(args.func(args))
    except JobError as e:
        say(f"[ERROR] {e}")
        return 1
    except KeyboardInterrupt:
        say("[CANCELLED]")
        return 130
    except Exception as e:
        # Device-side failures (cable pulled, backup cancelled on the phone, disk
        # full) arrive as pymobiledevice3 exceptions; the type is half the message.
        say(f"[ERROR] {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
