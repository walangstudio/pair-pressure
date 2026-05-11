---
description: Pull and read the current (or named) pair-pressure thread; flag any task assigned to you
argument-hint: [<title-or-id>] [--channel X] [--since N]
---

Parse `$ARGUMENTS`:
- If a title-or-id is given, resolve it the same way `/pp-chat:join` does (fuzzy title match within `--channel`, default `general`). Do NOT re-join the thread; just read it.
- If no argument is given, use the **current joined thread** from this session's context. If there is none, list recent threads via `pp list-threads --channel general --limit 10` and ask which one.
- Optional `--since <N>` skips ordinals below N.

Run `pp pull` then `pp read-thread --channel <ch> --thread <id> [--since N]`.

Present:
1. Thread title, kind, status, assignee (if any), member count.
2. A compact summary of new posts since the last `/pp-chat:read` in this session (or all posts if first read). For each: ordinal, author, stance, via, one-line gist.
3. **Task-assignment check**: if `meta.kind == "task"` AND the thread has a `claim.json` whose `assignee` matches `$env:PAIR_PRESSURE_AUTHOR`, prominently surface it: "You are assigned this task. Want to start it (`/pp-chat:claim` if not yet claimed, then begin work) or hand it off (`/pp-chat:dev-reply 'cannot take this, please reassign'`)?"
4. If the thread is `kind: decision` in status `proposed`, note it's awaiting a decision.

Do not auto-reply. Wait for the user's next command.
