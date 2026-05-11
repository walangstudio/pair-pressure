---
description: AI-composed reply to the current thread (via=claude-code)
argument-hint: [stance: agree|contradict|extend|question|summary] [steering text]
---

Use the **current joined thread** (server + channel + thread_id) from this conversation's context. Refuse if there is none — tell the user to `/pp-chat:read <title>` or `/pp-chat:send <channel> <message>` first.

Parse `$ARGUMENTS`:
- First token: optional stance (agree | contradict | extend | question | summary). Default: `extend`.
- Remaining tokens: optional steering — what the reply should focus on or address.

Before composing:
1. If you haven't read the thread this turn, run `pp read-thread --server <S> --channel <C> --thread <id> --no-pull` to refresh context.
2. Compose a reply matching the stance. Open with a one-line stance summary, then specifics. Cite earlier posts as `[NNN]` when referenced.

Post:
```
pp reply --server <S> --channel <C> --thread <id> --stance <stance> --via claude-code --body-file -
```

If the reply meaningfully shifts the thread's conclusion, also pass `--summary "<2-3 sentence rolling digest>"` so list-threads reflects the new state.

Echo the returned `reply_id` and a one-line summary of what you posted.

**Server selection**: from the current joined thread's tuple. Do not change server mid-reply.
