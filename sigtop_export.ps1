<#
  sigtop_export.ps1 - Export Signal Desktop's messages and attachments with sigtop.

  Signal Desktop keeps everything in an SQLCipher database under %APPDATA%\Signal
  that nothing else can read, and Signal itself offers no export. sigtop decrypts
  it in place (read-only) and writes a transcript per conversation plus the
  attachments as real files. That output IS the backup - there is no path back
  into Signal, so this is an archive, not a round-trip.

  * Additive: attachments already exported are never re-fetched and never
    deleted, so disappearing messages and deleted-for-everyone media stay in the
    archive after Signal itself has forgotten them. Same principle as
    rcu_export.ps1 - a backup, not a mirror.
  * Incremental: sigtop's -i keeps a .incremental file at the top of the
    attachments directory listing what it has already written. Without it every
    run re-exports all ~700 MB *alongside* the existing files as "name-2.jpeg",
    "name-3.jpeg" and so on - sigtop never overwrites an attachment. That
    failure mode is what the guard further down exists to prevent.
  * Deduplicated: -i keys on content, not on path, and it does so across the
    whole export - so a photo forwarded into three conversations is written once,
    under whichever conversation sigtop reaches first. It is not in the other two
    directories at all. This costs nothing: the transcripts identify attachments
    by the sender's filename and byte size, never by the name sigtop gives the
    exported file, so there is no reference to break.
  * Dated: -m stamps each attachment's modification time from the message it
    came from rather than from the time of the export. The mtimes are then
    stable across runs, which is what stops the downstream rclone jobs
    (external_drive, files_to_nas, files_to_google_drive) re-copying unchanged
    attachments to the NAS, Drive and Glacier every cycle.
  * Messages are rewritten in full every run: ~1.5 MB of text, and a full
    rewrite is how edits and deletions in old conversations reach the
    transcript. sigtop will not overwrite an existing transcript either, so they
    are exported to a staging directory and moved over the top - which also
    means a failed export leaves the previous transcripts intact.

  Signal Desktop can stay open; sigtop only reads. A message that arrives during
  the run may miss this export and land in the next one.

  Exit codes: sigtop returns 1 when it skipped any attachment that is not
  downloaded to this machine ("without path", "pending") even though the export
  itself succeeded, so this script judges on what sigtop actually reported rather
  than on the exit code alone.
#>
param(
    [string]$SigtopExe = "sigtop.exe",
    [string]$OutDir    = "G:\Files\Backups\Signal",
    # text = readable transcript per conversation (-> messages\).
    # json = full structure: reactions, quotes, edits, raw timestamps (-> messages-json\).
    # Both may be given: -MessageFormat text,json
    [ValidateSet("text", "json")]
    [string[]]$MessageFormat = @("text"),
    # Signal Desktop's data directory. Empty = sigtop's own default (%APPDATA%\Signal).
    [string]$SignalDir = "",
    [switch]$SkipMessages,
    [switch]$SkipAttachments,
    # For a first run against an attachments directory that was exported WITHOUT -i:
    # there is no .incremental telling sigtop what it already has, so a normal run
    # would write a second copy of everything. This moves the existing directory
    # aside, re-exports clean, then verifies by content hash that every file in the
    # old copy is accounted for in the new one. The old copy is left on disk either
    # way - nothing is deleted here.
    [switch]$SeedIncremental,
    [switch]$WhatIf                          # DRY RUN - prints the plan, writes nothing
)

$ErrorActionPreference = "Stop"
$here = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not [System.IO.Path]::IsPathRooted($SigtopExe)) { $SigtopExe = Join-Path $here $SigtopExe }

$attachDir = Join-Path $OutDir "attachments"
$stateFile = Join-Path $attachDir ".incremental"
$stagingDir = Join-Path $OutDir ".messages-staging"
# Message format -> output subdirectory. Separate directories rather than one mixed
# one: both formats use the same conversation names and only the extension differs.
$msgDirs = @{ "text" = "messages"; "json" = "messages-json" }

$script:warnings = @()
$script:errors   = @()

