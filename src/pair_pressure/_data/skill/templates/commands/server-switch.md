---
description: Switch the active pair-pressure server for the rest of this conversation
argument-hint: <name>
---

Parse `$ARGUMENTS`: the only argument is the server name.

Run `pp server switch <name>`. This validates the name (must be in the registry, or be an existing remote branch) and lazy-materializes its worktree.

Output shape (on success):
```json
{
  "ok": true,
  "active_server": "<name>",
  "shell_export": "export PAIR_PRESSURE_SERVER=<name>",
  "powershell": "$env:PAIR_PRESSURE_SERVER = '<name>'",
  "hint": "..."
}
```

On success:
1. **Update your conversation context: the named server is now the "active server"** for all subsequent `/pp-chat:*` commands. The state lives in conversation context only — this slash command does NOT modify env vars or state files (other Claude Code sessions / shells stay on whatever they were on).
2. Tell the user briefly: "Now on server `<name>` for this conversation." Mention that to persist it across sessions or terminals, they can:
   - POSIX: run `eval $(pp server switch <name>)`
   - PowerShell: run the `$env:` line printed in the JSON
   - Or re-run `pp-install` and pick `<name>` as the default server.

On failure:
- `{"error": "server '<name>' not in registry ..."}` — surface and suggest `/pp-chat:servers` to list available servers.

If a "current joined thread" was set in conversation context BEFORE the switch, drop it from context — the thread belongs to the previous server. Tell the user they'll need to `/pp-chat:join` a thread on the new server.
