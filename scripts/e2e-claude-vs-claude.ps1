<#
.SYNOPSIS
  End-to-end test: drives two `claude --print` subprocesses through a
  multi-turn pair-pressure debate in one channel. Each agent reads the
  channel and posts a challenge to the latest post via /pp-chat:* commands.

.DESCRIPTION
  - Orchestrator registers both clones as servers (alice/bob) and seeds
    the debate post (not Claude).
  - Alternates alice (odd turns) and bob (even turns) through N turns.
  - Each turn gets a tightly-scripted prompt that pins identity, server,
    and channel explicitly so the agent can't drift.
  - Each turn runs in its own ephemeral `claude --print` invocation;
    state lives only in the pair-pressure git repos (which is the point).

.NOTES
  - v1.0 model: one repo = one server, flat channels, no threads. Both
    repos below must be schema-v3 clones of the SAME remote.
  - Requires `claude` and `pp` on PATH (or set $env:PATH before running).
  - Uses --dangerously-skip-permissions because the test runs against
    throwaway clones with no internet exposure.
  - Costs Claude subscription tokens -- ~30-60k per turn x Turns.
  - Default paths below are MACHINE-SPECIFIC -- override every -*Repo /
    -*Dir param on any box that isn't the original author's.
#>
param(
    [int]    $Turns       = 6,
    [string] $Topic       = "Should we adopt structured (JSON) logging?",
    [string] $Channel     = "general",
    # User-specific default; override on any other machine.
    [string] $AliceRepo   = "F:\opt\projs\ai\claude\pair-pressure-chat-alice",
    # User-specific default; override on any other machine.
    [string] $BobRepo     = "F:\opt\projs\ai\claude\pair-pressure-chat-bob",
    # User-specific default for the dir containing pp.exe; usually the venv's Scripts/.
    [string] $PpScripts   = "f:\opt\coding\python\default\Scripts",
    # User-specific default; any writable dir works.
    [string] $LogDir      = "F:\opt\projs\ai\claude\_pp_e2e_logs",
    [string] $Model       = "sonnet"
)

$ErrorActionPreference = 'Continue'  # claude.exe writes a stderr warning on stdin; 'Stop' would kill the loop.

# --- preflight ---
$env:PATH = "$PpScripts;$env:PATH"
foreach ($cmd in @('claude','pp','git')) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        throw "$cmd not on PATH"
    }
}
foreach ($repo in @($AliceRepo, $BobRepo)) {
    if (-not (Test-Path "$repo\.git")) {
        throw "$repo is not a git working tree"
    }
    $schema = Join-Path $repo ".pair-pressure\schema-version"
    if (-not (Test-Path $schema) -or (Get-Content $schema).Trim() -ne "3") {
        throw "$repo is not a schema-v3 chat repo (re-init with pp-init --force)"
    }
}
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# --- register both clones as servers (idempotent: add fails if present) ---
$servers = @{ 'alice' = $AliceRepo; 'bob' = $BobRepo }
foreach ($name in $servers.Keys) {
    $repo = $servers[$name]
    $url = (git -C $repo remote get-url origin 2>$null)
    if (-not $url) { $url = "file://$repo" }
    $env:PAIR_PRESSURE_AUTHOR = "orchestrator"
    pp server add $name $url --path $repo --no-clone 2>$null | Out-Null
}

# --- seed post (orchestrator-authored, NOT via Claude) ---
$env:PAIR_PRESSURE_AUTHOR = "orchestrator"
pp use alice "#$Channel" 2>$null | Out-Null
$seedBody = @"
DEBATE: $Topic

This channel hosts an automated multi-agent debate driven by the e2e test
harness. Two AI agents will alternate turns arguing opposing positions.
Each reply must challenge the most recent post: name a tradeoff,
counterexample, or hidden cost.
"@

Write-Host "[seed] posting debate seed to #$Channel..." -ForegroundColor Cyan
$seedJson = $seedBody | pp send --channel $Channel --via human --body-file -
$seed = $seedJson | ConvertFrom-Json
$seedId = $seed.post_id
Write-Host "[seed] post_id=$seedId" -ForegroundColor Green

# --- turn loop ---
$roles = @{
    'alice' = "argue FOR structured (JSON) logging -- operability, queryability, tooling"
    'bob'   = "argue AGAINST structured logging -- readability, debugging cost, migration risk"
}
$turnResults = @()

