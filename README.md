# SaveSheet

A Windows GUI for managing device and cloud backups. Wraps common tools — adb, rclone, FreeFileSync, MediaDeviceCopier — behind a visual interface with per-job controls, connection testing, and backup history.

Built for people who back up phones, tablets, cameras, NAS, cloud storage, and external drives on a regular cadence and want something lighter than a full backup suite.

![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)
![License](https://img.shields.io/badge/license-MIT-green)

## Features

- **Per-job control** — each backup is run individually; no forced sequential mode
- **Device configurator** — wizard that tests connections (`adb devices`, `MediaDeviceCopier list-devices`), lets you pick folders, and saves to config
- **Debug tools** — test ADB, list MTP devices, check rclone remotes, ping your NAS
- **Backup history** — tracks last success time, status, and duration for every job
- **Dependency warnings** — advisory alerts when running a sync before its source devices have been backed up (never blocking)
- **Live log output** — streams command stdout/stderr in real time, and writes every run to its own file under `logs/`
- **YAML config** — human-readable, easy to edit by hand or through the GUI

## Supported Job Types

| Type | Tool | Use Case |
|------|------|----------|
| `adbsync` | [adbsync](https://github.com/jb2170/adbsync) + adb | Android phones and tablets |
| `mtp` | [MediaDeviceCopier](https://github.com/nicoriff/MediaDeviceCopier) | iPhones, iPads, and other MTP devices |
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

`-Format rmdoc` is the useful default: a restorable archive per document that keeps the ink. PDF output (`pdf-rm`, `pdf-v`, `pdf-b`) is also supported, but RCU renders page-by-page and `pdf-v` has been seen to truncate long notebooks, so the script verifies exported PDFs against the page count in the tablet's `.content` sidecar and flags any that come out short. Run one job per format, each with its own `-OutDir`, since the incremental manifest lives inside it.

Two prerequisites, both easy to miss:

- **A saved connection preset in the RCU GUI.** `--autoconnect` reuses the last-used preset and there is no way to pass a host on the command line.
- **The tablet's USB web interface, for `-Format rmdoc` and `pdf-rm` only** (Settings → Storage → USB web interface). Those two fetch over HTTP on port 80 rather than SSH; with it off, every document fails with `wget: can't connect to remote host (10.11.99.1): Connection refused` inside `save_rmwebui_file`. `rmn`, `pdf-v`, and `pdf-b` go over SSH and don't care. The script detects this case and stops rather than working through the whole document list.

RCU's CLI cannot report which collection a document belongs to, so the folder tree is rebuilt from the `.metadata` files in the rclone backup of `/home/root` — hence the `depends_on`. Point `-XochitlDir` elsewhere if your backup lives somewhere else; without it the export is flat.

RCU's whole-device Snapshot has no command-line flag, so it stays a manual GUI step.

### Dependencies

The `depends_on` field lists job IDs. When you run a job with unmet dependencies, the GUI shows a warning listing which ones haven't been run this session. You can always choose to proceed anyway — dependencies are advisory, never blocking.

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
