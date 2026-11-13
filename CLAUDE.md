# savesheet — notes for Claude sessions

savesheet runs on George's **Windows PC** from `N:\workspace\savesheet`, which is this folder
(`/nas/workspace/savesheet` on home-server). A session on the server is editing the live app.

## Never run `uv run`, `uv sync` or `uv lock` here from Linux

`.venv/` is the PC's live **Windows** environment. `run.pyw` starts `.venv\Scripts\pythonw.exe`,
and `tools.adbsync` is `.venv/Scripts/adbsync.exe`. Any uv project command run here on Linux
replaces it with a Linux venv. The app then won't start on the PC, and every adbsync and iOS job
fails. It happened on 2026-11-12 from a one-line `uv run python -c ...` config check.

- To check imports or config, use plain `python3` (with PyYAML), or point uv elsewhere:
  `UV_PROJECT_ENVIRONMENT=/tmp/<scratch>/env uv run ...`.
- To test a standalone script (e.g. `tools/immich-sync/immich_sync.py`), copy it to a scratch
  directory and run it there with `uv run --with <deps> python -I script.py`.
- To install a Windows package from Linux, target the Windows site-packages:
  `uv pip install ... --target .venv/Lib/site-packages --python-platform x86_64-pc-windows-msvc --python-version 3.13 --only-binary :all:`.
- If the venv is broken, the real fix is `uv sync` on the PC. The 2026-11-12 rebuild from
  Linux is described in the homelab wiki changelog for that day.

## Changing a job means it shows "Re-run"

`backup_manager/fingerprint.py` fingerprints each job: its settings, `JOB_LOGIC_VERSION` for its
type, and the contents of the in-repo scripts it runs. The app records the fingerprint of each job's
last successful run in `backup_fingerprints.yaml`, and a job whose current fingerprint differs
shows **Re-run** with "Never (changed)" (George, 2026-11-12). Config and script edits are picked up
automatically. **When you change how the executor builds a job type's commands, or a helper module a
job runs** (`adb_verify`, `card_conflicts`, `newest_copy`, ...), **bump that type in
`JOB_LOGIC_VERSION`**, or the change goes unflagged.

## Other rules

- `backup_config.yaml` is gitignored and loaded once when the app starts. Config edits take
  effect after savesheet is restarted. A GUI save rewrites the file without its comments.
- The rclone jobs' `--suffix` versioning is deliberate: it keeps every revision.
- Glacier uses the write-only IAM user `glacier-writer`, whose key is in `.env` as `RCLONE_CONFIG_GLACIER_*`
  (see `tools/glacier/`). Never put an admin AWS key back in `.env`. After each yearly run, seal the new
  bucket with `tools/glacier/glacier_seal.sh` (needs a root `aws login`). Details: the wiki's `backups.md`.
- The iOS backup password must never appear on a command line (the executor logs commands),
  and the `IOS_BACKUP_PASSWORD` line in `.env` must never be removed.
- Filenames must survive NTFS (see `/nas/workspace/CLAUDE.md`).
- The homelab wiki's live pages are `/shared-configs/wiki/docs/`, not the read-only mirror in
  the home-server repo. Backup docs are on `backups.md` there.
