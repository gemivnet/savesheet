<#
  tar_excludes.ps1 - what glacier_backup.ps1 leaves out of the yearly archive, and how it
  turns that list into `tar --exclude` patterns for each archive.

  The list is the exclude list of the files_to_google_drive job in backup_config.yaml,
  verbatim, so the Glacier tar leaves out the same credentials and Claude transcripts that
  Drive does (wiki Security Review S1). Paths are relative to the source root (G:\Files),
  rclone filter syntax: a leading / anchors at the root, ** crosses folders.
  KEEP THE TWO LISTS IN STEP: a change to one is a change to the other.

  Why this matters more here than on Drive: a sealed bucket (SealedArchive policy) cannot be
  edited by anyone but the AWS root user, so a secret that goes in stays in for good.
  backup-2026-07-12 and backup-2024-07 were sealed with theirs still inside.

  Windows' tar is bsdtar. Its --exclude patterns are not anchored, `*` matches `/`, and a
  pattern that names a folder drops everything under it. Each archive is made from the
  parent of its folder, so its entries start with the folder's own name; Get-TarExcludes
  rewrites every pattern that applies to that folder to start there.
#>

$script:GlacierExcludes = @(
    '/Server Backup/**/.env'
    '/Server Backup/**/*.env'
    '/Server Backup/**/*.pem'
    '/Server Backup/**/*.key'
    '/Server Backup/**/.*_token'
    '/Server Backup/**/.credentials.json'
    '/Server Backup/**/credentials.json'
    '/Server Backup/**/token*.json'
    '/Server Backup/**/rclone.conf'
    '/Server Backup/**/portainer.db'
    '/Server Backup/configs/portainer-backups/**'
    '/Server Backup/configs/callsheet/secrets/**'
    '/Server Backup/configs/w70b-provision/**'
    '/Server Backup/configs/claude/projects/**'
    '/Server Backup/configs/claude/file-history/**'
    '/Server Backup/configs/claude/paste-cache/**'
    '/Server Backup/configs/claude/history*.jsonl'
    '/Backups/home_assistant_backup_emergency_kit*'
    '/Backups/Shelby reMarkable/home-root/.local/share/remarkable/user-auth/**'
)

function Get-TarExcludes {
    # $RelPath: the folder being tarred, relative to the source root, with / separators
    # ("Server Backup", "Backups/Shelby reMarkable"). Returns the patterns for its tar.
    param([string]$RelPath, [string[]]$Patterns = $script:GlacierExcludes)
    $rsegs = @($RelPath.Trim('/') -split '/')
    $leaf  = $rsegs[-1]
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($p in $Patterns) {
        $psegs = @($p.Trim('/') -split '/')
        $i = 0
        while ($i -lt $rsegs.Count -and $i -lt $psegs.Count -and $psegs[$i] -ne '**') {
            if ($psegs[$i] -ine $rsegs[$i]) { break }
            $i++
        }
        if ($i -lt $psegs.Count -and $psegs[$i] -eq '**') {
            # ** swallows whatever is left of the folder's own path, then the remainder applies at any depth
            $rest = $psegs[($i + 1)..($psegs.Count - 1)] -join '/'
            $out.Add("$leaf/$rest")
            $out.Add("$leaf/*/$rest")
        } elseif ($i -eq $rsegs.Count -and $i -lt $psegs.Count) {
            $rest = @($psegs[$i..($psegs.Count - 1)])
            if ($rest[-1] -eq '**') { $rest = @($rest[0..($rest.Count - 2)]) }
            if ($rest.Count -eq 0) { continue }
            $joined = $rest -join '/'
            if ($joined.Contains('**')) {
                # a ** in the middle: zero folders, or any number (bsdtar's * crosses /)
                $out.Add("$leaf/" + $joined.Replace('/**/', '/'))
                $out.Add("$leaf/" + $joined.Replace('/**/', '/*/'))
            } else {
                $out.Add("$leaf/$joined")
            }
        }
    }
    return $out.ToArray()
}

function Test-ExcludedFile {
    # $RelFile: a loose file, relative to the source root, with / separators. Loose files are
    # uploaded raw (no tar), so they need the same test done here.
    param([string]$RelFile, [string[]]$Patterns = $script:GlacierExcludes)
    $f = $RelFile.Trim('/')
    foreach ($p in $Patterns) {
        $pat = $p.Trim('/')
        if ($pat.EndsWith('/**')) { $pat = $pat.Substring(0, $pat.Length - 3) + '/*' }
        $zero = $pat.Replace('/**/', '/')
        $many = $pat.Replace('/**/', '/*/')
        if ($f -like $zero -or $f -like $many) { return $true }
    }
    return $false
}
