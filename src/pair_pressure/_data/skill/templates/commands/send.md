---
description: Post to the current thread. Verbatim by default — instant, no AI thinking.
argument-hint: <message> | ai [stance] <steering> | <channel> [<thread>] <message>
---

# DO NOT THINK. EXECUTE.

The default behaviour is **post the bytes verbatim, immediately, no pre-flight
reads, no rewriting**. Only branch into the slower modes when the fast path
below cannot match.

---

## Fast path — handle this FIRST

If ALL of these hold:

1. A current thread `(server, channel, thread_id)` is in conversation context, AND
2. The first token of `$ARGUMENTS` is NOT one of `ai`, `ai-reply`, AND
3. The first token does NOT exactly match a channel name on the active server
   (skip this check if you don't already know the channel list — do NOT call
   `pp list-channels` just to find out).

→ POST IT NOW. One tool call, no thinking:

```
pp reply --server <S> --channel <C> --thread <T> --stance extend --via human --body-file -
```

Pipe `$ARGUMENTS` verbatim on stdin. The body lands signed as just the author
(no AI alias, because `--via human`). Do NOT read the thread first. Do NOT
paraphrase, polish, or add framing. Echo only the returned `reply_id`.

If `$ARGUMENTS` contains `@<path>` tokens, replace each with the file's
verbatim contents (text first, blank line, then file body if mixed). Resolve
relative paths against the user's cwd.

---

## Default-fallback path — when no current thread is set

If the first token is NOT `ai`/`ai-reply` AND no current thread is in
conversation context AND the first token does NOT match a known channel name,
**auto-resolve to defaults and post without asking**.

Defaults:
- channel: `$PAIR_PRESSURE_DEFAULT_CHANNEL` ?? `general`
- thread title: `$PAIR_PRESSURE_DEFAULT_THREAD_TITLE` ?? `general-chat`

Algorithm (do not prompt, do not confirm):

1. `pp channel ensure --server <S> --name <DEFAULT_CHANNEL>` — idempotent.
2. `pp list-threads --server <S> --channel <DEFAULT_CHANNEL> --no-pull`.
3. Find the most-recent thread whose id ends with `_<slug(DEFAULT_TITLE)>`
   (e.g. `2026-05-12_general-chat`). Slug = lowercase, non-alphanumerics →
   hyphens, trim, max 48 chars.
4. **Match found** → reply to it:
   ```
   pp reply --server <S> --channel <DEFAULT_CHANNEL> --thread <id> --stance extend --via human --body-file -
   ```
5. **No match** → create the thread; the message becomes its seed:
   ```
   pp new-thread --server <S> --channel <DEFAULT_CHANNEL> --title "<DEFAULT_TITLE>" --kind discussion --via human --body-file -
   ```
6. Remember `(server, DEFAULT_CHANNEL, resolved_thread_id)` as the current
   tuple for the rest of this conversation. Subsequent `/pp-chat:send` calls
   take the fast path above (one tool call).

Echo the resulting `reply_id` or `thread_id`. No commentary about which
defaults were used unless the user asks.

---

## AI mode

Trigger: first token is literally `ai` or `ai-reply`. The body is signed
`<author>/<alias>`.

1. Use the **current joined thread**. Refuse if none — tell the user to
   `/pp-chat:read <title>` first.
2. Read the thread once (skip if already read this turn):
   `pp read-thread --server <S> --channel <C> --thread <T> --no-pull`.
3. Parse the steering text after `ai`:
   - Optional next token: stance `agree | contradict | extend | question | summary` (default `extend`).
   - Free-form: any `check: …` / "verify that …" items are pre-flight lookups; any
     `about: …` / "focus on …" items are topic constraints.
4. Compose. Open with a one-line stance summary, then specifics. Cite earlier
   posts as `[<short-id>]` (last 6 chars of the timestamp suffice; the reader
   resolves substrings).
5. If your reply meaningfully shifts the thread's conclusion, include
   `--summary "<2-3 sentence rolling digest>"`.

```
pp reply --server <S> --channel <C> --thread <T> --stance <stance> --via claude-code [--alias <N>] --body-file -
```

If `/pp-chat:alias <N>` was invoked earlier in this conversation, include
`--alias <N>` so the post signs as `<author>/<N>` and not the env-var alias.

---

## Explicit-target mode

Trigger: no current thread in context, OR the first token matches a known
channel name.

Forms:

1. `<channel> <thread> <msg>` — channel + fuzzy thread title/id + body.
2. `<channel> <msg>` — channel + body, create a new discussion thread. Derive
   title from first sentence (max 8 words).

Resolve channel by exact match against `pp list-channels --server <S> --no-pull`.
Resolve thread by fuzzy substring match against
`pp list-threads --server <S> --channel <C> --no-pull`. On multiple matches,
ask which. On zero matches in form 1, fall back to form 2.

```
pp new-thread --server <S> --channel <C> --title "<derived>" --kind discussion --via human --body-file -
pp reply      --server <S> --channel <C> --thread <id>       --stance <s>      --via human --body-file -
```

The `--via human` default still applies here — these are user-typed
messages. Use `--via claude-code` only when the body is AI-composed (which in
explicit-target mode would mean the user prefixed with `ai`, in which case
the AI-mode section above applies first).

---

## Aliases

If `PAIR_PRESSURE_ALIAS` is set, AI-composed posts (`--via claude-code`) are
signed `<author>/<alias>` (e.g. `alice/Echo`). Human verbatim posts
(`--via human`) are signed just `<author>`. The CLI handles this from
`--via`; never override.

In a thread, posts addressed to `@<your-alias>`, `<your-alias>:`, or
`<your-alias> says` are addressing **you** specifically. Posts addressed to
a different alias are not for you.

---

## After any post

- Remember `(server, channel, thread)` as the current tuple for this conversation.
- `via: human` = dev typed those bytes. `via: claude-code` = AI composed.
  Preserve faithfully.
- **Password-gated threads**: on `{"ok": false, "reason": "not_a_member"}`,
  prompt for the password and pipe it via stdin (keeps it out of process
  listings):
  `printf '%s' "<P>" | pp join --server <S> --channel <C> --thread <id> --password-stdin`,
  then retry.
- **Server selection priority**: explicit `--server` flag > conversation-context
  active server > `PAIR_PRESSURE_SERVER` > sole-server fallback > error.
