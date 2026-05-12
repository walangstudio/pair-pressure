---
description: Task lifecycle. Subcommands: list, new, claim, done.
argument-hint: <list|new|claim|done> [args; @<path> attaches a file]
---

Parse the first token of `$ARGUMENTS` as the subcommand. Remaining text is per-subcommand.

### `task list`

List open and claimable tasks on the active server.
```
pp search --kind task --no-pull
```
Present as a compact table: title, status (unclaimed / claimed / in_progress / done), assignee, last activity, thread id. Default sort: descending by last activity (newest first). `pp search` already uses state for server selection.

### `task new <title> [--to <user>] [--channel <C>] [--password <P>] [body-or-@file]`

Create a task thread in one call. Single tool call:

- Title: first quoted string in `$ARGUMENTS`, or everything before the first `--`.
- `--to <user>`: auto-claim then handoff to `<user>`. `pp task new` does this in-process.
- `--channel <C>`: target channel; if omitted, defaults via state → env → `general` (channel auto-created if missing).
- `--password <P>`: gate the thread. Prefer the stdin form below.
- Body: remaining tokens after flags. `@<path>` reads a file verbatim. If body is empty, `pp task new` writes a seed template (Context / What "done" looks like / Constraints) automatically.

No password:
```
pp task new "<T>" [--to <U>] [--channel <C>] --body-file -
```
Pipe the body on stdin.

With password (keep it off process listings):
```
{ printf '%s\n' "<P>"; printf '%s' "<body>"; } |
  pp task new "<T>" [--to <U>] [--channel <C>] --password-stdin --body-file -
```

Response:
```json
{"ok": true, "server": "...", "channel": "...", "thread_id": "...", "assignee": "<U>|null"}
```

`pp task new` updates state to the new thread, so subsequent `/pp-chat:send`
or `/pp-chat:task done` operate on it without further args. Echo the
thread id and assignee.

### `task claim <title-or-id>`

Resolve `<title-or-id>` (this is a **thread id** — `YYYY-MM-DD_<slug>` — not a
post id):
- If it matches `\d{4}-\d{2}-\d{2}_.*` → treat as thread id directly.
- Otherwise → fuzzy substring match against open tasks (status ≠ done): single match → use it; multiple → ask which; zero → say "no open task matched `<input>`".

```
pp claim --channel <C> --thread <id>
```
Possible responses:
- `{"ok": true, ...}` → confirm; suggest the user begin work and use `/pp-chat:task done` when finished.
- `{"ok": false, "claimed_by": "<other>", ...}` → tell the user `<other>` already holds this task. Do not retry.

Remember the claimed thread as the current tuple. (`pp claim` doesn't update
state for you yet — pass the same `(server, channel, thread)` to subsequent
`pp` calls or run `pp read <id>` afterward to set state.)

### `task done [summary]`

Use the **current joined thread** from state. Refuse if it's not a task —
`pp task done` checks this and returns an error rather than completing.

```
pp task done [--summary "<text>"]
```
Possible responses:
- `{"ok": true, "state": "done"}` → confirm.
- `{"ok": false, "error": "not assignee", ...}` → tell the user only the current assignee can complete; suggest `/pp-chat:send` instead.
- `{"ok": false, "error": "current thread is not a task", "kind": "..."}` →
  for discussions/investigations, suggest `/pp-chat:send <summary>` to post a
  final summary post; for decisions, `pp resolve` directly.
- `{"ok": false, "error": "no current thread in state ..."}` → tell the user
  to first `/pp-chat:read <title>` to set context.

**Server selection**: handled internally by `pp` — explicit `--server` >
per-session state > global state > env > sole-server fallback. You don't need
to pass `--server` unless the user named one explicitly.
