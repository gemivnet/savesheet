<#
  glacier_backup.ps1 - Full archival backup of a folder tree to AWS S3 Glacier Deep Archive.

  Tars each top-level subfolder of $SourceRoot and uploads it to a dated bucket
  (backup-YYYY-MM-DD), so the bucket name is generated automatically each run.

  * Self-contained: uses the rclone bundled in this repo + the pre-configured
    "glacier" remote (S3 / DEEP_ARCHIVE storage class).
  * Resumable: archives already present in the bucket are skipped, so a run that
    fails partway can simply be re-run to continue where it left off.
  * Verbose: every step is timestamped to the console AND to a log file
    (glacier-backup-YYYY-MM-DD.log next to this script) for debugging.
  * Size-driven split: top-level folders larger than $MaxTarSizeGB are broken out
    one level deeper (each subfolder its own .tar) to avoid huge single objects.
  * -WhatIf: DRY RUN. Prints the exact plan. Creates NO tars, NO staging dir, NO
    bucket, NO uploads, and writes NO log file. 100% read-only.
#>
param(
    [string]$SourceRoot       = "G:\Files",
    [string]$Staging          = "G:\glacier-staging",
    [int]$MaxTarSizeGB        = 150,        # top-level folders bigger than this are split one level (size only)
    [string[]]$ExpandOneLevel = @(),        # (optional) force-split these folders regardless of size
    [string[]]$Exclude        = @(),        # top-level folder names to skip entirely
    [string]$Bucket           = "",         # bucket override; empty = backup-<today>. Pass a previous run's bucket to resume it.
    [switch]$WhatIf                          # DRY RUN - plan only, no tars/uploads/side effects
)

