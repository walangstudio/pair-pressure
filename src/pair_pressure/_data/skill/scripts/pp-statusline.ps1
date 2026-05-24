# pair-pressure standalone statusline (0 LLM tokens).
# Claude Code renders our stdout at the bottom; the model never sees it.
# This script replaces whatever statusLine was configured (a copy of the
# previous command is saved in settings.json `_pp_prev_statusline` so
# `pp watch wire --undo` can restore it). No chaining, no subprocesses.
$ErrorActionPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = New-Object Text.UTF8Encoding $false } catch {}

# Drain stdin (Claude Code feeds session JSON; we don't currently use it).
[void]([Console]::In.ReadToEnd())

$base = $env:USERPROFILE
if (-not $base) { $base = $HOME }
$ppHome = Join-Path $base '.pair-pressure'

# offline state (machine-global config)
$offline = $false
$cfg = Join-Path $ppHome 'config.json'
if (Test-Path $cfg) {
    try { $offline = [bool](Get-Content -Raw -LiteralPath $cfg | ConvertFrom-Json).offline } catch {}
}

# unread badge (read this session's bucket; no pp/python spawn).
# Bucket key = $env:PAIR_PRESSURE_SESSION_ID or '__shared__'. Tolerates the
# legacy flat shape ({count,latest,updated_at}) by treating it as __shared__.
$count = 0; $who = $null; $where = $null
$uf = Join-Path $ppHome 'unread.json'
if (Test-Path $uf) {
    try {
        $root = Get-Content -Raw -LiteralPath $uf | ConvertFrom-Json
        $key = $env:PAIR_PRESSURE_SESSION_ID
        if (-not $key) { $key = '__shared__' }
        $u = $null
        if ($root.PSObject.Properties.Match('count').Count -gt 0 -and
            $root.PSObject.Properties.Match('__shared__').Count -eq 0) {
            # legacy flat
            if ($key -eq '__shared__') { $u = $root }
        } elseif ($root.PSObject.Properties.Match($key).Count -gt 0) {
            $u = $root.$key
        }
        if ($u) {
            $count = [int]$u.count
            if ($u.latest) {
                if ($u.latest.author)  { $who = $u.latest.author }
                if ($u.latest.channel) { $where = "#$($u.latest.channel)" }
            }
        }
    } catch {}
}

# Compose. Silent when nothing to report and online (lets the user reclaim
# screen). Always shows when offline so the mode is visible.
$parts = @('pp')
if ($offline) { $parts += '(offline)' }
if ($count -gt 0) {
    $detail = ''
    if ($who -and $where) { $detail = " $who $where" }
    elseif ($who)          { $detail = " $who" }
    elseif ($where)        { $detail = " $where" }
    $parts += ("$count new$detail")
}

if ($count -gt 0 -or $offline) {
    Write-Output ('[' + ($parts -join ' ') + ']')
} else {
    # Nothing to say; emit empty line (Claude Code accepts blank statuslines).
    Write-Output ''
}
