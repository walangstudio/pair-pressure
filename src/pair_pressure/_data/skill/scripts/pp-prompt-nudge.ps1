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

try { $root = Get-Content -Raw -LiteralPath $uf | ConvertFrom-Json } catch { return }
$key = $env:PAIR_PRESSURE_SESSION_ID
if (-not $key) { $key = '__shared__' }
$u = $null
$legacyFlat = $false
if ($root.PSObject.Properties.Match('count').Count -gt 0 -and
    $root.PSObject.Properties.Match('__shared__').Count -eq 0) {
    $legacyFlat = $true
    if ($key -eq '__shared__') { $u = $root }
} elseif ($root.PSObject.Properties.Match($key).Count -gt 0) {
    $u = $root.$key
}
if (-not $u) { return }

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

# Ack THIS bucket only so other sessions keep their badges.
try {
    $now = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    if ($legacyFlat) {
        # migrate-in-place: wrap legacy as __shared__ and reset it
        $obj = @{ '__shared__' = @{ count = 0; latest = $null; updated_at = $now } }
    } else {
        # rewrite full root with this bucket zeroed, others preserved
        $obj = @{}
        foreach ($p in $root.PSObject.Properties) {
            $obj[$p.Name] = $p.Value
        }
        $obj[$key] = @{ count = 0; latest = $null; updated_at = $now }
    }
    Set-Content -LiteralPath $uf -Encoding utf8 -Value ($obj | ConvertTo-Json -Depth 6 -Compress)
} catch {}
