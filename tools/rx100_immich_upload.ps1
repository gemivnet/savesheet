# Post-step for the sony_rx1000 job: upload the camera's NEW photos and clips to Immich.
#
# Replaces G:\Files\Photos\RX1000 VII\immich_upload.ps1 as the job's post_script. That one
# had two problems:
#   * It ended without `exit`, and a PowerShell script run with -File exits 0 when it
#     reaches its end, so a down server or an expired key still showed the job green.
#   * It ran `immich upload --recursive` over the WHOLE folder (5,731 files, 140 GB on
#     2026-11-12). The CLI reads and hashes every file before asking the server which ones
#     it lacks, so every run re-read 140 GB (George: "immich takes so long on the rx100 step").
#
# Now it keeps a list of what it has uploaded ($State: relative path + size, one per line)
# and hands the CLI only files not on it. File dates aren't used: FreeFileSync keeps the
# camera's times, so they say nothing about what is new on G:. The first run seeds the list
# with everything older than $Days days (the old step uploaded the whole folder on every
# run, so those are in Immich) and uploads the rest. A file the CLI finds already in Immich
# is simply skipped by it. Anything this ever misses is still caught by immich_sync, which
# uploads everything under G:\Files\Photos that Immich lacks.
#
# "Continue", not "Stop": in Windows PowerShell 5.1 a native command's stderr output can
# become a terminating error under Stop (see sigtop_export.ps1). Exit codes decide success.
param([int]$Days = 7)
$ErrorActionPreference = "Continue"
$folder = "G:\Files\Photos\RX1000 VII"
$state  = "G:\Files\Tools\rx100-immich-uploaded.txt"
$immich = "C:\Users\georg\AppData\Local\nvm\v22.16.0\immich.cmd"
# What Immich takes from this camera. DNGs stay out of Immich, as before.
$media = @(".jpg", ".jpeg", ".arw", ".hif", ".heic", ".mp4", ".mov", ".mts")

function Get-Key($file) {
    return ("{0}|{1}" -f $file.FullName.Substring($folder.Length + 1), $file.Length)
}

$all = @(Get-ChildItem -LiteralPath $folder -Recurse -File -ErrorAction Stop |
         Where-Object { $media -contains $_.Extension.ToLower() })

if (-not (Test-Path -LiteralPath $state)) {
    $since = (Get-Date).AddDays(-$Days)
    $seed = @($all | Where-Object { $_.LastWriteTime -lt $since -and $_.CreationTime -lt $since } |
              ForEach-Object { Get-Key $_ })
    # An empty -Value would leave no file behind, and the next run would seed again.
    New-Item -ItemType File -Path $state -Force -ErrorAction Stop | Out-Null
    if ($seed.Count -gt 0) { Add-Content -LiteralPath $state -Value $seed -Encoding UTF8 -ErrorAction Stop }
    Write-Output "[rx100-immich] first run: recorded $($seed.Count) older files as already in Immich"
}

$done = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
foreach ($line in @(Get-Content -LiteralPath $state -Encoding UTF8 -ErrorAction Stop)) {
    if ($line) { [void]$done.Add($line) }
}
$new = @($all | Where-Object { -not $done.Contains((Get-Key $_)) })
Write-Output "[rx100-immich] $($all.Count) photos/clips in the folder, $($new.Count) not uploaded yet"
if ($new.Count -eq 0) { exit 0 }

# Batches keep each command line well under Windows' 32,767-character limit, and each
# batch is recorded as soon as it succeeds, so a failure part-way loses nothing.
$batch = 100
$failed = 0
for ($i = 0; $i -lt $new.Count; $i += $batch) {
    $chunk = @($new[$i..([Math]::Min($i + $batch, $new.Count) - 1)])
    $paths = @($chunk | ForEach-Object { $_.FullName })
    & $immich upload @paths
    if ($LASTEXITCODE -ne 0) {
        $failed++
        Write-Output "[rx100-immich] batch starting at file $($i + 1) failed (exit $LASTEXITCODE)"
        continue
    }
    Add-Content -LiteralPath $state -Value @($chunk | ForEach-Object { Get-Key $_ }) -Encoding UTF8
}
if ($failed -gt 0) {
    Write-Output "[rx100-immich] FAILED: $failed batch(es) did not upload; they will be retried next run"
    exit 1
}
Write-Output "[rx100-immich] done"
exit 0