function Log {
    param([string]$Msg, [string]$Level = "INFO")
    Write-Host ("{0} [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level, $Msg)
    if ($Level -eq "WARN")  { $script:warnings += $Msg }
    if ($Level -eq "ERROR") { $script:errors   += $Msg }
}

function Invoke-Sigtop {
    # sigtop writes to stderr, and in PowerShell 5.1 a native command's stderr becomes
    # a terminating error under ErrorActionPreference=Stop even at exit 0. Capture both
    # streams, split "skipping ..." notices (expected, not failures) from everything
    # else, and let the caller decide.
    param([string[]]$SigArgs)
    $full = @()
    if ($SignalDir) { $full += @("-d", $SignalDir) }
    $full += $SigArgs
    Log ("sigtop {0}" -f ($full -join " "))
    if ($WhatIf) { return [pscustomobject]@{ Code = 0; Skips = @(); Other = @() } }

    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $out = & $SigtopExe @full 2>&1 | ForEach-Object { [string]$_ }
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prev

    # "skipping attachment without path" / "skipping pending attachment": the media was
    # never downloaded to this machine, so there is nothing on disk to export. Expected
    # on any account that reads on a phone, and the sole reason for a non-zero exit on
    # an otherwise clean run - collapsed to a count so it cannot bury a real error.
    $skips = @($out | Where-Object { $_ -match ':\s*skipping ' })
    $other = @($out | Where-Object { $_ -and $_ -notmatch ':\s*skipping ' })
    [pscustomobject]@{ Code = $code; Skips = $skips; Other = $other }
}

function Report-Sigtop {
    # Log a sigtop result and return $true if the run is genuinely healthy.
    param([pscustomobject]$Result, [string]$What)
    foreach ($line in $Result.Other) { Log "    sigtop> $line" }
    if ($Result.Skips.Count -gt 0) {
        Log ("{0}: {1} attachment(s) skipped - not downloaded to this machine." -f $What, $Result.Skips.Count)
        foreach ($s in ($Result.Skips | Select-Object -First 3)) { Log "    sigtop> $s" }
        if ($Result.Skips.Count -gt 3) { Log ("    ... and {0} more" -f ($Result.Skips.Count - 3)) }
    }
    # Trust what sigtop reported over the exit code: skip notices alone set it to 1.
    if ($Result.Other.Count -eq 0) { return $true }
    Log ("{0} failed (exit {1})." -f $What, $Result.Code) "ERROR"
    return $false
}

function Measure-Tree {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return [pscustomobject]@{ Files = 0; MB = 0.0 } }
    $m = Get-ChildItem -LiteralPath $Path -Recurse -File -Force -ErrorAction SilentlyContinue |
         Measure-Object -Property Length -Sum
    [pscustomobject]@{ Files = [int]$m.Count; MB = [math]::Round(($m.Sum / 1MB), 1) }
}

function Get-DuplicateSuffixed {
    # sigtop never overwrites an attachment: a second export of the same file lands as
    # "name-2.ext". One sitting next to an identically sized "name.ext" means the
    # incremental state was lost and the archive is growing a copy of itself per run.
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return @() }
    $dupes = @()
    foreach ($f in Get-ChildItem -LiteralPath $Path -Recurse -File -Force -ErrorAction SilentlyContinue) {
        if ($f.Name -notmatch '^(.+)-([2-9]|\d\d+)(\.[^.]*)?$') { continue }
        $original = Join-Path $f.DirectoryName ($Matches[1] + $Matches[3])
        if (-not (Test-Path -LiteralPath $original)) { continue }
        # Same stem and same byte count - a re-export, not a file the sender happened
        # to call "photo-2.jpg". Several attachments in one message also get numbered,
        # but those differ in size.
        if ((Get-Item -LiteralPath $original).Length -eq $f.Length) { $dupes += $f.FullName }
    }
    $dupes
}

