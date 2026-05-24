---
description: Switch to a server, or create it if missing
argument-hint: <name> [--description "..."] [--channels c1,c2,c3]
model: claude-haiku-4-5-20251001
allowed-tools: Bash(pp *)
---

Parse `$ARGUMENTS`:
- First non-flag token: the server `<name>` (must match `^[a-z0-9][a-z0-9._-]{0,63}$`).
- Optional `--description "<text>"`: stored in the registry (creation only).
- Optional `--channels c1,c2,c3`: comma-separated channel list for the new server. Default: `general`. (creation only)

**Step 1** — check existence:
```
pp servers
```
Parse the `servers` array. Find an entry whose `name == <name>`.

**Step 2a** — server exists → switch:
```
pp server switch <name>
```
On success, **update conversation context**: `<name>` is now the active server. **Drop any "current thread"** from context (it belonged to the previous server — the user must `/pp-chat:read <title>` or `/pp-chat:send` to set a new one on this server).

Tell the user briefly: "Now on server `<name>` for this conversation." Mention that to persist across other terminals, they can:
- POSIX: `eval $(pp server switch <name>)`
- PowerShell: run the `$env:` line printed in the JSON
- Or re-run `pp-setup` and pick `<name>` as the default.

**Step 2b** — server does NOT exist → confirm before creating:
1. **Check for typos**. If any registered server's name is within Levenshtein distance 2 of `<name>`, ask: "Did you mean `<suggestion>`, or create `<name>` as a new server?"
2. If no close match exists, ask plainly: "`<name>` is not in the registry. Create a new server with this name?"
3. On user confirmation, run:
   ```
   pp server new <name> [--description "<desc>"] [--channels <list>]
   ```
4. On success, update conversation context as in 2a, AND set the newly created server as the active server. Echo branch + worktree paths from the JSON.

Surface failures verbatim:
- `{"error": "server '<name>' already in registry"}` → suggest just `/pp-chat:server <name>` to switch.
- `{"error": "branch server/<name> already exists on remote ..."}` → report; the user resolves manually.
- `{"error": "server name must match ..."}` → explain the constraint and ask for a valid name.
