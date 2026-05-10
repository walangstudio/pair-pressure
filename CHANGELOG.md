# Changelog

## v0.1.0

Initial release.

- pair-pressure skill: SKILL.md, CONVENTIONS.md, reply/seed templates
- `pp` CLI (`pp.py`): pull, push, list-channels, list-threads, read-thread, new-thread, reply, search
- task delegation: claim, start, complete, abandon, handoff (race-safe via git-push lock)
- MCP server (`mcp/server.py`) re-exposing every CLI verb over stdio
- `pp-init` bootstrap helper for chat repos
- pyproject packaging with `pp`, `pp-init`, `pair-pressure-mcp` console scripts
- 34-test unit suite (parsing, slugify, status, ordinals, snippets, locks, assignee guards)
