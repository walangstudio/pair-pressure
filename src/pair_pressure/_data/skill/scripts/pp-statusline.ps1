# pair-pressure statusline wrapper (0 LLM tokens).
# Claude Code feeds session JSON on stdin and renders our stdout; the model
# never sees it. We forward stdin to the PREVIOUS statusline command
# (recorded by `pp watch wire`) and append a [pp:N] badge when there are
# unread pair-pressure messages. No `pp`/python spawn in this hot path.
$ErrorActionPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = New-Object Text.UTF8Encoding $false } catch {}

$inp = [Console]::In.ReadToEnd()

$base = $env:USERPROFILE
if (-not $base) { $base = $HOME }
$ppHome = Join-Path $base '.pair-pressure'

# Chain to the user's original statusline command, if any.
$prevFile = Join-Path $ppHome 'statusline-prev.txt'
$prev = ''
if (Test-Path $prevFile) {
    $prev = (Get-Content -Raw -LiteralPath $prevFile -ErrorAction SilentlyContinue)
    if ($prev) { $prev = $prev.Trim() }
}
$line = ''
if ($prev) {
    try { $line = ($inp | & cmd /c $prev 2>$null | Out-String).TrimEnd("`r","`n") } catch { $line = '' }
}

# Append the unread badge (read the counter file directly; no subprocess).
$badge = ''
$uf = Join-Path $ppHome 'unread.json'
if (Test-Path $uf) {
    try {
        $u = Get-Content -Raw -LiteralPath $uf | ConvertFrom-Json
        $c = [int]$u.count
        if ($c -gt 0) { $badge = " [pp:$c]" }
    } catch {}
}

if ($line) { Write-Output ($line + $badge) }
elseif ($badge) { Write-Output $badge.Trim() }
