# Savesheet job "immich_sync": keep Immich and G:\Files in step, both ways - upload
# photos Immich lacks, fix dates, download Immich originals Files lacks, and copy
# Immich's newest database dump into Files. See immich_sync.py for details.
#
# The code lives here in the savesheet repo; its data (rules.yaml, state.sqlite, the
# hash cache, reports\) stays in G:\Files\Tools\immich-sync. Each run copies the
# current immich_sync.py over the one there and runs it there, so the copy on G: is
# never stale - edit this one, not that one.
#
# Pass -DryRun to only write reports.
param([switch]$DryRun)
# "Continue", not "Stop": in Windows PowerShell 5.1 a native command's stderr output can
# become a terminating error under Stop (see sigtop_export.ps1). The exit code below is
# what decides success.
$ErrorActionPreference = "Continue"
$data = "G:\Files\Tools\immich-sync"
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "immich_sync.py") -Destination (Join-Path $data "immich_sync.py") -Force -ErrorAction Stop
Set-Location $data -ErrorAction Stop
$uvArgs = @("run", "immich_sync.py", "all")
if ($DryRun) { $uvArgs += "--dry-run" }
& "$env:USERPROFILE\.local\bin\uv.exe" @uvArgs
exit $LASTEXITCODE
