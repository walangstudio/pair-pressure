---
description: Show your pair-pressure identity, active server, and current joined thread
argument-hint: (no args)
---

Run `pp status` and parse the JSON it returns. The verb is designed to work even when env vars aren't loaded, so this is safe right after `pp-install` and before a Claude Code restart.

The output shape:
```json
{
  "saved":   {"PAIR_PRESSURE_AUTHOR": "...", "PAIR_PRESSURE_REPO": "..."},
  "active":  {"PAIR_PRESSURE_AUTHOR": "...", "PAIR_PRESSURE_REPO": "..."},
  "verdict": "ready" | "needs_restart" | "not_configured" | "mismatch" | "active_only",
  "message": "<one-line summary suitable for direct display>",
  "servers": ["<name1>", "<name2>", ...],
  "active_server": "<name>" | null
}
```

Present it to the user as:

```
Saved (~/.claude/settings.*):
  - PAIR_PRESSURE_AUTHOR = <saved.PAIR_PRESSURE_AUTHOR or "(unset)">
  - PAIR_PRESSURE_REPO   = <saved.PAIR_PRESSURE_REPO   or "(unset)">

Active (current session env):
  - PAIR_PRESSURE_AUTHOR = <active.PAIR_PRESSURE_AUTHOR or "(unset)">
  - PAIR_PRESSURE_REPO   = <active.PAIR_PRESSURE_REPO   or "(unset)">

Servers:
  - registered: <comma-separated names, or "(none — run /pp-chat:server-new)">
  - active:     <active_server or "(none — use /pp-chat:server-switch or pass --server)">

<message from the JSON, verbatim>

Current thread: <(server, channel, thread_id, title) you joined/created/read most recently in this session,
                or "none — use /pp-chat:join or /pp-chat:new to set one">
```

The "Current thread" line comes from conversation context — it's the thread you most recently worked with this session. It's not in the `pp status` output; the slash command supplies it.

If you've set an active server in this conversation but `active_server` in the JSON disagrees, prefer the conversation-context server for subsequent calls (the JSON only knows about env / sole-server; it can't see your in-conversation choice).

If `pp` is not on PATH (i.e. pair-pressure isn't installed yet), report the error verbatim and tell the user to run `./install.ps1` from the cloned repo.
