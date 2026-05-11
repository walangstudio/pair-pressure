<#
.SYNOPSIS
  Bootstrap installer for pair-pressure on Windows.

.DESCRIPTION
  Detects Python and a package installer (uv preferred, pipx fallback, pip
  last resort), sources the pair-pressure code (uses an existing clone or
  clones from GitHub), installs the package, then runs the pp-install
  wizard for per-dev config (env vars, skill, slash commands).

  Safe to re-run: if pair-pressure is already installed, routes to the
  upgrade flow in pp-install.

.PARAMETER NoConfig
  Skip the pp-install wizard at the end. Useful for CI / unattended use
  when you just want the package installed.

.PARAMETER CloneTo
  Override the default clone target (~/pair-pressure) when this script is
  run from outside an existing clone.

.PARAMETER Installer
  Force a specific installer: 'uv', 'pipx', or 'pip'. By default we pick
  the first available in that order.

.PARAMETER BinName
  Installed binary name. Defaults to 'pp'. Use 'pair-pp' (or similar) if
  another `pp` is already on your PATH and you don't want the shadow.

.PARAMETER Reinstall
  Pass through to pp-install: skip upgrade detection, force full fresh
  wizard.

.PARAMETER Uninstall
  Remove pair-pressure entirely: uninstall the package (via whichever
  installer placed it), remove the skill junction, remove the
  ~/.claude/commands/pp-chat slash command files, and clear the
  PAIR_PRESSURE_* env vars from ~/.claude/settings.local.json (backing
  up the file first). The cloned tooling repo and your chat repo data
  are left untouched.

.PARAMETER KeepSettings
  When used with -Uninstall, do NOT touch ~/.claude/settings.local.json.

.PARAMETER Yes
  Skip the confirmation prompt on -Uninstall (for scripted teardowns).

.NOTES
  Requires: PowerShell 5+, Python 3.9+, git. One of: uv, pipx, pip.
#>
param(
    [switch] $NoConfig,
    [string] $CloneTo  = "",
    [ValidateSet('uv','pipx','pip','')]
    [string] $Installer = "",
    [string] $BinName   = "pp",
    [switch] $Reinstall,
    [switch] $Uninstall,
    [switch] $KeepSettings,
    [switch] $Yes
)

# Native exes (uv, pipx, git) routinely write friendly progress / success
# messages to stderr -- with $ErrorActionPreference='Stop' PowerShell wraps
# each stderr line as a terminating NativeCommandError BEFORE pipeline
# redirects (2>&1, *>$null) can swallow it. So we use Continue and gate on
# $LASTEXITCODE explicitly after every native invocation that matters.
$ErrorActionPreference = 'Continue'

