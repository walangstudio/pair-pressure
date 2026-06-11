---
description: Switch server and/or channel in one move. Persists across resume.
argument-hint: <server> | #<channel> | <server> #<channel>
model: claude-haiku-4-5-20251001
allowed-tools: Bash(pp *)
---

# DO NOT THINK. EXECUTE.

One tool call — pass `$ARGUMENTS` verbatim:

```
pp use $ARGUMENTS
```

Targets: `<server>` (channel resets to default), `#<channel>` (stay on the
active server), or `<server> #<channel>`.

Response: `{"ok": true, "where": "<server> #<channel> (alias: X)",
"server": "...", "channel": "...", "alias": "..."}` — `pp` also prints
`now in: ...` to stderr. Echo exactly one line to the user:

```
Now in: <server> #<channel> (alias: <alias>)
```

On `{"error": ...}` (unregistered server, missing/archived channel, private
group you're not in) relay the error verbatim — it names the fix
(`pp server list`, `pp channels`, `pp channel unarchive`).

The switch is saved to this conversation's session state (via
`PAIR_PRESSURE_SESSION_ID`) and the machine-global default, so resuming the
conversation restores it.
