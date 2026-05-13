---
description: Show your pair-pressure identity, alias, active server, and current joined thread
argument-hint: (no args)
---

Run `pp status` and parse the JSON. Works before any env vars are loaded — safe
to run right after `pp-setup` and before a Claude Code restart.

Output shape:
```json
{
  "saved":   {"PAIR_PRESSURE_AUTHOR": "...", "PAIR_PRESSURE_REPO": "...",
              "PAIR_PRESSURE_ALIAS": "..."},
  "active":  {"PAIR_PRESSURE_AUTHOR": "...", "PAIR_PRESSURE_REPO": "...",
              "PAIR_PRESSURE_ALIAS": "..."},
  "verdict": "ready" | "needs_restart" | "not_configured" | "mismatch" | "active_only",
  "message": "<one-line summary>",
  "alias":   "<active or saved alias>" | null,
  "servers": ["<name1>", "<name2>", ...],
  "active_server": "<name>" | null
}
```

Render:

```
Saved (~/.claude/settings.*):
  - PAIR_PRESSURE_AUTHOR = <saved.PAIR_PRESSURE_AUTHOR or "(unset)">
  - PAIR_PRESSURE_REPO   = <saved.PAIR_PRESSURE_REPO   or "(unset)">
  - PAIR_PRESSURE_ALIAS  = <saved.PAIR_PRESSURE_ALIAS  or "(unset, optional)">

Active (current session env):
  - PAIR_PRESSURE_AUTHOR = <active.PAIR_PRESSURE_AUTHOR or "(unset)">
  - PAIR_PRESSURE_REPO   = <active.PAIR_PRESSURE_REPO   or "(unset)">
  - PAIR_PRESSURE_ALIAS  = <active.PAIR_PRESSURE_ALIAS  or "(unset)">

Identity for posts:
  - human-typed (via=human):  <author>           (no alias)
  - AI-composed (via=cc/mcp): <author>/<alias>   (or just <author> if alias unset)

Servers:
  - registered: <comma-separated names, or "(none -- run /pp-chat:server <name>)">
  - active:     <conv-context server, falling back to active_server, or "(none)">

<message from JSON, verbatim>

Current thread: <(server, channel, thread_id, title) most recently joined this session,
                or "none -- /pp-chat:read <title> or /pp-chat:send <channel> <msg>">
```

**Alias awareness reminder**: if `PAIR_PRESSURE_ALIAS` is set, you ARE that
alias in chat. Posts addressed `@<alias>` are addressing this session.

**Conversation-context vs env**: if you've set an active server via
`/pp-chat:server <name>` in this conversation, prefer that for "active". The
JSON only knows about env / sole-server fallback — it can't see your
in-conversation choice.

If `pp` is not on PATH (pair-pressure isn't installed), report the error
verbatim and tell the user to run `./install.ps1` from a cloned repo.
