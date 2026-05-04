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
- **Live log output** — streams command stdout/stderr in real time
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

Rclone jobs automatically append `--progress` and `--suffix ".<timestamp>"` for versioning.

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

### Dependencies

The `depends_on` field lists job IDs. When you run a job with unmet dependencies, the GUI shows a warning listing which ones haven't been run this session. You can always choose to proceed anyway — dependencies are advisory, never blocking.

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
