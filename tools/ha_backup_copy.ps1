# Savesheet job "nas_homeassistant": copy the newest Home Assistant automatic backup into
# G:\Files, keeping the last two there. Fails if HA's newest backup is over 3 days old.
# See backup_manager\newest_copy.py for why it is the newest one only.
# "Continue", not "Stop": in Windows PowerShell 5.1 a native command's stderr output can
# become a terminating error under Stop (see sigtop_export.ps1). The exit code below is
# what decides success.
$ErrorActionPreference = "Continue"
Set-Location (Join-Path $PSScriptRoot "..")
& ".\.venv\Scripts\python.exe" -m backup_manager.newest_copy `
    --src "\\server\nas\appdata\homeassistant" --glob "automatic_backup_*.tar" `
    --dest "G:\Files\Server Backup\homeassistant" --keep 2 --max-age-days 3
exit $LASTEXITCODE
