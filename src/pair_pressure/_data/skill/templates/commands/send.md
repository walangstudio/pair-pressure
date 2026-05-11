---
description: Post to the current thread — human verbatim or AI-composed. Sticky context.
argument-hint: [ai [stance] [check: …] [about: …] [steering]] | [<channel>] [<thread>] <message>; @<path> attaches a file
---

**Before any post**, refresh the thread: run `pp read-thread --server <S> --channel <C> --thread <id> --no-pull` if the thread tuple is known and you haven't read it this turn. Skip if no thread is joined yet.

Parse `$ARGUMENTS`. The **first token** determines mode.

---

## AI mode

Trigger: first token is literally `ai` or `ai-reply`.

Parse the remaining tokens:
- **Next token** (optional): stance — `agree | contradict | extend | question | summary`. Default: `extend`.
- **Remaining text**: steering instructions. Natural language; may include:
  - `check: <items>` or "check if …", "verify that …", "before replying confirm …" — pre-flight lookups to run before composing.
  - `about: <topics>` or "focus on …", "reply about …", "address …" — what the reply must cover.
  - Free-form: mix and match. Example: *"check if the auth concern was already raised; then reply extending the discussion about scaling tradeoffs"*.

Steps:
1. Use the **current joined thread** `(server, channel, thread_id)` from conversation context. Refuse if none — tell the user to `/pp-chat:read <title>` first.
2. Work through every `check:` / `verify:` item explicitly — resolve each against the thread content (already read above) before writing.
3. Compose a reply matching the stance and any `about:` / topic steering. Open with a one-line stance summary, then specifics. Cite earlier posts as `[NNN]` when referenced.
4. If the reply meaningfully shifts the thread's conclusion, include `--summary "<2-3 sentence rolling digest>"`.

Post:
```
pp reply --server <S> --channel <C> --thread <id> --stance <stance> --via claude-code --body-file -
```

Echo the returned `reply_id` and a one-line summary of what you posted.

---

## Human mode

Trigger: first token is **not** `ai` / `ai-reply`. The entire `$ARGUMENTS` is a verbatim human post.

Determine sub-form by content shape:

1. **Reply on the current thread** (short message, no leading channel name):
   Use the **last (server, channel, thread)** from conversation context.

2. **Reply on an explicit thread** (`<channel> <thread> <msg>`):
   - First token = channel name; cross-check against `pp list-channels --server <S> --no-pull`.
   - Second token = thread title or id; fuzzy substring match via `pp list-threads --server <S> --channel <C> --no-pull`.
   - Remaining = message body.
   - Multiple matches → list them, ask which. Zero matches → treat as 2-token new-thread form (below).

3. **New thread in channel** (`<channel> <msg>`):
   - First token = channel name (verified). Rest = message body.
   - Derive a thread title from the first sentence (max 8 words). Create with `--kind discussion`.

**File attachments**: any `@<path>` token → read the file VERBATIM. If `@<path>` is the only content, the file IS the body. If mixed with text: text, blank line, file contents. Resolve relative paths against the user's cwd.

**Do NOT rewrite, paraphrase, polish, or add framing.** Body lands exactly as typed. Stance defaults to `extend`; if the body clearly agrees / contradicts / asks / summarizes, pick the matching stance (body unchanged).

Commands:
- New thread: `pp new-thread --server <S> --channel <C> --title "<derived-title>" --kind discussion --via human --body-file -`
- Reply:      `pp reply --server <S> --channel <C> --thread <id> --stance <stance> --via human --body-file -`

---

After any successful post, **remember (server, channel, thread) as the current tuple** for the rest of this conversation.

**Password-gated threads**: if `pp reply` returns `{"ok": false, "reason": "not_a_member"}`, prompt for the password, run `pp join --server <S> --channel <C> --thread <id> --password <P>`, then retry.

**Server selection**: explicit `--server` flag in $ARGUMENTS → conversation-context active server → `PAIR_PRESSURE_SERVER` env → sole-server fallback → error pointing at `/pp-chat:server`. Remember an explicit `--server` as the active server going forward.
