# SaveSheet

A Windows GUI for managing device and cloud backups. Wraps common tools — adb, rclone, FreeFileSync, MediaDeviceCopier, pymobiledevice3 — behind a visual interface with per-job controls, connection testing, and backup history.

Built for people who back up phones, tablets, cameras, NAS, cloud storage, and external drives on a regular cadence and want something lighter than a full backup suite.

![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)
![License](https://img.shields.io/badge/license-MIT-green)

## Features

- **Per-job control** — each backup is run individually; no forced sequential mode
- **Device configurator** — wizard that tests connections (`adb devices`, `MediaDeviceCopier list-devices`), lets you pick folders, and saves to config
- **Full iPhone / iPad backups** — encrypted, incremental, restorable device backups, each job bound to one device by UDID and each backup checked against its password
- **Debug tools** — test ADB, list MTP and iOS devices, check rclone remotes, ping your NAS
- **Backup history** — tracks last success time, status, and duration for every job
- **Dependency warnings** — advisory alerts when running a sync before its source devices have been backed up (never blocking)
- **Live log output** — streams command stdout/stderr in real time, and writes every run to its own file under `logs/`
- **YAML config** — human-readable, easy to edit by hand or through the GUI

## Supported Job Types

| Type | Tool | Use Case |
|------|------|----------|
| `adbsync` | [adbsync](https://github.com/jb2170/adbsync) + adb | Android phones and tablets |
| `mtp` | [MediaDeviceCopier](https://github.com/nicoriff/MediaDeviceCopier) | Photos from iPhones, iPads, and other MTP devices |
| `ios_backup` | [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) | Full, encrypted, restorable iPhone and iPad backups |
| `ffs` | [FreeFileSync](https://freefilesync.org/) | Cameras, SD cards (via batch files) |
| `rclone` | [rclone](https://rclone.org/) | Cloud storage, NAS, external drives |
| `script` | PowerShell | Custom scripts as a fallback |

## Prerequisites

- **Python 3.11+**
- **[uv](https://docs.astral.sh/uv/)** — Python package manager
- One or more of the backup tools above, depending on your devices

## Setup

```bash
git clone https://github.com/gemivnet/savesheet.git
cd savesheet
uv sync
```

Copy the example config and edit it with your real paths and devices:

```bash
cp backup_config.example.yaml backup_config.yaml
```

## Usage

```bash
uv run python -m backup_manager
```

Or double-click `run.pyw` (no console window).

### Quick start

1. Open **Debug Tools** to verify your tools are working (Test ADB, List MTP Devices, etc.)
2. Click **+ Add Device** to add your phones, tablets, or cameras through the wizard
3. Edit sync jobs (rclone) by double-clicking them
4. Right-click a job and select **Run**, or select it and press Enter

## Configuration

All configuration lives in `backup_config.yaml` (gitignored — your copy stays local). See [`backup_config.example.yaml`](backup_config.example.yaml) for the full schema.

### Tools section

Paths to the backup tools. Can be absolute or relative to the config file's directory.

```yaml
tools:
  adb: platform-tools/adb.exe
  adbsync: path/to/adbsync.exe
  rclone: rclone/rclone.exe
  media_device_copier: MediaDeviceCopier.exe
  freefile_sync: "C:\\Program Files\\FreeFileSync\\FreeFileSync.exe"
```

### Jobs section

Each job has a unique ID (the YAML key) and these fields:

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Display name in the GUI |
| `type` | yes | `adbsync`, `mtp`, `rclone`, `ffs`, or `script` |
| `group` | yes | `device` or `sync` — controls UI grouping |
| `source` | varies | Source path (device path, rclone remote, local path) |
| `destination` | varies | Local backup destination |
| `exclude` | no | List of glob patterns to exclude |
| `depends_on` | no | List of job IDs that should run first (advisory) |

#### Type-specific fields

**adbsync** — backs up Android devices over USB:
```yaml
my_phone:
  name: "My Phone"
  type: adbsync
  group: device
  source: "/storage/emulated/0/"
  destination: "C:\\Backups\\Phone"
  exclude: ["*cache*", "*.db*"]
```

**mtp** — copies from iPhones, iPads, and other MTP devices:
```yaml
my_ipad:
  name: "My iPad"
  type: mtp
  group: device
  device_name: "My iPad"        # Name shown in MediaDeviceCopier list-devices
  source: "Internal Storage"     # Or "Internal Storage\DCIM" for photos only
  destination: "C:\\Backups\\iPad"
  recursive: true
```

On an Apple device MTP reaches the camera roll and nothing else. For everything else, use `ios_backup`.

**ios_backup** — the device's own backup, the kind Finder and iTunes take: messages, app data, settings, home screen layout and, because it is encrypted, Health, saved passwords, Wi-Fi networks and call history. It is what you restore onto a replacement device.
```yaml
my_iphone_full:
  name: "My iPhone (Full)"
  type: ios_backup
  group: device
  destination: "C:\\Backups\\iOS Full\\My iPhone"
  device_class: iPhone           # or iPad; only used to pick the device on the first run
```

Put the backup password in a `.env` file next to `backup_config.yaml` (gitignored) as `IOS_BACKUP_PASSWORD=<a long random string>`. It is read from there, or from the environment, and never passed on a command line — every command the app runs is echoed to the log panel and written to `logs/`. **Keep it.** No backup taken with it can be opened without it.

What a run does:

1. **Finds its device.** The first run binds the job to the single matching device on USB and records its UDID in `savesheet-device.json` in the destination. After that the job refuses any other device, and a device already bound to a neighbouring folder is never offered to a new job. Windows calls every iPhone "Apple iPhone", so the UDID is the only thing that tells two apart; keep the destinations under one common parent so the jobs can see each other's bindings. To move a job to a replacement device, delete `savesheet-device.json`.
2. **Waits for trust.** A device new to this PC shows *Trust This Computer?* — unlock it, tap Trust, enter the passcode. The job waits three minutes.
3. **Turns on encryption** if it is off, using the password from `.env`. The device asks for its passcode.
4. **Backs up** into `<destination>\<UDID>\`. Incremental after the first run, which copies roughly the device's used storage and can take hours. iOS asks for the passcode at the start of every backup.
5. **Proves the backup opens**, by unwrapping its keybag and manifest key with the password from `.env`. A device given a backup password years ago in iTunes keeps using that one and will produce backups nobody can open; this step catches it and says how to clear the old password.

If a device already has a backup password from before, add it to `.env` as `IOS_BACKUP_OLD_PASSWORD=...` and run the job once: it changes that device over to `IOS_BACKUP_PASSWORD` and moves the backup made with the old password aside as `<UDID>.old-password-<date>`. If the old password is unknown, *Settings > General > Transfer or Reset > Reset > Reset All Settings* on the device clears it. A job whose device is in this state fails immediately on later runs instead of taking another backup it cannot open.

Requires Apple's *Apple Mobile Device Service*, which MTP does not need. A backup run installs it with `winget install Apple.AppleMobileDeviceSupport` if it is missing.

An `mtp` job can set `same_device_as: my_iphone_full` to check the bound UDID before copying photos; the check is skipped until the full-backup job has run once.

To restore, use Finder, iTunes or the Apple Devices app, or `pymobiledevice3 backup2 restore "<destination>"`.

**rclone** — syncs to/from cloud, NAS, or external drives:
```yaml
files_to_nas:
  name: "Files to NAS"
  type: rclone
  group: sync
  depends_on: [my_phone, my_ipad]
  rclone_command: copy            # copy, sync, or move
  source: "C:\\Users\\me\\Files"
  destination: "\\\\server\\nas\\files"
  flags:
    transfers: 8
    buffer-size: 32M
    checksum: true
```

Rclone jobs automatically append `--stats 5s --stats-one-line --stats-log-level NOTICE` and `--suffix ".<timestamp>"` for versioning. One-line stats rather than `--progress` because the latter redraws in place with terminal escapes, which the log panel cannot render; `--stats-log-level NOTICE` is required, not cosmetic: stats print at INFO while rclone's default log level is NOTICE, so without it the periodic line never appears. Set `stats`, `stats-one-line`, or `stats-log-level` in a job's `flags` to override any of them.

For remotes that need on-the-fly config (e.g. SFTP):
```yaml
rclone_config_create: "myremote sftp host=server.local user=me key_file=path/to/key.pem"
```

**ffs** — runs a FreeFileSync batch file:
```yaml
my_camera:
  name: "My Camera"
  type: ffs
  group: device
  ffs_batch: "Camera.ffs_batch"
```

**script** — runs an arbitrary PowerShell script:
```yaml
custom_task:
  name: "Custom Backup"
  type: script
  group: sync
  script_path: "my_script.ps1"
```

#### reMarkable tablets

There is no dedicated job type. The tablet is backed up with `rclone` over its SSH server:

```yaml
my_remarkable:
  name: "My reMarkable"
  type: rclone
  group: device
  rclone_command: copy
  rclone_config_create: "remarkable sftp host=10.11.99.1 user=root pass=<root password> shell_type=unix md5sum_command=md5sum sha1sum_command=sha1sum --obscure"
  source: "remarkable:/home/root"
  destination: "C:\\Backups\\reMarkable"
  flags:
    transfers: 4
    sftp-host-key-algorithms: ssh-ed25519
```

`10.11.99.1` is the tablet over USB; on Wi-Fi use its LAN address (SSH-over-Wi-Fi must be enabled first on system software 3.22+). The root password is under Settings → General → Help → Copyrights and licenses. It can change across firmware updates and is reset by a factory reset, though it does not always change — editing this one line is how you recover when it does.

`--obscure` matters: without it rclone can misread a 22+ character base64-looking password as already-obscured and write it to `rclone.conf` in the clear. Note that obscuring is reversible either way, so the password in `backup_config.yaml` is effectively plaintext — if this checkout sits inside a tree that other jobs back up, it travels with them. To avoid that, drop `rclone_config_create`, run the same `rclone config create` command by hand once, and let the credential live only in `rclone.conf`. An SSH key via `key_file=` also works and survives firmware updates, but Windows `ssh-keygen` cannot write a key to a network drive — generate it under `%USERPROFILE%\.ssh` and use an absolute path.

`sftp-host-key-algorithms: ssh-ed25519` is required, not a tuning knob. The dropbear build on the tablet advertises an RSA host key it does not have; without pinning ed25519 the handshake fails with `couldn't connect SSH: handshake failed: EOF` before rclone gets to authenticate.

This copies the raw notebook data, which is what RCU restores from. To also get readable files, [`rcu_export.ps1`](rcu_export.ps1) drives RCU's CLI (`RCU-CLI.exe`) to pull each document as `.rmdoc` or rendered PDF, skipping documents that have not changed since the last run:

```yaml
my_remarkable_docs:
  name: "My reMarkable (rmdoc via RCU)"
  type: script
  group: device
  depends_on: [my_remarkable]
  script_path: rcu_export.ps1
  script_args: -Format rmdoc -OutDir "C:\\Backups\\reMarkable\\Documents"
```

`-Format rmdoc` is the format to back up: a lossless, restorable archive per document. Its `.rm` ink files are byte-for-byte the tablet's own, checked for all 238 written pages on 2026-11-12, and the reMarkable app can open it or export a PDF from it on demand. PDF output (`pdf-rm`, `pdf-v`, `pdf-b`) is still supported but **not used, deliberately**. The reMarkable's pages scroll: one page grows downward as you write (on Shelby's tablet, 132 of 232 written pages ran past the first screen, one by about 20 screens). RCU's `pdf-v` renders each page at a single screen size, so everything below the first screenful is cut off, and a long note looks like a one-page PDF. The cut-off `PDF` export job was removed on 2026-11-12, along with its folder. If you add a PDF job anyway, give it its own `-OutDir` (the incremental manifest lives inside it), and compare page heights with each page's `verticalScroll` in the `.content` sidecar, not just page counts.

Two prerequisites, both easy to miss:

- **A saved connection preset in the RCU GUI.** `--autoconnect` reuses the last-used preset and there is no way to pass a host on the command line.
- **The tablet's USB web interface, for `-Format rmdoc` and `pdf-rm` only** (Settings → Storage → USB web interface). Those two fetch over HTTP on port 80 rather than SSH; with it off, every document fails with `wget: can't connect to remote host (10.11.99.1): Connection refused` inside `save_rmwebui_file`. `rmn`, `pdf-v`, and `pdf-b` go over SSH and don't care. The script detects this case and stops rather than working through the whole document list.

RCU's CLI cannot report which collection a document belongs to, so the folder tree is rebuilt from the `.metadata` files in the rclone backup of `/home/root` — hence the `depends_on`. Point `-XochitlDir` elsewhere if your backup lives somewhere else; without it the export is flat.

RCU's whole-device Snapshot has no command-line flag, so it stays a manual GUI step.

#### Signal Desktop

Also a `script` job. Signal offers no export and its database is SQLCipher-encrypted, so the conversations exist only inside `%APPDATA%\Signal`. [`sigtop_export.ps1`](sigtop_export.ps1) drives [sigtop](https://github.com/tbvdm/sigtop) to read that database (read-only — Signal Desktop can stay open) and write a transcript per conversation plus the attachments as real files:

```yaml
signal_desktop:
  name: Signal Desktop (sigtop)
  type: script
  group: device
  script_path: sigtop_export.ps1
  script_args: -OutDir "C:\\Backups\\Signal"
```

Output is `messages\<conversation>.txt` and `attachments\<conversation>\`. `-MessageFormat json` writes `messages-json\` instead — reactions, quotes, edits and raw timestamps, which the text transcript drops. Both can be exported at once with `-MessageFormat text,json`.

There is no path back into Signal. This is an archive, not a restorable backup.

Two sigtop behaviours shape the whole script:

- **It never overwrites an attachment.** A second export of the same file lands next to the first as `name-2.jpg`. So the job runs `export-attachments -i`, which keeps a `.incremental` file at the top of the attachments directory recording what it has already written. Delete that file and the next run writes a second copy of the entire archive. The script refuses to run against an attachments directory that has files but no `.incremental` — pass `-SeedIncremental` to move the untracked copy aside, re-export clean, and verify by SHA-256 that every file in the old copy is accounted for before you delete it. `-i` also keys on content rather than path, so a photo forwarded into three conversations is stored once, under whichever conversation sigtop reaches first; the transcripts identify attachments by the sender's filename and byte size, never by the exported name, so nothing refers to the copies that are not written.
- **It refuses to overwrite a transcript**, failing the whole `export-messages` command with `open <file>: The file exists`. Transcripts are therefore exported to a staging directory and moved over the destination, which also means a failed export leaves the previous ones intact. Transcripts for conversations that no longer exist in Signal are left in place rather than swept away — the export is additive, like `rcu_export.ps1`, so disappearing messages and deleted-for-everyone media stay in the archive after Signal itself has forgotten them.

`export-attachments -m` stamps each file's modification time from the message it came from rather than from the time of the export. Without it every attachment looks new to the rclone jobs on every run, and ~670 MB gets re-copied to the NAS, Drive and Glacier each cycle.

sigtop exits 1 when it skipped any attachment that was never downloaded to this machine (`skipping attachment without path`, `skipping pending attachment`) even though the export succeeded — normal for an account that is read mostly on a phone. The script judges on what sigtop reported rather than on the exit code, and collapses the skips to a count so they cannot bury a real error.

### Archiving a job

Set `group: archived` on a job for a device that is no longer in use. It moves out of DEVICES into its own ARCHIVED section, keeps its history and its record of where the backup lives, and can still be run. Remove it from other jobs' `depends_on` so they stop warning about it. The backed-up files are not touched.

### Dependencies

The `depends_on` field lists job IDs. A dependency counts as met if it succeeded this session, or if its last run succeeded within the past 24 hours and it hasn't been changed since. When you run a job with unmet dependencies, the GUI lists them and asks whether to proceed. You can always say yes: dependencies are advisory, never blocking.

### Queue

Right-click a job → **Add to Queue** to run it after whatever is running. **Run** on a job while another is running does the same. Queued jobs run one at a time, in order, and show **Queued #n** in the Status column. The toolbar shows the count and has **Clear Queue**, and right-click → **Remove from Queue** takes one job out.

The dependency check happens when a job is queued. Jobs running or queued ahead of it count as met, because they will run first. If one of them then fails, the job that depended on it is **skipped** when its turn comes, with no prompt (the queue is meant to run unattended), and the rest of the queue carries on. A dependency problem you said yes to when queuing still runs. **Stop** asks whether to clear the queue as well; if you say no, the next job starts. The queue isn't saved, so it's empty when the app restarts.

### Logs

Every run writes a full transcript to `logs/<timestamp>_<job_id>.log` next to the config — the command line, all stdout/stderr, and the exit code. The GUI panel shows the same stream but is cleared on **Clear** and lost on exit, so the file is what you go to after a failure you didn't watch. **Open Logs** in the toolbar opens the directory.

If this checkout sits inside a tree one of your own rclone jobs backs up, exclude `logs/` from that job. rclone fails a file whose size changes mid-copy, so a job would reliably error on the log it is writing at that moment.

Output is flushed on every write, so a cancelled run, a crash, or a killed app still leaves a readable log. Files older than 30 days are pruned at the start of each run (`log_retention_days` on `JobExecutor`). A log that cannot be opened produces a warning and the backup runs anyway.

Note that the transcript includes the command line verbatim, so any credential passed through `rclone_config_create` appears in the log file too.

### History

Backup history is stored in `backup_history.yaml` (auto-generated, gitignored). It tracks the last success time, last attempt, status, and duration for each job. The GUI displays this as relative time ("2d ago", "18d ago", "Never").

## Project Structure

```
savesheet/
├── backup_config.example.yaml   # Example config (no PII)
├── backup_manager/
│   ├── __main__.py              # Entry point
│   ├── app.py                   # Tk root and app orchestration
│   ├── config.py                # YAML config loader and Job model
│   ├── executor.py              # Command builders and subprocess runner
│   ├── history.py               # Backup history tracking
│   ├── utils.py                 # Path resolution, relative time formatting
│   └── ui/
│       ├── main_frame.py        # Job list, log panel, toolbar
│       ├── device_wizard.py     # Add/edit device dialog with connection testing
│       ├── job_editor.py        # Edit rclone/script jobs
│       └── debug_dialog.py      # Diagnostic tools
├── pyproject.toml
├── run.pyw                      # Double-click launcher (no console)
└── README.md
```

## License

MIT
