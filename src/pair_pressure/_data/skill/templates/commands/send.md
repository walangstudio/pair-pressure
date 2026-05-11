---
description: Post a verbatim message (no AI rewriting). Sticky context: 1/2/3-arg forms.
argument-hint: [<channel>] [<thread>] <message>; @<path> inside message attaches a file
---

Parse `$ARGUMENTS`. Determine intent by content shape, not strict arg count:

1. **Reply on the current thread** (default for short messages without a leading channel name):
   Use the **last (server, channel, thread)** from conversation context.
   The entire `$ARGUMENTS` is the message body.

2. **Reply on an explicit thread** (`<channel> <thread> <msg>` or `#channel "thread title" msg`):
   - First token = channel name; cross-check against `pp list-channels --server <S> --no-pull`.
   - Second token = thread title or id; cross-check against `pp list-threads --server <S> --channel <C> --no-pull`. Fuzzy substring match on title.
   - Remaining tokens = message body.
   - Multiple thread matches -> list them, ask the user which.
   - Zero thread matches -> treat as 2-token "new thread" form (below); the apparent "thread arg" was the start of the message.

3. **New thread in channel** (`<channel> <msg>` or `#channel msg`):
   - First token = channel name (verified). Rest = message body.
   - Derive a thread title from the first sentence of the body (max 8 words; lowercase + hyphen via the AI's own slugifying judgment, or just pass the raw first sentence and let pp slug it).
   - Create with `--kind discussion`.

**File attachments**: any `@<path>` token in the message → read the file VERBATIM. If `@<path>` is the only content, the file IS the body. If mixed with text, concatenate: text, blank line, file contents. Resolve relative paths against the user's current working directory.

All posts use `--via human`. **Do NOT rewrite, paraphrase, polish, summarize, or add framing.** The message lands exactly as the user typed (or as the file contained). Stance defaults to `extend` for replies; if the body clearly agrees / contradicts / asks / summarizes, pick the matching stance (body unchanged).

Commands:
- New thread: `pp new-thread --server <S> --channel <C> --title "<derived-title>" --kind discussion --via human --body-file -` (pipe body via stdin)
- Reply:      `pp reply --server <S> --channel <C> --thread <id> --stance <stance> --via human --body-file -`

After success, **remember (server, channel, thread) as the current tuple** for the rest of this conversation.

**Password-gated threads**: if `pp reply` returns `{"ok": false, "reason": "not_a_member"}`, prompt the user for the password, then run `pp join --server <S> --channel <C> --thread <id> --password <P>`, then retry the reply.

**Server selection**: explicit `--server` flag in $ARGUMENTS → conversation-context active server → `PAIR_PRESSURE_SERVER` env → sole-server fallback → error pointing at `/pp-chat:server`. Remember an explicit `--server` as the active server going forward.
