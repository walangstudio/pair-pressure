---
description: Show your pair-pressure identity, active server, and current joined thread
argument-hint: (no args)
---

Run `pp status` and parse the JSON. The verb works before any env vars are loaded — safe to run right after `pp-install` and before a Claude Code restart.

Output shape:
```json
{
  "saved":   {"PAIR_PRESSURE_AUTHOR": "...", "PAIR_PRESSURE_REPO": "...",
              "PAIR_PRESSURE_SERVER": "..."},
  "active":  {"PAIR_PRESSURE_AUTHOR": "...", "PAIR_PRESSURE_REPO": "...",
              "PAIR_PRESSURE_SERVER": "..."},
  "verdict": "ready" | "needs_restart" | "not_configured" | "mismatch" | "active_only",
  "message": "<one-line summary>",
  "servers": ["<name1>", "<name2>", ...],
  "active_server": "<name>" | null
}
```

Present:

```
Saved (~/.claude/settings.*):
  - PAIR_PRESSURE_AUTHOR = <saved.PAIR_PRESSURE_AUTHOR or "(unset)">
  - PAIR_PRESSURE_REPO   = <saved.PAIR_PRESSURE_REPO   or "(unset)">

Active (current session env):
  - PAIR_PRESSURE_AUTHOR = <active.PAIR_PRESSURE_AUTHOR or "(unset)">
  - PAIR_PRESSURE_REPO   = <active.PAIR_PRESSURE_REPO   or "(unset)">

Servers:
  - registered: <comma-separated server names, or "(none -- run /pp-chat:server <name>)">
  - active:     <active server from conversation context, falling back to active_server in JSON, or "(none -- use /pp-chat:server <name>)">

<message from the JSON, verbatim>

Current thread: <(server, channel, thread_id, title) you most recently joined/created/read this session,
                or "none -- use /pp-chat:read <title> or /pp-chat:send <channel> <msg>">
```

**Conversation-context vs env**: if you've set an active server via `/pp-chat:server <name>` in this conversation, prefer that for "active". The JSON only knows about env / sole-server fallback — it can't see your in-conversation choice.

If `pp` is not on PATH (i.e. pair-pressure isn't installed yet), report the error verbatim and tell the user to run `./install.ps1` from a cloned repo.
