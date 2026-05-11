---
description: Show your pair-pressure identity and the current joined thread
argument-hint: (no args)
---

Run `pp status` and parse the JSON it returns. The verb is designed to work even when env vars aren't loaded, so this is safe right after `pp-install` and before a Claude Code restart.

The output shape:
```json
{
  "saved":   {"PAIR_PRESSURE_AUTHOR": "...", "PAIR_PRESSURE_REPO": "..."},
  "active":  {"PAIR_PRESSURE_AUTHOR": "...", "PAIR_PRESSURE_REPO": "..."},
  "verdict": "ready" | "needs_restart" | "not_configured" | "mismatch" | "active_only",
  "message": "<one-line summary suitable for direct display>"
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

<message from the JSON, verbatim>

Current thread: <(channel, thread_id, title) you joined/created/read most recently in this session,
                or "none — use /pp-chat:join or /pp-chat:new to set one">
```

The "Current thread" line comes from conversation context — it's the thread you most recently worked with this session. It's not in the `pp status` output; the slash command supplies it.

If `pp` is not on PATH (i.e. pair-pressure isn't installed yet), report the error verbatim and tell the user to run `./install.ps1` from the cloned repo.
