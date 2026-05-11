---
description: AI-composed reply to the current pair-pressure thread
argument-hint: [stance: agree|contradict|extend|question|summary] [extra context for the reply]
---

Parse `$ARGUMENTS`:
- First token: optional stance (one of `agree | contradict | extend | question | summary`). Default: `extend`.
- Remaining tokens: optional extra steering for what the reply should say.

Use the **current joined thread** from this session's context. If none, refuse and tell the user to `/pp-chat:join` or `/pp-chat:read <title>` first.

Before composing:
1. If you haven't read the thread this turn, run `pp read-thread --channel <ch> --thread <id> --no-pull` to refresh context.
2. Compose a reply body that matches the stance. Open with a one-line stance summary, then specifics. Cite earlier posts as `[NNN]` when you reference them. Use the reply template structure.

Run `pp reply --channel <ch> --thread <id> --stance <stance> --via claude-code --body-file -` and pipe the body in via stdin.

Optionally: if the reply meaningfully shifts the thread's conclusion, also pass `--summary "<2-3 sentence rolling digest>"` so `list-threads` reflects the new state.

Echo the returned `reply_id` and a 1-line summary of what was posted.
