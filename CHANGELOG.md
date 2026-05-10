# Changelog

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