$ErrorActionPreference = "Stop"
$here    = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$rclone  = Join-Path $here "rclone\rclone.exe"
$bucket  = if ($Bucket) { $Bucket } else { "backup-" + (Get-Date -Format "yyyy-MM-dd") }
$remote  = "glacier:$bucket"
$logFile = Join-Path $here ("glacier-backup-{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))
# rclone flags: verbose + a one-line stats update every 20s (newline-terminated so it
# shows in savesheet's live log panel). Deep Archive storage class + big multipart chunks.
$upFlags = @("--s3-storage-class","DEEP_ARCHIVE","--s3-chunk-size","128M","--s3-upload-concurrency","16","-v","--stats","20s","--stats-one-line")

# --- counters -----------------------------------------------------------------
$script:uploaded   = 0
$script:uploadedGB = 0.0
$script:skipped    = 0
$script:failures   = @()
$script:present    = @{}

function Log {
    param([string]$Msg, [string]$Level = "INFO")
    $line = "{0} [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level, $Msg
    Write-Host $line
    if (-not $WhatIf) { Add-Content -LiteralPath $logFile -Value $line -ErrorAction SilentlyContinue }
}

function Invoke-Rclone {
    # Run rclone, tee combined output to console + log, tolerate stderr (no halt), return exit code.
    param([string[]]$RcArgs)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $rclone @RcArgs 2>&1 | ForEach-Object {
        $s = "    rclone> $_"
        Write-Host $s
        if (-not $WhatIf) { Add-Content -LiteralPath $logFile -Value $s -ErrorAction SilentlyContinue }
    }
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prev
    return $code
}

function Get-FolderSizeGB {
    param([string]$Path)
    $sum = (Get-ChildItem -LiteralPath $Path -Recurse -File -Force -ErrorAction SilentlyContinue |
            Measure-Object -Property Length -Sum).Sum
    if ($sum) { return $sum / 1GB } else { return 0 }
}

function Backup-Item {
    param([string]$SourceDir, [string]$RemoteName, [bool]$IsFile = $false)

    if ($script:present.ContainsKey($RemoteName)) {
        Log "SKIP    $RemoteName (already in bucket)"
        $script:skipped++
        return
    }

    # ---- loose file: upload as-is (no tar) ----
    if ($IsFile) {
        Log "PLAN    file $SourceDir  ->  $RemoteName"
        if ($WhatIf) { return }
        Log "UPLOAD  $RemoteName (raw file) -> Deep Archive"
        $code = Invoke-Rclone (@("copyto", $SourceDir, "$remote/$RemoteName") + $upFlags)
        if ($code -ne 0) { throw "rclone upload exited $code" }
        Log "DONE    $RemoteName"
        $script:uploaded++
        return
    }

    Log "PLAN    $SourceDir  ->  $RemoteName"
    if ($WhatIf) { return }

    $tarPath = Join-Path $Staging (($RemoteName -replace '[\\/]', '__'))
    New-Item -ItemType Directory -Force -Path (Split-Path $tarPath) | Out-Null
    $parent = Split-Path $SourceDir -Parent
    $leaf   = Split-Path $SourceDir -Leaf

    Log "TAR     $RemoteName  <-  $SourceDir"
    $t0 = Get-Date
    & tar -cf "$tarPath" -C "$parent" "$leaf"
    if ($LASTEXITCODE -ne 0) { throw "tar exited $LASTEXITCODE" }
    $tarGB = [math]::Round((Get-Item $tarPath).Length / 1GB, 3)
    Log ("TARDONE $RemoteName  {0} GB in {1}" -f $tarGB, ((Get-Date) - $t0).ToString('hh\:mm\:ss'))

    Log "UPLOAD  $RemoteName ($tarGB GB) -> $remote (Deep Archive)"
    $u0 = Get-Date
    $code = Invoke-Rclone (@("copyto", $tarPath, "$remote/$RemoteName") + $upFlags)
    if ($code -ne 0) { throw "rclone upload exited $code (tar kept at $tarPath for retry)" }
    Log ("UPDONE  $RemoteName in {0}" -f ((Get-Date) - $u0).ToString('hh\:mm\:ss'))

    Remove-Item -LiteralPath $tarPath -Force
    $script:uploaded++
    $script:uploadedGB += $tarGB
}

# ==============================================================================
Log ("===== Glacier Deep Archive {0} -> {1} =====" -f ($(if ($WhatIf) { "DRY RUN" } else { "BACKUP" }), $remote))
Log "Source: $SourceRoot   Staging: $Staging   Split over: $MaxTarSizeGB GB   Log: $logFile"

# ---- preflight ----
if (-not (Test-Path $rclone))      { Log "rclone not found at $rclone" "ERROR"; exit 1 }
if (-not (Test-Path $SourceRoot))  { Log "source not found: $SourceRoot" "ERROR"; exit 1 }
if ((& $rclone listremotes) -notcontains "glacier:") { Log "rclone remote 'glacier:' not configured" "ERROR"; exit 1 }

# ---- create bucket + gather already-present objects for resume ----
if (-not $WhatIf) {
    Log "Ensuring bucket exists: $remote"
    [void](Invoke-Rclone @("mkdir", $remote))
    New-Item -ItemType Directory -Force -Path $Staging | Out-Null
}
try {
    $ErrorActionPreference = "Continue"
    $listing = & $rclone lsf --recursive $remote 2>$null
    $ErrorActionPreference = "Stop"
    foreach ($p in @($listing)) { if ($p) { $script:present[($p -replace '/$', '').Trim()] = $true } }
} catch { $ErrorActionPreference = "Stop" }
if ($script:present.Count) { Log "Resume: $($script:present.Count) object(s) already in $bucket will be skipped" }

# ---- build the work list (apply exclude + size-driven split) ----
$work = @()
foreach ($item in Get-ChildItem -LiteralPath $SourceRoot -Force | Sort-Object Name) {
    if ($Exclude -contains $item.Name) { Log "EXCLUDE $($item.Name)"; continue }
    if ($item.PSIsContainer) {
        $sizeGB = Get-FolderSizeGB $item.FullName
        $split  = ($ExpandOneLevel -contains $item.Name) -or ($sizeGB -gt $MaxTarSizeGB)
        if ($split) {
            Log ("SPLIT   {0} ({1} GB > {2} GB) -> one .tar per subfolder" -f $item.Name, [math]::Round($sizeGB), $MaxTarSizeGB)
            foreach ($sub in Get-ChildItem -LiteralPath $item.FullName -Directory -Force | Sort-Object Name) {
                $work += @{ Src = $sub.FullName; Name = "$($item.Name)/$($sub.Name).tar"; IsFile = $false }
            }
            # -Directory above skips loose files sitting directly in the split folder. The
            # unsplit branch tars the whole folder and catches them, so without this they are
            # dropped from the archive while the run still reports "Archive complete".
            # They keep their bare name; subfolders get a .tar suffix, so the two cannot collide.
            $loose = @(Get-ChildItem -LiteralPath $item.FullName -File -Force | Sort-Object Name)
            foreach ($f in $loose) {
                $work += @{ Src = $f.FullName; Name = "$($item.Name)/$($f.Name)"; IsFile = $true }
            }
            if ($loose.Count) { Log ("SPLIT   {0} + {1} loose file(s) at its root" -f $item.Name, $loose.Count) }
        } else {
            $work += @{ Src = $item.FullName; Name = "$($item.Name).tar"; IsFile = $false }
        }
    } else {
        $work += @{ Src = $item.FullName; Name = $item.Name; IsFile = $true }
    }
}
Log "Planned $($work.Count) archive(s)$(if ($WhatIf) { ' (DRY RUN - nothing will be created)' })"

# ---- process (each item isolated so one failure doesn't abort the rest) ----
foreach ($w in $work) {
    try {
        Backup-Item -SourceDir $w.Src -RemoteName $w.Name -IsFile $w.IsFile
    } catch {
        Log "FAIL    $($w.Name) : $($_.Exception.Message)" "ERROR"
        $script:failures += $w.Name
    }
}

if (-not $WhatIf) { Remove-Item -LiteralPath $Staging -Recurse -Force -ErrorAction SilentlyContinue }

# ---- summary ----
Log "===== SUMMARY ($bucket) ====="
Log ("Uploaded: {0} archive(s), {1} GB" -f $script:uploaded, [math]::Round($script:uploadedGB, 2))
Log "Skipped (already present): $script:skipped"
if ($script:failures.Count) {
    Log ("FAILED: {0} -> {1}" -f $script:failures.Count, ($script:failures -join ', ')) "ERROR"
    Log "Re-run this job to retry the failed archives (completed ones are skipped)." "ERROR"
    exit 1
}
Log ("All done. {0}" -f $(if ($WhatIf) { "(DRY RUN - no changes made)" } else { "Archive complete." }))
