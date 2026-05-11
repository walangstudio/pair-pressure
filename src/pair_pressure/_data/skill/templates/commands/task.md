---
description: Task lifecycle. Subcommands: list, new, claim, done.
argument-hint: <list|new|claim|done> [args; @<path> attaches a file]
---

Parse the first token of `$ARGUMENTS` as the subcommand. Remaining text is per-subcommand.

### `task list`

List open and claimable tasks on the active server.
```
pp search --server <S> --kind task --no-pull
```
Present as a compact table: title, status (unclaimed / claimed / in_progress / done), assignee, last activity, thread id. Default sort: descending by last activity (newest first).

### `task new <title> [--to <user>] [--channel <C>] [--password <P>] [body-or-@file]`

Create a task thread.
- Title: first quoted string in $ARGUMENTS, or everything before the first `--`.
- `--to <user>`: auto-claim then handoff to `<user>`.
- `--channel <C>`: target channel (default: `general`).
- `--password <P>`: gate the thread.
- Body: remaining tokens after flags. `@<path>` reads a file verbatim (same rules as `/pp-chat:send`). If body is empty, draft a short default using the seed template:
  ```
  ## Context
  <one-sentence problem statement>
  ## What "done" looks like
  <observable acceptance>
  ## Constraints
  <known constraints, or "none">
  ```

Create:
```
pp new-thread --server <S> --channel <C> --title "<T>" --kind task [--password <P>] --via human --body-file -
```
If `--to <user>`:
```
pp claim   --server <S> --channel <C> --thread <new_id>
pp handoff --server <S> --channel <C> --thread <new_id> --to <user>
```

Remember (server, channel, thread_id) as the current tuple. Echo the thread id and assignee.

### `task claim <title-or-id>`

Resolve `<title-or-id>`:
- If it matches `\d{4}-\d{2}-\d{2}_.*` → treat as thread id directly.
- Otherwise → fuzzy substring match against open tasks (status ≠ done): single match → use it; multiple → ask which; zero → say "no open task matched `<input>`".

```
pp claim --server <S> --channel <C> --thread <id>
```
Possible responses:
- `{"ok": true, ...}` → confirm; suggest the user begin work and use `/pp-chat:task done` when finished.
- `{"ok": false, "claimed_by": "<other>", ...}` → tell the user `<other>` already holds this task. Do not retry.

Remember the claimed thread as the current tuple.

### `task done [summary]`

Use the **current joined thread** from conversation context. Refuse if its kind is not `task` — for discussions/investigations, use `/pp-chat:send <summary>` to post a final summary post; for decisions, the user must invoke `pp resolve` directly (decisions are a power-user verb).

```
pp complete --server <S> --channel <C> --thread <id> [--summary "<text>"]
```
Possible responses:
- `{"ok": true, "state": "done"}` → confirm.
- `{"ok": false, "error": "not assignee", ...}` → tell the user only the current assignee can complete; suggest `/pp-chat:send` instead.

**Server selection**: explicit `--server` wins; otherwise conversation-context active server; otherwise `PAIR_PRESSURE_SERVER`; otherwise sole-server fallback. Remember an explicit `--server` as the active server going forward.
