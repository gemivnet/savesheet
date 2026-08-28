<#
  rcu_export.ps1 - Export every document off a reMarkable tablet with RCU-CLI.

  RCU has no command-line flag for its whole-device Snapshot; the CLI only speaks
  in individual documents. So this walks the document list and pulls each one to
  its own file, which is the scriptable equivalent of "back up my notebooks".

  * Incremental: a manifest next to the exports records each document's
    modification date, so unchanged documents are skipped on the next run.
  * Additive: documents deleted on the tablet are LEFT in place locally. This is
    a backup, not a mirror - the whole point is to still have them.
  * Foldered: RCU's CLI cannot report which collection a document lives in, so
    the tree is rebuilt from the .metadata files in the rclone backup of
    /home/root (see -XochitlDir). Without it the export is flat.
  * Formats: rmdoc (reMarkable archive, restorable, keeps ink), rmn (RCU's own
    archive), or a rendered PDF (pdf-v vector, pdf-b bitmap, pdf-rm web UI).

  Two prerequisites, both easy to miss:
    1. A saved connection preset in the RCU GUI. --autoconnect reuses the
       last-used preset and there is no way to pass a host on the CLI.
    2. For -Format rmdoc and pdf-rm only: the tablet's USB web interface must be
       ON (Settings - Storage - USB web interface). Those two formats fetch over
       HTTP on port 80 rather than SSH, and fail with "Connection refused" for
       every single document when it is off.
#>
param(
    [string]$RcuExe = "rcu-5.1.1-windows\RCU-CLI.exe",
    [string]$OutDir = "G:\Files\Backups\Shelby reMarkable\Documents",
    [ValidateSet("rmdoc", "rmn", "pdf-v", "pdf-b", "pdf-rm")]
    [string]$Format = "rmdoc",
    # Metadata source for the folder tree. Defaults to the rclone job's output.
    [string]$XochitlDir = "G:\Files\Backups\Shelby reMarkable\home-root\.local\share\remarkable\xochitl",
    # Give up early rather than spending six seconds each on 161 doomed documents.
    [int]$MaxConsecutiveFailures = 5,
    # Deleted-but-not-purged documents export to _Trash by default: this is a backup,
    # and the tablet's trash is exactly where an accidental deletion goes.
    [switch]$SkipTrash,
    [switch]$WhatIf                          # DRY RUN - lists what would export, writes nothing
)

$ErrorActionPreference = "Stop"
$here = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not [System.IO.Path]::IsPathRooted($RcuExe)) { $RcuExe = Join-Path $here $RcuExe }
$manifestPath = Join-Path $OutDir ".rcu-manifest.json"

# Per-format: the RCU flag that downloads a document, the extension it produces,
# and whether it needs the tablet's HTTP web interface rather than SSH.
$formats = @{
    "rmdoc"  = @{ Flag = "--download-rmdoc"; Ext = ".rmdoc"; WebUI = $true  }
    "rmn"    = @{ Flag = "--download-doc";   Ext = ".rmn";   WebUI = $false }
    "pdf-v"  = @{ Flag = "--export-pdf-v";   Ext = ".pdf";   WebUI = $false }
    "pdf-b"  = @{ Flag = "--export-pdf-b";   Ext = ".pdf";   WebUI = $false }
    "pdf-rm" = @{ Flag = "--export-pdf-rm";  Ext = ".pdf";   WebUI = $true  }
}
$flag  = $formats[$Format].Flag
$ext   = $formats[$Format].Ext
$webUI = $formats[$Format].WebUI

$script:exported = 0
$script:skipped  = 0
$script:failures = @()
# --export-pdf-v returns 1 on every document while writing a perfectly good PDF, so this
# is a tally rather than a per-document warning: 161 identical WARN lines bury real ones.
$script:exitQuirk = 0
$script:tiny      = @()
$script:shortPages = @()