function Have-Cmd($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

function Die($msg) {
    Write-Host $msg -ForegroundColor Red
    exit 1
}

function Remove-EnvVarsFromSettingsFile {
    param([string] $settings)
    if (-not (Test-Path $settings)) { return }
    try {
        $data = Get-Content $settings -Raw | ConvertFrom-Json
    } catch {
        Write-Host "  $(Split-Path $settings -Leaf) is not valid JSON; skipping" -ForegroundColor Yellow
        return
    }
    if (-not $data.env) { return }
    $changed = $false
    foreach ($key in @('PAIR_PRESSURE_REPO','PAIR_PRESSURE_AUTHOR')) {
        if ($data.env.PSObject.Properties.Name -contains $key) {
            $data.env.PSObject.Properties.Remove($key)
            $changed = $true
        }
    }
    if ($changed) {
        Copy-Item $settings "$settings.bak" -Force
        # Write without BOM. PowerShell 5.1's `Set-Content -Encoding utf8`
        # adds one which would make Python's json.loads (used by pp-install)
        # choke on a future read.
        $json = $data | ConvertTo-Json -Depth 10
        [System.IO.File]::WriteAllText($settings, $json, (New-Object System.Text.UTF8Encoding($false)))
        Write-Host "  cleared PAIR_PRESSURE_* from $(Split-Path $settings -Leaf) (backup: $(Split-Path $settings -Leaf).bak)" -ForegroundColor DarkGray
    }
}

function Remove-EnvVarsFromSettings {
    # Two settings files might carry the env vars (the wizard writes to both).
    Remove-EnvVarsFromSettingsFile "$env:USERPROFILE\.claude\settings.local.json"
    Remove-EnvVarsFromSettingsFile "$env:USERPROFILE\.claude\settings.json"
}

function Remove-EnvVarsFromShellProfile {
    # Strip the marker-wrapped block the wizard inserted, if present.
    $beginMarker = "# >>> pair-pressure env vars (pp-install) >>>"
    $endMarker   = "# <<< pair-pressure env vars <<<"
    $candidates = @(
        "$env:USERPROFILE\Documents\WindowsPowerShell\profile.ps1",
        "$env:USERPROFILE\OneDrive\Documents\WindowsPowerShell\profile.ps1"
    )
    foreach ($path in $candidates) {
        if (-not (Test-Path $path)) { continue }
        $text = Get-Content $path -Raw
        $pattern = [regex]::Escape($beginMarker) + "[\s\S]*?" + [regex]::Escape($endMarker)
        if ($text -match $pattern) {
            $updated = [regex]::Replace($text, $pattern, "").TrimEnd() + "`r`n"
            Copy-Item $path "$path.bak" -Force
            [System.IO.File]::WriteAllText($path, $updated, (New-Object System.Text.UTF8Encoding($false)))
            Write-Host "  removed pair-pressure block from $(Split-Path $path -Leaf) (backup: $(Split-Path $path -Leaf).bak)" -ForegroundColor DarkGray
        }
    }
}

function Invoke-Uninstall {
    # Detect python early so we can fall back to `python -m pip` when `pip`
    # isn't directly on PATH (Windows users with Python installed but no
    # pip-on-PATH alias hit this otherwise -- the pip branch silently
    # skipped and left an orphan install).
    $uninstallPython = if (Have-Cmd 'python') { 'python' }
                       elseif (Have-Cmd 'py') { 'py' }
                       else { $null }

    Write-Host "==> pair-pressure uninstall" -ForegroundColor Cyan
    if (-not $Yes) {
        Write-Host "This will:"
        Write-Host "  - Uninstall the pair-pressure package via uv / pipx / pip (whichever owns it)"
        Write-Host "  - Remove the skill at $env:USERPROFILE\.claude\skills\pair-pressure"
        Write-Host "  - Remove slash commands at $env:USERPROFILE\.claude\commands\pp-chat"
        if (-not $KeepSettings) {
            Write-Host "  - Clear PAIR_PRESSURE_* env vars from settings.local.json AND settings.json"
            Write-Host "  - Strip the pair-pressure block from your PowerShell profile"
            Write-Host "    (.bak backups created for every file we touch)"
        }
        Write-Host ""
        Write-Host "It will NOT touch:"
        Write-Host "  - The tooling repo at $PSScriptRoot"
        Write-Host "  - Your chat repo data (wherever PAIR_PRESSURE_REPO points)"
        Write-Host ""
        $resp = Read-Host "Proceed? [y/N]"
        if ($resp.ToLower() -ne 'y') {
            Write-Host "Cancelled." -ForegroundColor Yellow
            exit 0
        }
    }

    Write-Host "==> uninstalling package" -ForegroundColor Cyan
    if (Have-Cmd 'uv')   { & uv tool uninstall pair-pressure *> $null; if ($LASTEXITCODE -eq 0) { Write-Host "  uv tool: removed" -ForegroundColor DarkGray } }
    if (Have-Cmd 'pipx') { & pipx uninstall pair-pressure *> $null;    if ($LASTEXITCODE -eq 0) { Write-Host "  pipx:    removed" -ForegroundColor DarkGray } }
    if (Have-Cmd 'pip') {
        & pip uninstall -y pair-pressure *> $null
        if ($LASTEXITCODE -eq 0) { Write-Host "  pip:     removed" -ForegroundColor DarkGray }
    } elseif ($uninstallPython) {
        & $uninstallPython -m pip uninstall -y pair-pressure *> $null
        if ($LASTEXITCODE -eq 0) { Write-Host "  pip (via $uninstallPython -m): removed" -ForegroundColor DarkGray }
    }

    Write-Host "==> removing Claude Code wiring" -ForegroundColor Cyan
    $skill = "$env:USERPROFILE\.claude\skills\pair-pressure"
    if (Test-Path $skill) {
        # Remove-Item on a junction removes only the junction, not its target.
        Remove-Item $skill -Force -Recurse -ErrorAction SilentlyContinue
        Write-Host "  removed skill at $skill" -ForegroundColor DarkGray
    }
    $cmds = "$env:USERPROFILE\.claude\commands\pp-chat"
    if (Test-Path $cmds) {
        Remove-Item -Recurse $cmds -Force -ErrorAction SilentlyContinue
        Write-Host "  removed slash commands at $cmds" -ForegroundColor DarkGray
    }

    if (-not $KeepSettings) {
        Write-Host "==> cleaning Claude Code settings + PowerShell profile" -ForegroundColor Cyan
        Remove-EnvVarsFromSettings
        Remove-EnvVarsFromShellProfile
    }

    Write-Host ""
    Write-Host "Uninstall complete." -ForegroundColor Green
    Write-Host "The tooling repo at $PSScriptRoot is untouched -- delete manually if you want it gone too."
    exit 0
}

if ($Uninstall) { Invoke-Uninstall }

# ---- Phase 0: preflight ----
Write-Host "==> pair-pressure installer (Windows)" -ForegroundColor Cyan

$python = if (Have-Cmd 'python') { 'python' }
          elseif (Have-Cmd 'py') { 'py' }
          else { Die "Python 3.9+ not found. Install from https://python.org and re-run." }

if (-not (Have-Cmd 'git')) {
    Die "git not found. Install from https://git-scm.com and re-run."
}

# Pick installer
$picked = $Installer
if (-not $picked) {
    if     (Have-Cmd 'uv')   { $picked = 'uv' }
    elseif (Have-Cmd 'pipx') { $picked = 'pipx' }
    elseif (Have-Cmd 'pip')  { $picked = 'pip' }
    else {
        Die @"
Need at least one of: uv (recommended), pipx, or pip.
Install uv: https://docs.astral.sh/uv/  (winget install astral-sh.uv)
Install pipx: python -m pip install --user pipx
"@
    }
}
Write-Host "    python:    $python" -ForegroundColor DarkGray
Write-Host "    installer: $picked" -ForegroundColor DarkGray

# ---- Phase 0.5: collision detection ----
$existingPp = Get-Command pp -ErrorAction SilentlyContinue
if ($existingPp) {
    $version = & pp --version 2>&1 | Out-String
    if ($version -notmatch 'pair-pressure') {
        Write-Warning @"
A different ``pp`` is already on PATH at:
  $($existingPp.Source)

It is NOT pair-pressure. Continuing will create a second ``pp`` (the one
that wins depends on PATH ordering).

To avoid the shadow, re-run with -BinName pair-pp.
"@
        $resp = Read-Host "Proceed anyway? [y/N]"
        if ($resp.ToLower() -ne 'y') {
            Write-Host "Cancelled." -ForegroundColor Yellow
            exit 1
        }
    }
}

# ---- Phase 1: source the code ----
$repoRoot = ""
if (Test-Path "$PSScriptRoot\pyproject.toml") {
    # Running from inside an existing clone.
    $repoRoot = $PSScriptRoot
    Write-Host "    repo:      $repoRoot (existing clone)" -ForegroundColor DarkGray
} else {
    $defaultDir = if ($CloneTo) { $CloneTo } else { "$env:USERPROFILE\pair-pressure" }
    if (-not (Test-Path $defaultDir)) {
        Write-Host "==> cloning pair-pressure to $defaultDir" -ForegroundColor Cyan
        git clone https://github.com/walangstudio/pair-pressure.git $defaultDir
    } else {
        Write-Host "    repo:      $defaultDir (already cloned)" -ForegroundColor DarkGray
    }
    $repoRoot = $defaultDir
}

# ---- Phase 2: install the package ----
Write-Host "==> installing pair-pressure via $picked" -ForegroundColor Cyan
switch ($picked) {
    'uv' {
        & uv tool install --editable $repoRoot --reinstall
        if ($LASTEXITCODE -ne 0) { Die "uv tool install failed (exit $LASTEXITCODE)" }
        # update-shell writes a friendly "already in PATH" / "added X to PATH"
        # line to stderr on success; we want neither aborting the run nor
        # spamming the user. Run it, ignore exit code (it's informational).
        & uv tool update-shell *> $null
    }
    'pipx' {
        & pipx install --editable $repoRoot --force
        if ($LASTEXITCODE -ne 0) { Die "pipx install failed (exit $LASTEXITCODE)" }
        & pipx ensurepath *> $null
    }
    'pip' {
        & $python -m pip install --user --editable $repoRoot --upgrade
        if ($LASTEXITCODE -ne 0) { Die "pip install failed (exit $LASTEXITCODE)" }
    }
}

# ---- Phase 2.5: verify pp on PATH ----
# Refresh PATH for this process — uv tool update-shell / pipx ensurepath
# write into the user PATH but don't propagate to the current session.
$env:PATH = [System.Environment]::GetEnvironmentVariable("PATH","Machine") + ";" + `
            [System.Environment]::GetEnvironmentVariable("PATH","User")

if (-not (Have-Cmd 'pp-install')) {
    Write-Host ""
    Write-Host "pp / pp-install not on PATH in this shell." -ForegroundColor Yellow
    Write-Host "Fix and re-run the wizard:" -ForegroundColor Yellow
    switch ($picked) {
        'uv'   { Write-Host "  uv tool update-shell   # then close + reopen this shell" }
        'pipx' { Write-Host "  pipx ensurepath        # then close + reopen this shell" }
        'pip'  {
            $userBase = & $python -m site --user-base
            Write-Host "  Add $userBase\Scripts to your PATH (System Properties -> Environment Variables)"
        }
    }
    Write-Host "  pp-install             # to run the wizard"
    exit 0
}

# ---- Phase 3: wizard ----
if ($NoConfig) {
    Write-Host ""
    Write-Host "Package installed. -NoConfig set; skipping wizard." -ForegroundColor Green
    Write-Host "Run ``pp-install`` later to configure env vars + skill + slash commands."
    exit 0
}

Write-Host ""
Write-Host "==> launching pp-install wizard" -ForegroundColor Cyan
$wizardArgs = @()
if ($Reinstall) { $wizardArgs += '--reinstall' }
if ($BinName -and $BinName -ne 'pp') { $wizardArgs += '--bin-name', $BinName }

& pp-install @wizardArgs
exit $LASTEXITCODE
