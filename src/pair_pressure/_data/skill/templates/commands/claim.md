---
description: Atomically claim the current task thread
argument-hint: (no args; operates on current thread)
---

Use the **current joined thread** (server + channel + thread_id) from this session's context. Refuse if none.

Run `pp claim --server <server> --channel <ch> --thread <id>`.

Possible responses:
- `{"ok": true, "assignee": "<you>", "state": "claimed"}` — confirm to the user, suggest they begin work and use `/pp-chat:complete` when done.
- `{"ok": false, "claimed_by": "<other>", "state": "..."}` — tell the user that <other> already holds this task. Do not retry.

Only meaningful for `kind: task` threads. If the current thread is some other kind, tell the user `/pp-chat:claim` only applies to tasks and suggest `/pp-chat:reply` instead.

**Server selection.** The `--server` value comes from the current joined thread's tuple; do not infer or change it.
