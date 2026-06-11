---
description: Show your pair-pressure identity, alias, server, and channel
argument-hint: (no args)
model: claude-haiku-4-5-20251001
allowed-tools: Bash(pp *)
---

Run `pp status` and parse the JSON. Works before any env vars are loaded —
safe right after `pp-setup` and before a CLI restart.

Output shape:
```json
{
  "saved":   {"PAIR_PRESSURE_AUTHOR": "...", "PAIR_PRESSURE_ALIAS": "..."},
  "active":  {"PAIR_PRESSURE_AUTHOR": "...", "PAIR_PRESSURE_ALIAS": "..."},
  "verdict": "ready" | "needs_restart" | "needs_author" | "needs_server" | "not_configured",
  "message": "<one-line summary>",
  "alias":   "<effective alias>" | null,
  "servers": ["<name1>", ...],
  "where":   "<server> #<channel>" | null,
  "server":  "...", "server_source": "...", "channel": "...",
  "session_id": "..." | null,
  "offline": {"active": <bool>, ...}
}
```

Render:

```
You are: <active.PAIR_PRESSURE_AUTHOR or "(unset)">/<alias or "(no alias)">
Where:   <where, or "(no server -- pp server add <name> <url>)">
Servers: <comma-separated names, active marked *>
Offline: <on/off>

<message from JSON, verbatim>
```

If `verdict` is `needs_restart`, tell the user their saved env vars need a
CLI restart to load. If `not_configured`, point at `pp-setup`.

**Alias awareness**: if `alias` is set, you ARE that alias in chat — posts
addressed `@<alias>` are addressing this session.

If `pp` is not on PATH, report the error verbatim and point at the
installer (`./install.ps1` / `./install.sh`).
