<#
.SYNOPSIS
  End-to-end test: drives two `claude --print` subprocesses through a
  multi-turn pair-pressure conversation. Each agent reads the thread and
  posts a contradiction-flavored reply via the /pp-chat:* slash commands.

.DESCRIPTION
  - Orchestrator seeds a fresh investigation thread (not Claude).
  - Alternates alice (odd turns) and bob (even turns) through N turns.
  - Each turn gets a tightly-scripted prompt that pins identity, position,
    channel, and thread_id explicitly so the agent can't drift.
  - Each turn runs in its own ephemeral `claude --print` invocation;
    state lives only in the pair-pressure git repo (which is the point).

.NOTES
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
}
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# --- seed thread (orchestrator-authored, NOT via Claude) ---
$env:PAIR_PRESSURE_REPO   = $AliceRepo
$env:PAIR_PRESSURE_AUTHOR = "orchestrator"
$seedBody = @"
## Context
$Topic

## Findings
- Each side has tradeoffs; no clear consensus yet.
- This thread is an automated multi-agent debate driven by the e2e test
  harness. Two AI agents will alternate turns arguing opposing positions.

## Open questions
- What's the strongest counter-argument to the previous reply?
- Where does the previous reply overstate its case?
"@

$titleSuffix = (Get-Date -Format "HHmmss")
$title = "E2E debate $titleSuffix"
Write-Host "[seed] creating thread '$title'..." -ForegroundColor Cyan
$seedJson = $seedBody | pp new-thread `
    --channel $Channel --title $title --kind investigation --body-file -
$seed = $seedJson | ConvertFrom-Json
$tid = $seed.thread_id
Write-Host "[seed] thread_id=$tid" -ForegroundColor Green

# --- turn loop ---
$roles = @{
    'alice' = "argue FOR structured (JSON) logging -- operability, queryability, tooling"
    'bob'   = "argue AGAINST structured logging -- readability, debugging cost, migration risk"
}
$repos = @{ 'alice' = $AliceRepo; 'bob' = $BobRepo }
$turnResults = @()

for ($i = 1; $i -le $Turns; $i++) {
    $name = if ($i % 2 -eq 1) { 'alice' } else { 'bob' }
    $env:PAIR_PRESSURE_REPO   = $repos[$name]
    $env:PAIR_PRESSURE_AUTHOR = $name

    Write-Host "`n========== TURN $i / $Turns -- $name ==========" -ForegroundColor Cyan
    Write-Host "  position: $($roles[$name])" -ForegroundColor DarkGray

    $prompt = @"
You are "$name" in an automated multi-agent investigation thread on pair-pressure.

Your assigned position: $($roles[$name])

Channel: $Channel
Thread ID: $tid

Do EXACTLY these steps in order. Do not skip, do not improvise.

1. Run the slash command: /pp-chat:read $tid
   This pulls the latest thread state and shows you all current posts.
2. Identify the MOST RECENT post (highest ordinal). Read its argument carefully.
3. Compose a 2-4 sentence reply that GENUINELY CHALLENGES that post from
   your assigned position. Cite the post you are responding to as [NNN]
   where NNN is the ordinal. Be specific and concrete -- name a tradeoff,
   counterexample, or hidden cost. Do not be abstract.
4. Run: /pp-chat:reply contradict <your reply body>
   (Use stance "contradict" unless you genuinely cannot disagree at all,
    in which case use "extend" with a sharpening point.)

After posting, your final printed line MUST be:
TURN_DONE reply_id=<ordinal>

That's the orchestrator's signal that you finished. Do not print anything
after that line.
"@

    $logFile = Join-Path $LogDir "turn_${i}_${name}.log"
    $startedAt = Get-Date
    Write-Host "  invoking claude --print (model=$Model)..." -ForegroundColor DarkGray

    # Pipe "" in to close stdin (claude --print otherwise waits 3s for stdin).
    # Note: NO --add-dir — passing the chat repo there caused claude to hang
    # indefinitely on startup (likely .claude/settings autodiscovery in the
    # added dir). pp accesses the repo via PAIR_PRESSURE_REPO env var inside
    # its own Bash subprocess; the agent doesn't need file-tool access there.
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
Write-Host "`n========== FINAL THREAD STATE ==========" -ForegroundColor Cyan
$env:PAIR_PRESSURE_REPO   = $AliceRepo
$env:PAIR_PRESSURE_AUTHOR = "orchestrator"
pp pull | Out-Null
$threadJson = pp read-thread --channel $Channel --thread $tid
$thread = $threadJson | ConvertFrom-Json

Write-Host "title:  $($thread.meta.title)" -ForegroundColor White
Write-Host "kind:   $($thread.meta.kind)"
Write-Host "status: $($thread.meta.status)"
Write-Host "posts:  $($thread.posts.Count)  (expected: $($Turns + 1) -- seed + $Turns replies)"
Write-Host ""

$thread.posts | ForEach-Object {
    $bodySnippet = ($_.body -replace "`n", " ").Substring(0, [Math]::Min(100, $_.body.Length))
    "{0,3}  {1,-12} {2,-10} via={3,-12} {4}" -f $_.id, $_.author, $_.stance, $_.via, $bodySnippet
}

# --- summary ---
Write-Host "`n========== TURN SUMMARY ==========" -ForegroundColor Cyan
$turnResults | Format-Table -AutoSize

$expectedReplies = $Turns
$actualReplies   = $thread.posts.Count - 1
$contradictions  = @($thread.posts | Where-Object { $_.stance -eq 'contradict' }).Count
$aliceReplies    = @($thread.posts | Where-Object { $_.author -eq 'alice' }).Count
$bobReplies      = @($thread.posts | Where-Object { $_.author -eq 'bob' }).Count

Write-Host "expected replies: $expectedReplies; actual: $actualReplies"
Write-Host "alice replies: $aliceReplies; bob replies: $bobReplies"
Write-Host "contradict-stance posts: $contradictions"

if ($actualReplies -eq $expectedReplies) {
    Write-Host "`nE2E PASS -- all $Turns turns posted" -ForegroundColor Green
    exit 0
} else {
    Write-Host "`nE2E FAIL -- only $actualReplies/$expectedReplies replies landed" -ForegroundColor Red
    exit 1
}
