# pair-pressure statusline (0 LLM tokens).
# Claude Code renders our stdout at the bottom; the model never sees it.
# Composable: if another statusLine was configured before pp wired itself, we
# run that prior command (its string is saved in settings.json
# `_pp_prev_statusline`), feed it the same session JSON on stdin, and APPEND
# the pp badge after its output -- so other statusline plugins keep working.
# A failure in the prior command can never break our line (fails to empty).
$ErrorActionPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = New-Object Text.UTF8Encoding $false } catch {}

# Buffer stdin ONCE (it can only be read once). Claude Code feeds session JSON;
# we don't use it ourselves, but the prior statusline command might.
$stdin = ''
try { $stdin = [Console]::In.ReadToEnd() } catch {}

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

# Compose the pp badge. Silent when nothing to report and online (lets the
# user reclaim screen). Always shows when offline so the mode is visible.
$parts = @('pp')
if ($offline) { $parts += '(offline)' }
if ($count -gt 0) {
    $detail = ''
    if ($who -and $where) { $detail = " $who $where" }
    elseif ($who)          { $detail = " $who" }
    elseif ($where)        { $detail = " $where" }
    $parts += ("$count new$detail")
}
$ppBadge = ''
if ($count -gt 0 -or $offline) {
    $ppBadge = '[' + ($parts -join ' ') + ']'
}

# Run the previously-configured statusline (if any) and prepend its output, so
# pp composes with other plugins instead of replacing them.
$prevOut = ''
$prev = $null
$settings = Join-Path $base '.claude\settings.json'
if (Test-Path $settings) {
    try {
        $sj = Get-Content -Raw -LiteralPath $settings | ConvertFrom-Json
        if ($sj.PSObject.Properties.Match('_pp_prev_statusline').Count -gt 0) {
            $prev = [string]$sj._pp_prev_statusline
        }
    } catch {}
}
if ($prev -and $prev.Trim()) {
    # Run the prior command verbatim from a temp .cmd file. This survives
    # quoted paths with spaces (which break `cmd /c "<cmd>"` and Windows
    # PowerShell's native-arg quoting) and forwards the buffered session JSON
    # on stdin so stdin-driven statuslines still work.
    $cf = Join-Path ([System.IO.Path]::GetTempPath()) "pp_prev_$PID.cmd"
    try {
        Set-Content -LiteralPath $cf -Value $prev -Encoding Default
        $prevOut = ($stdin | & $env:ComSpec '/q' '/c' $cf 2>$null | Out-String)
        $prevOut = $prevOut.TrimEnd("`r", "`n")
    } catch { $prevOut = '' }
    finally { Remove-Item -LiteralPath $cf -Force -ErrorAction SilentlyContinue }
}

if ($prevOut -and $ppBadge) {
    Write-Output ($prevOut + ' ' + $ppBadge)
} elseif ($prevOut) {
    Write-Output $prevOut
} elseif ($ppBadge) {
    Write-Output $ppBadge
} else {
    # Nothing to say; emit empty line (Claude Code accepts blank statuslines).
    Write-Output ''
}
