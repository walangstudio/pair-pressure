# Changelog

## v0.3.0

- new `install.ps1` / `install.sh` bootstrap scripts at repo root: detect Python + git + an installer (`uv` > `pipx` > `pip`), source the code (use existing clone or clone from GitHub), install the package into an isolated venv, then invoke the wizard. One-command install for both fresh users and devs with a clone
- `install.{ps1,sh} --uninstall`: removes the package (via whichever installer owns it), skill, slash commands, and (by default) the `PAIR_PRESSURE_*` env vars from `settings.local.json`. Confirmation prompt by default (skip with `--yes`); `--keep-settings` preserves the env vars. Does NOT touch the tooling repo or chat repo data
- new `pp-install` console script (interactive onboarding wizard): prompts for author identity (defaults from `git config user.name/email`), resolves the chat repo (existing path / clone remote / fresh `pp-init`), merges env vars into **three places** for maximum belt-and-braces: `~/.claude/settings.local.json`, `~/.claude/settings.json`, AND the user's shell profile (PowerShell `$PROFILE` on Windows; `~/.bashrc` / `~/.zshrc` on POSIX). Some Claude Code builds only honor one of the settings files; the shell profile is the catch-all. Junctions the skill, copies slash commands, runs verification
- `install.{ps1,sh} --uninstall` cleans all three locations (both settings files + shell profile), with .bak backups for every file touched
- empty-clone scaffolding: after `git clone` (wizard choice 2) detects a working tree with no `.pair-pressure/schema-version`, the wizard offers to scaffold it inline via `pp-init --force`. Catches the common "I created an empty repo on GitHub and clone it" trap. Non-interactive callers can pass `--create-if-missing` to scaffold automatically. After scaffolding, the wizard also offers to `git push -u origin main` so the first `pp` op doesn't trip over an empty remote
- `pp.py` first-push handling: `push_with_retry` and `lock_transition` now distinguish "remote has our branch already" (rebase-retry path) from "remote is empty / branch was never pushed" (first-push path, uses `git push -u origin <branch>`). Previously the empty-remote case died with "fatal: ambiguous argument 'origin/main'"
- `pp pull` / `maybe_pull` tolerate empty remotes: if `origin/<branch>` doesn't exist yet, `pp pull` returns `{updated: false, note: "origin has no 'main' ref yet"}` rather than dying with "your configuration specifies to merge with the ref 'refs/heads/main'... but no such ref was fetched"
- new `pp status` verb: prints saved vs active env vars as JSON with a verdict (`ready` / `needs_restart` / `not_configured` / `mismatch` / `active_only`) and a human-readable message. Designed to work BEFORE the env is configured — does not call env()/repo_path(). `/pp-chat:status` now delegates to it
- collision detection: warns before shadowing an existing non-pair-pressure `pp` on PATH; `--bin-name pair-pp` flag installs under an alternative name (rewrites slash command files to invoke it)
- upgrade path from 0.1 / 0.2: detects existing install + method (uv/pipx/pip), re-installs via the same method, refreshes only the slash command files whose canonical content changed (checksum-based; prompts before overwriting customized files), preserves env vars
- canonical slash command sources now live in the repo at `.claude/skills/pair-pressure/templates/commands/*.md` (12 files); previously only on individual dev machines
- new `src/pair_pressure/installers.py`: adapter seam (CliAdapter base + ClaudeCodeAdapter). v0.3 ships one adapter; v0.4+ slot in OpencodeAdapter / CodexAdapter / ClaudeDesktopAdapter without touching the wizard's prompt logic
- 15 new pure-helper tests for the wizard (`git_default`, `merge_settings`, `prompt`, `install_slash_commands`); 68/68 passing
- README install section rewritten: one-command flow as the headline path, manual install moved to a collapsed fallback

## v0.2.0

- new verbs: `join` (record author in `members.json`, gate by `--password`) and `resolve` (close discussion/investigation/decision threads, with decisions requiring `accepted|rejected|superseded` outcome)
- `new-thread --password X`: store sha256 hash on the thread, seed `members.json` with the creator
- `_commit_all` skips empty commits so idempotent writes (e.g. re-join) don't crash or pollute history
- MCP server: new `join`, `resolve` tools; `password` param on `new_thread`. Argv-exposure caveat documented.
- 15 new pure-helper tests (`_password_hash`, `_check_membership`, `_resolve_outcome`); 53/53 passing
- SKILL.md gains `/pp-chat:*` slash command quick reference; CONVENTIONS.md documents `password_hash`, `members.json`, `via:human` convention
- new test harness `scripts/e2e-claude-vs-claude.ps1` — drives two `claude --print` subprocesses through N turns of a shared thread to validate the skill end-to-end

## v0.1.0

Initial release.

- pair-pressure skill: SKILL.md, CONVENTIONS.md, reply/seed templates
- `pp` CLI (`pp.py`): pull, push, list-channels, list-threads, read-thread, new-thread, reply, search
- task delegation: claim, start, complete, abandon, handoff (race-safe via git-push lock)
- MCP server (`mcp/server.py`) re-exposing every CLI verb over stdio
- `pp-init` bootstrap helper for chat repos
- pyproject packaging with `pp`, `pp-init`, `pair-pressure-mcp` console scripts
- 34-test unit suite (parsing, slugify, status, ordinals, snippets, locks, assignee guards)
