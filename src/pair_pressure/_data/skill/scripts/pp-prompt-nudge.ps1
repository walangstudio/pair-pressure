# pair-pressure UserPromptSubmit nudge (OPT-IN; INCURS TOKEN COST).
# Claude Code injects this script's stdout into the model context. We emit
# ONE short line only when there are unread pair-pressure messages, then
# clear the counter so it fires once per batch (~15-25 tokens, only when
# there is news). Nothing is printed when there is nothing new (0 tokens).
# No `pp`/python spawn. Enable/disable via `pp watch wire [--nudge] [--undo]`.
$ErrorActionPreference = 'SilentlyContinue'
[void]([Console]::In.ReadToEnd())

$base = $env:USERPROFILE
if (-not $base) { $base = $HOME }
$uf = Join-Path (Join-Path $base '.pair-pressure') 'unread.json'
if (-not (Test-Path $uf)) { return }

try { $u = Get-Content -Raw -LiteralPath $uf | ConvertFrom-Json } catch { return }
$c = 0
try { $c = [int]$u.count } catch {}
if ($c -le 0) { return }

$who = 'someone'; $where = ''
if ($u.latest) {
    if ($u.latest.author)  { $who = $u.latest.author }
    if ($u.latest.channel) { $where = " in #$($u.latest.channel)" }
}
if ($c -eq 1) {
    Write-Output "[pair-pressure] 1 new message from $who$where - run /pp-chat:read to view"
} else {
    Write-Output "[pair-pressure] $c new messages (latest from $who$where) - run /pp-chat:read to view"
}

# Ack so the nudge fires once per batch (auto-clears, like reading does).
try {
    $now = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    Set-Content -LiteralPath $uf -Encoding utf8 -Value ('{"count":0,"latest":null,"updated_at":"' + $now + '"}')
} catch {}
