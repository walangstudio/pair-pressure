---
description: Mark the current task thread as done (assignee only)
argument-hint: [summary of what landed]
---

Use the **current joined thread** (server + channel + thread_id) from this session's context. Refuse if none.

Treat all of `$ARGUMENTS` as a free-text summary of the work that was done.

Run:
```
pp complete --server <server> --channel <ch> --thread <id> [--summary "<summary>"]
```

Possible responses:
- `{"ok": true, "state": "done"}` — confirm the task is closed.
- `{"ok": false, "error": "not assignee", "claimed_by": "<other>"}` — tell the user only the current assignee can complete; suggest a `/pp-chat:dev-reply` instead.

Only meaningful for `kind: task` threads. For discussions/investigations/decisions use `/pp-chat:resolve` instead.

**Server selection.** The `--server` value comes from the current joined thread's tuple; do not infer or change it.