function Log {
    param([string]$Msg, [string]$Level = "INFO")
    Write-Host ("{0} [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level, $Msg)
}

function Get-SafeSegment {
    # Document and folder names are free text; make them path segments Windows accepts.
    param([string]$Name)
    $safe = [string]$Name -replace '[\\/:*?"<>|]', '_'
    $safe = ($safe -replace '\s+', ' ').Trim().TrimEnd('.')
    if (-not $safe) { $safe = "Untitled" }
    if ($safe.Length -gt 120) { $safe = $safe.Substring(0, 120).Trim() }
    $safe
}

function Get-MetadataMap {
    # RCU's --list-documents gives no parent, so the collection tree comes from the
    # raw xochitl metadata that the rclone job already pulls down.
    param([string]$Dir)
    $map = @{}
    if (-not $Dir -or -not (Test-Path -LiteralPath $Dir)) { return $map }
    $script:metaSkipped = 0
    foreach ($f in Get-ChildItem -LiteralPath $Dir -Filter *.metadata -File -ErrorAction SilentlyContinue) {
        # Bare UUID only: rclone's --suffix versioning parks old copies next to the
        # live file as "<uuid>.<timestamp>.metadata".
        if ($f.BaseName -notmatch '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$') { continue }
        try { $m = Get-Content -LiteralPath $f.FullName -Raw -ErrorAction Stop | ConvertFrom-Json } catch { $script:metaSkipped++; continue }
        $map[$f.BaseName] = [pscustomobject]@{
            Name   = $m.visibleName
            Parent = $m.parent
            Type   = $m.type
        }
    }
    $map
}

function Get-PageCount {
    # Pages recorded on the tablet, from the xochitl .content sidecar. Returns 0 when
    # unknown, which the caller treats as "cannot verify" rather than "zero pages".
    param([string]$Dir, [string]$DocId)
    if (-not $Dir) { return 0 }
    $f = Join-Path $Dir "$DocId.content"
    if (-not (Test-Path -LiteralPath $f)) { return 0 }
    try { $c = Get-Content -LiteralPath $f -Raw -ErrorAction Stop | ConvertFrom-Json } catch { return 0 }
    if ($c.pageCount -gt 0) { return [int]$c.pageCount }
    if ($c.pages) { return @($c.pages).Count }
    if ($c.cPages -and $c.cPages.pages) { return @($c.cPages.pages).Count }
    0
}

function Get-PdfPageCount {
    # Deliberately crude: enough to catch a one-page render of a long notebook. Returns 0
    # when the page tree sits inside a compressed object stream, i.e. cannot be counted
    # without a real PDF library.
    param([string]$Path)
    try { $raw = [System.IO.File]::ReadAllBytes($Path) } catch { return 0 }
    $text = [System.Text.Encoding]::Latin1.GetString($raw)
    ([regex]::Matches($text, '/Type\s*/Page[^s]')).Count
}

function Get-DocFolder {
    # Walk parent links up to the root, building "Folder\Subfolder".
    param([hashtable]$Map, [string]$DocId)
    $node = $Map[$DocId]
    if (-not $node) { return "" }
    $parent = $node.Parent
    if ($parent -eq "trash") { return "_Trash" }
    $parts = @()
    $guard = 0
    while ($parent -and $parent -ne "" -and $parent -ne "trash" -and $guard -lt 32) {
        $p = $Map[$parent]
        if (-not $p) { break }
        $parts = ,(Get-SafeSegment $p.Name) + $parts
        $parent = $p.Parent
        $guard++
    }
    if ($parts.Count -eq 0) { return "" }
    $parts -join "\"
}

if (-not (Test-Path -LiteralPath $RcuExe)) {
    Log "RCU-CLI not found: $RcuExe" "ERROR"
    exit 1
}
Log "RCU-CLI:  $RcuExe"
Log "Output:   $OutDir"
Log "Format:   $Format ($flag)"
if ($webUI) { Log "This format fetches over the tablet's USB web interface (port 80), not SSH." }
if ($WhatIf) { Log "DRY RUN - nothing will be written." }

# --- folder tree ---------------------------------------------------------------
$meta = Get-MetadataMap -Dir $XochitlDir
if ($meta.Count -gt 0) {
    Log ("Folder tree: {0} metadata entries from {1}" -f $meta.Count, $XochitlDir)
} else {
    Log "No xochitl metadata found - exporting flat. Run the rclone reMarkable job first to get folders." "WARN"
}

# --- document list --------------------------------------------------------------
Log "Connecting to tablet (--autoconnect) and listing documents..."
$ErrorActionPreference = "Continue"
$listing = & $RcuExe --autoconnect --cli --list-documents 2>&1
$listExit = $LASTEXITCODE
$ErrorActionPreference = "Stop"

if ($listExit -ne 0) {
    $listing | ForEach-Object { Log "    rcu> $_" }
    Log "--list-documents failed (exit $listExit). Is the tablet plugged in, and has a connection preset been saved in the RCU GUI?" "ERROR"
    exit 1
}

# Tab-separated: doc_id, name, modification date, filetype. Anything that does not
# start with a UUID is a banner or progress line, not a document.
$docs = @()
foreach ($line in $listing) {
    $fields = ([string]$line) -split "`t"
    if ($fields.Count -lt 3) { continue }
    $docId = $fields[0].Trim()
    if ($docId -notmatch '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-') { continue }
    $docs += [pscustomobject]@{
        Id       = $docId
        Name     = $fields[1].Trim()
        Modified = $fields[2].Trim()
        Type     = if ($fields.Count -gt 3) { $fields[3].Trim() } else { "" }
    }
}

if ($docs.Count -eq 0) {
    $listing | ForEach-Object { Log "    rcu> $_" }
    Log "No documents parsed from --list-documents output." "ERROR"
    exit 1
}
Log ("Found {0} document(s) on the tablet." -f $docs.Count)

# Why each document resolves the way it does. A document at the root because the tablet
# says so is fine; one at the root because its metadata or its parent is missing is a
# broken backup, and the two are indistinguishable in the output tree.
if ($meta.Count -gt 0) {
    $atRoot = $noMeta = $inTrash = $brokenChain = $foldered = 0
    $missingIds = @()
    $missingParents = @()
    foreach ($d in $docs) {
        $node = $meta[$d.Id]
        if (-not $node) { $noMeta++; $missingIds += $d.Name; continue }
        $par = $node.Parent
        if ($par -eq "trash") { $inTrash++; continue }
        if (-not $par) { $atRoot++; continue }
        if (-not $meta[$par]) { $brokenChain++; $missingParents += ("{0} -> {1}" -f $d.Name, $par); continue }
        $foldered++
    }
    Log ("Placement: {0} foldered, {1} at tablet root, {2} in trash, {3} with no metadata, {4} with a missing parent." -f `
        $foldered, $atRoot, $inTrash, $noMeta, $brokenChain)
    if ($script:metaSkipped -gt 0) { Log ("{0} metadata file(s) failed to parse." -f $script:metaSkipped) "WARN" }
    foreach ($m in $missingIds     | Select-Object -First 5) { Log "  no metadata:     $m" "WARN" }
    foreach ($m in $missingParents | Select-Object -First 5) { Log "  missing parent:  $m" "WARN" }
}

# --- manifest of what we already hold --------------------------------------------
$manifest = @{}
if (Test-Path -LiteralPath $manifestPath) {
    try {
        (Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json).PSObject.Properties |
            ForEach-Object { $manifest[$_.Name] = $_.Value }
        Log ("Manifest: {0} document(s) from a previous run." -f $manifest.Count)
    } catch {
        Log "Manifest unreadable, re-exporting everything: $_" "WARN"
        $manifest = @{}
    }
} else {
    Log "No manifest yet - this is a first full export."
}

if (-not $WhatIf -and -not (Test-Path -LiteralPath $OutDir)) {
    New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
}

# --- export -----------------------------------------------------------------------
$consecutiveFailures = 0
foreach ($doc in $docs) {
    $folder   = Get-DocFolder -Map $meta -DocId $doc.Id
    # The id prefix keeps two same-named notebooks apart and makes the file traceable.
    $fileName = "{0} [{1}]{2}" -f (Get-SafeSegment $doc.Name), $doc.Id.Substring(0, 8), $ext
    $relPath  = if ($folder) { Join-Path $folder $fileName } else { $fileName }
    $outPath  = Join-Path $OutDir $relPath
    $prev     = $manifest[$doc.Id]

    # Skip only when the tablet's modification date matches what we recorded AND the
    # file is still there - a deleted local file must come back.
    if ($prev -and $prev.modified -eq $doc.Modified -and $prev.format -eq $Format -and (Test-Path -LiteralPath $outPath)) {
        $script:skipped++
        continue
    }

    if ($SkipTrash -and $folder -eq "_Trash") {
        $script:skipped++
        continue
    }

    Log ("Exporting: {0}{1} ({2})" -f $(if ($folder) { "$folder\" } else { "" }), $doc.Name, $doc.Type)
    if ($WhatIf) { $script:exported++; continue }

    $parent = Split-Path -Parent $outPath
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }

    $ErrorActionPreference = "Continue"
    $out = & $RcuExe --autoconnect --cli $flag $doc.Id $outPath 2>&1
    $code = $LASTEXITCODE
    $ErrorActionPreference = "Stop"

    # Trust the artefact over the exit code: RCU has been known to return non-zero
    # after writing a perfectly good file.
    $size  = if (Test-Path -LiteralPath $outPath) { (Get-Item -LiteralPath $outPath).Length } else { 0 }
    $wrote = $size -gt 0

    if (-not $wrote) {
        $out | ForEach-Object { Log "    rcu> $_" }
        Log ("FAILED: {0} (exit {1})" -f $doc.Name, $code) "ERROR"
        $script:failures += $doc.Name
        $consecutiveFailures++

        if ($webUI -and ($out -match 'Connection refused|10\.11\.99\.1:80|save_rmwebui_file')) {
            Log "" "ERROR"
            Log "-Format $Format fetches from the tablet's USB web interface, and it is refusing connections." "ERROR"
            Log "Turn it on: Settings - Storage - USB web interface. Or re-run with -Format rmn / pdf-v, which use SSH." "ERROR"
            exit 1
        }
        if ($consecutiveFailures -ge $MaxConsecutiveFailures -and $script:exported -eq 0) {
            Log "" "ERROR"
            Log ("Giving up after {0} consecutive failures with nothing exported - this is a setup problem, not bad luck." -f $consecutiveFailures) "ERROR"
            exit 1
        }
        continue
    }
    if ($code -ne 0) {
        if ($script:exitQuirk -eq 0) {
            Log ("{0}: RCU exited {1} but wrote the file; keeping it. Further occurrences are counted, not logged." -f $doc.Name, $code) "WARN"
        }
        $script:exitQuirk++
    }
    # A PDF or archive this small is a header and nothing else - a truncated render that
    # would otherwise be recorded in the manifest and never retried.
    if ($size -lt 1024) {
        Log ("{0}: only {1} bytes - likely truncated." -f $doc.Name, $size) "WARN"
        $script:tiny += $doc.Name
    } elseif ($ext -eq ".pdf") {
        $want = Get-PageCount -Dir $XochitlDir -DocId $doc.Id
        $got  = Get-PdfPageCount -Path $outPath
        if ($want -gt 1 -and $got -gt 0 -and $got -lt $want) {
            Log ("{0}: {1} page(s) exported but the notebook has {2}." -f $doc.Name, $got, $want) "WARN"
            $script:shortPages += ("{0} ({1}/{2})" -f $doc.Name, $got, $want)
        }
    }

    $consecutiveFailures = 0
    $manifest[$doc.Id] = [pscustomobject]@{
        name     = $doc.Name
        modified = $doc.Modified
        format   = $Format
        file     = $relPath
    }
    $script:exported++
    # Written after every document so an interrupted run keeps its progress.
    $manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $manifestPath -Encoding UTF8
}

# --- summary -----------------------------------------------------------------------
Log ("Done. Exported {0}, skipped {1} unchanged, {2} failed." -f $script:exported, $script:skipped, $script:failures.Count)
if ($script:exitQuirk -gt 0) {
    Log ("{0} document(s) were written despite a non-zero exit code from RCU." -f $script:exitQuirk)
}
if ($script:shortPages.Count -gt 0) {
    Log ("{0} PDF(s) have fewer pages than the notebook does:" -f $script:shortPages.Count) "WARN"
    foreach ($n in $script:shortPages) { Log "  short: $n" "WARN" }
}
if ($script:tiny.Count -gt 0) {
    Log ("{0} file(s) came out under 1 KB and are probably truncated:" -f $script:tiny.Count) "WARN"
    foreach ($n in $script:tiny) { Log "  suspect: $n" "WARN" }
}
if ($script:failures.Count -gt 0) {
    foreach ($f in $script:failures) { Log "  failed: $f" "ERROR" }
    exit 1
}
exit 0
