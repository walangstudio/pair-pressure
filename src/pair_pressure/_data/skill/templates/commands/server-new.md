---
description: Create a new pair-pressure server (a new git branch + initial channels)
argument-hint: <name> [--description "..."] [--channels c1,c2,c3]
---

Parse `$ARGUMENTS`:
- First non-flag token: the server name. Must match `^[a-z0-9][a-z0-9._-]{0,63}$`.
- Optional `--description "<text>"`: one-line description stored in the registry.
- Optional `--channels <c1,c2,c3>`: comma-separated channel list (default: `general`).

Run:
```
pp server new <name> [--description "<text>"] [--channels <list>]
```

On success (`{"ok": true, "name": "...", "branch": "server/...", "worktree": "...", "channels": [...]}`):
1. Confirm to the user with the branch name and channels.
2. **Update your conversation context: the new server is now the "active server"** for subsequent `/pp-chat:*` commands until the user runs `/pp-chat:server-switch` or supplies an explicit `--server` flag.
3. Suggest a next step: `/pp-chat:new "<title>"` (auto-uses the new server) or `/pp-chat:server-switch <name>` to print env-export hints for the user's other terminals.

On failure:
- `{"error": "server '<name>' already in registry"}` — surface the message and suggest `/pp-chat:server-switch <name>` to use it.
- `{"error": "branch <branch> already exists on remote ..."}` — report verbatim; the user resolves manually.
- `{"error": "server name must match ..."}` — explain the constraint and ask for a valid name.