function Test-ContentCovered {
    # Every file under Reference must exist somewhere under Candidate, by SHA-256.
    # Paths are deliberately ignored: -i deduplicates across conversations, so the
    # same bytes legitimately move to a different directory. Returns the files whose
    # content is nowhere in Candidate - i.e. actual loss.
    param([string]$Reference, [string]$Candidate)
    $have = @{}
    foreach ($f in Get-ChildItem -LiteralPath $Candidate -Recurse -File -Force -ErrorAction SilentlyContinue) {
        if ($f.Name -eq ".incremental") { continue }
        $have[(Get-FileHash -LiteralPath $f.FullName -Algorithm SHA256).Hash] = $true
    }
    $lost = @()
    foreach ($f in Get-ChildItem -LiteralPath $Reference -Recurse -File -Force -ErrorAction SilentlyContinue) {
        if (-not $have.ContainsKey((Get-FileHash -LiteralPath $f.FullName -Algorithm SHA256).Hash)) {
            $lost += $f.FullName
        }
    }
    $lost
}

# --- preflight ------------------------------------------------------------------
if (-not (Test-Path -LiteralPath $SigtopExe)) {
    Log "sigtop not found: $SigtopExe" "ERROR"
    exit 1
}
$sigDirEffective = if ($SignalDir) { $SignalDir } else { Join-Path $env:APPDATA "Signal" }
if (-not (Test-Path -LiteralPath $sigDirEffective)) {
    Log "Signal Desktop directory not found: $sigDirEffective" "ERROR"
    Log "Signal Desktop must be installed and have been run at least once on this machine." "ERROR"
    exit 1
}
Log "sigtop:   $SigtopExe"
Log "Signal:   $sigDirEffective"
Log "Output:   $OutDir"
Log ("Messages: {0}" -f $(if ($SkipMessages) { "skipped" } else { $MessageFormat -join ", " }))
if ($WhatIf) { Log "DRY RUN - nothing will be written." }
if (Get-Process -Name Signal -ErrorAction SilentlyContinue) {
    Log "Signal Desktop is running. That is fine - sigtop only reads the database."
}

if (-not $WhatIf -and -not (Test-Path -LiteralPath $OutDir)) {
    New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
}

