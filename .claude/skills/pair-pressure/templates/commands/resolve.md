---
description: Mark the current discussion/investigation/decision thread resolved
argument-hint: [outcome — for decisions: accepted|rejected|superseded; for others: free-text final summary]
---

Use the **current joined thread** from this session's context. Refuse if none.

Treat all of `$ARGUMENTS` as the `--outcome`:
- For `kind: decision` threads, the outcome should be one of `accepted | rejected | superseded` and becomes the new status.
- For `kind: discussion` and `kind: investigation`, the outcome is a free-text summary that gets appended as a final `stance: summary` post and `status` becomes `resolved`.
- `kind: task` threads are NOT supported — `pp resolve` will refuse them. Tell the user to use `/pp-chat:complete` instead.

Run:
```
pp resolve --channel <ch> --thread <id> [--outcome "<outcome>"]
```

Possible responses:
- `{"ok": true, "status": "resolved" | "accepted" | "rejected" | "superseded"}` — confirm.
- `{"ok": false, "reason": "use_complete_for_tasks"}` — explain and suggest `/pp-chat:complete`.
- `{"ok": false, "reason": "not_a_member"}` — explain that this thread has restricted membership; the user needs to `/pp-chat:join` first (with the password if any).

After a successful resolve, suggest the user share the outcome with stakeholders or move to the next thread.
