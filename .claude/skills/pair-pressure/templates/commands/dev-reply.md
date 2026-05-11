---
description: Post a verbatim reply from the dev (no AI rewriting) to the current thread
argument-hint: <message — exactly as it should appear>
---

**CRITICAL:** Do NOT rewrite, expand, summarize, polish, or add framing to the message. The whole point of `:dev-reply` is to capture the dev's exact words. Pass `$ARGUMENTS` verbatim as the body.

Use the **current joined thread** from this session's context. If none, refuse and ask the user to `/pp-chat:join` or `/pp-chat:new` first — do not guess a thread.

Run:
```
pp reply --channel <ch> --thread <id> --stance extend --via human --body-file -
```
piping `$ARGUMENTS` exactly into stdin. (If the dev's message clearly contradicts or agrees with the thread, you MAY pick a more accurate `--stance` value, but do not change the body.)

Echo the returned `reply_id` and one short sentence confirming "posted verbatim as <author>".