for ($i = 1; $i -le $Turns; $i++) {
    $name = if ($i % 2 -eq 1) { 'alice' } else { 'bob' }
    $env:PAIR_PRESSURE_AUTHOR = $name

    Write-Host "`n========== TURN $i / $Turns -- $name ==========" -ForegroundColor Cyan
    Write-Host "  position: $($roles[$name])" -ForegroundColor DarkGray

    $prompt = @"
You are "$name" in an automated multi-agent debate on pair-pressure.

Your assigned position: $($roles[$name])

Server: $name    Channel: $Channel

Do EXACTLY these steps in order. Do not skip, do not improvise.

1. Run: /pp-chat:use $name #$Channel
   This pins your server and channel.
2. Run: /pp-chat:read $Channel
   This pulls the latest channel state and shows you the posts.
3. Identify the MOST RECENT post (bottom of the feed) and note its short
   id handle (the 6-char id shown after the post). Read its argument.
4. Compose a 2-4 sentence reply that GENUINELY CHALLENGES that post from
   your assigned position. Be specific and concrete -- name a tradeoff,
   counterexample, or hidden cost. Do not be abstract.
5. Run: /pp-chat:send ai <your reply body>
   (The send must use --reply-to <short-id-from-step-3> so the reply
    chain is preserved -- pass it through to pp send.)

After posting, your final printed line MUST be:
TURN_DONE post_id=<the post_id from the send result>

That's the orchestrator's signal that you finished. Do not print anything
after that line.
"@

    $logFile = Join-Path $LogDir "turn_${i}_${name}.log"
    $startedAt = Get-Date
    Write-Host "  invoking claude --print (model=$Model)..." -ForegroundColor DarkGray

    # Pipe "" in to close stdin (claude --print otherwise waits 3s for stdin).
    # Note: NO --add-dir — passing the chat repo there caused claude to hang
    # indefinitely on startup (likely .claude/settings autodiscovery in the
    # added dir). pp accesses the repo via the server registry inside its own
    # Bash subprocess; the agent doesn't need file-tool access there.
    $output = "" | & claude `
        --print `
        --model $Model `
        --dangerously-skip-permissions `
        $prompt 2>&1 | Out-String

    $elapsed = (Get-Date) - $startedAt
    $output | Out-File -FilePath $logFile -Encoding utf8

    # Extract TURN_DONE line if present
    $turnDone = ($output -split "`n" | Where-Object { $_ -match 'TURN_DONE' } | Select-Object -Last 1)
    if (-not $turnDone) { $turnDone = "(no TURN_DONE found)" }

    Write-Host "  done in $([int]$elapsed.TotalSeconds)s -- $turnDone" -ForegroundColor Green
    Write-Host "  log: $logFile" -ForegroundColor DarkGray

    $turnResults += [PSCustomObject]@{
        Turn      = $i
        Author    = $name
        ElapsedS  = [int]$elapsed.TotalSeconds
        TurnDone  = $turnDone
        LogFile   = $logFile
    }
}

# --- final verification ---
Write-Host "`n========== FINAL CHANNEL STATE ==========" -ForegroundColor Cyan
$env:PAIR_PRESSURE_AUTHOR = "orchestrator"
pp use alice "#$Channel" 2>$null | Out-Null
pp pull | Out-Null
$readJson = pp read $Channel --limit 100 --no-pull
$view = $readJson | ConvertFrom-Json
$posts = @($view.posts | Where-Object { $_.id -ge $seedId })

Write-Host "where:  $($view.where)" -ForegroundColor White
Write-Host "posts:  $($posts.Count)  (expected: $($Turns + 1) -- seed + $Turns replies)"
Write-Host ""

$posts | ForEach-Object {
    $bodySnippet = ($_.body -replace "`n", " ")
    $bodySnippet = $bodySnippet.Substring(0, [Math]::Min(100, $bodySnippet.Length))
    $reply = if ($_.reply_to) { "->" + $_.reply_to.Substring($_.reply_to.Length - 6) } else { "      " }
    "{0}  {1,-12} {2} via={3,-12} {4}" -f $_.id, $_.author, $reply, $_.via, $bodySnippet
}

# --- summary ---
Write-Host "`n========== TURN SUMMARY ==========" -ForegroundColor Cyan
$turnResults | Format-Table -AutoSize

$expectedReplies = $Turns
$actualReplies   = $posts.Count - 1
$withReplyTo     = @($posts | Where-Object { $_.reply_to }).Count
$aliceReplies    = @($posts | Where-Object { $_.author -eq 'alice' }).Count
$bobReplies      = @($posts | Where-Object { $_.author -eq 'bob' }).Count

Write-Host "expected replies: $expectedReplies; actual: $actualReplies"
Write-Host "alice replies: $aliceReplies; bob replies: $bobReplies"
Write-Host "replies carrying reply-to: $withReplyTo"

if ($actualReplies -eq $expectedReplies) {
    Write-Host "`nE2E PASS -- all $Turns turns posted" -ForegroundColor Green
    exit 0
} else {
    Write-Host "`nE2E FAIL -- only $actualReplies/$expectedReplies replies landed" -ForegroundColor Red
    exit 1
}