# --- messages -------------------------------------------------------------------
# sigtop refuses to overwrite an existing transcript ("open <file>: The file exists")
# and fails the whole command, so each format is exported into an empty staging
# directory and moved over the destination afterwards. Transcripts for conversations
# that no longer exist in Signal are left alone rather than swept away: additive.
if (-not $SkipMessages) {
    foreach ($fmt in $MessageFormat) {
        $dest  = Join-Path $OutDir $msgDirs[$fmt]
        $stage = Join-Path $stagingDir $msgDirs[$fmt]
        Log "--- Messages ($fmt) -> $dest ---"
        if (-not $WhatIf) {
            Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
            New-Item -ItemType Directory -Path $stage -Force | Out-Null
            if (-not (Test-Path -LiteralPath $dest)) { New-Item -ItemType Directory -Path $dest -Force | Out-Null }
        }
        $r = Invoke-Sigtop @("export-messages", "-f", $fmt, $stage)
        if (-not (Report-Sigtop -Result $r -What "export-messages -f $fmt")) { continue }
        if ($WhatIf) { continue }

        $written = @(Get-ChildItem -LiteralPath $stage -File -Force -ErrorAction SilentlyContinue)
        if ($written.Count -eq 0) {
            Log "export-messages produced no transcripts - the existing ones are untouched." "ERROR"
            continue
        }
        foreach ($f in $written) {
            Move-Item -LiteralPath $f.FullName -Destination (Join-Path $dest $f.Name) -Force
        }
        Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
        $m = Measure-Tree $dest
        Log ("{0}: {1} conversation(s) rewritten, {2} file(s) / {3} MB in place" -f `
            $msgDirs[$fmt], $written.Count, $m.Files, $m.MB)
    }
    if (-not $WhatIf) { Remove-Item -LiteralPath $stagingDir -Recurse -Force -ErrorAction SilentlyContinue }
}

# --- attachments ----------------------------------------------------------------
if (-not $SkipAttachments) {
    Log "--- Attachments -> $attachDir ---"
    $before   = Measure-Tree $attachDir
    $hasFiles = $before.Files -gt 0
    $seeded   = Test-Path -LiteralPath $stateFile
    $parked   = ""

    # The guard. An attachments directory with files but no .incremental was exported
    # without -i, so sigtop has no record of it and would write a second copy of all
    # of it. Refuse rather than silently double the archive.
    if ($hasFiles -and -not $seeded) {
        if (-not $SeedIncremental) {
            Log "" "ERROR"
            Log ("{0} holds {1} file(s) / {2} MB but has no .incremental state file." -f `
                $attachDir, $before.Files, $before.MB) "ERROR"
            Log "It was exported without -i, so sigtop does not know it exists and this run would" "ERROR"
            Log "write a second copy of every attachment as 'name-2.ext'. Re-run once with" "ERROR"
            Log "-SeedIncremental to move it aside and re-export it clean, or with" "ERROR"
            Log "-SkipAttachments to export messages only." "ERROR"
            exit 1
        }
        $stamp  = Get-Date -Format "yyyyMMdd-HHmmss"
        $parked = "$attachDir.preincremental-$stamp"
        Log ("Seeding: moving the un-tracked export aside to {0}" -f $parked)
        if (-not $WhatIf) { Move-Item -LiteralPath $attachDir -Destination $parked }
    }

    if (-not $WhatIf -and -not (Test-Path -LiteralPath $attachDir)) {
        New-Item -ItemType Directory -Path $attachDir -Force | Out-Null
    }

    # -i: skip attachments already recorded in .incremental (and duplicate content).
    # -m: set each file's mtime from the time of its message, not the time of this run.
    $r = Invoke-Sigtop @("export-attachments", "-i", "-m", $attachDir)
    if (Report-Sigtop -Result $r -What "export-attachments") {
        $after = Measure-Tree $attachDir
        Log ("attachments: {0} file(s) / {1} MB  ({2:+#;-#;0} file(s), {3:+#.#;-#.#;0} MB this run)" -f `
            $after.Files, $after.MB, ($after.Files - $before.Files), ($after.MB - $before.MB))

        if (-not $WhatIf -and -not (Test-Path -LiteralPath $stateFile)) {
            Log "sigtop wrote no .incremental file - the next run would duplicate everything." "WARN"
        }
        $dupes = Get-DuplicateSuffixed $attachDir
        if ($dupes.Count -gt 0) {
            Log ("{0} attachment(s) look like re-exported duplicates of a file already here:" -f $dupes.Count) "WARN"
            foreach ($d in ($dupes | Select-Object -First 10)) { Log "  duplicate: $d" "WARN" }
            if ($dupes.Count -gt 10) { Log ("  ... and {0} more" -f ($dupes.Count - 10)) "WARN" }
            Log "That means the .incremental state was lost. Delete them and check it is being kept." "WARN"
        }

        # After seeding, prove the old copy is redundant before anyone deletes it. A
        # lower file count is expected - -i writes duplicate content once - so the
        # check is by content hash, not by path or by count.
        if ($parked -and -not $WhatIf) {
            Log "Verifying the parked copy is fully covered by the new export (by SHA-256)..."
            $lost = Test-ContentCovered -Reference $parked -Candidate $attachDir
            $old  = Measure-Tree $parked
            if ($lost.Count -eq 0) {
                Log ("All {0} file(s) in the parked copy are present in the new export." -f $old.Files)
                Log ("{0} fewer file(s) here is -i storing duplicate content once." -f ($old.Files - $after.Files + 1))
                Log ("Safe to delete: {0}" -f $parked)
            } else {
                Log ("{0} file(s) in the parked copy are NOT in the new export. Do not delete it." -f $lost.Count) "WARN"
                foreach ($l in ($lost | Select-Object -First 10)) { Log "  only in parked copy: $l" "WARN" }
                if ($lost.Count -gt 10) { Log ("  ... and {0} more" -f ($lost.Count - 10)) "WARN" }
            }
        }
    }
}

# --- summary --------------------------------------------------------------------
Log "===== SUMMARY ====="
$total = Measure-Tree $OutDir
Log ("{0}: {1} file(s), {2} MB total" -f $OutDir, $total.Files, $total.MB)
if ($script:warnings.Count -gt 0) { Log ("{0} warning(s) above." -f $script:warnings.Count) }
if ($script:errors.Count -gt 0) {
    Log ("FAILED with {0} error(s)." -f $script:errors.Count) "ERROR"
    exit 1
}
Log ("Done.{0}" -f $(if ($WhatIf) { " (DRY RUN - no changes made)" } else { "" }))
exit 0
