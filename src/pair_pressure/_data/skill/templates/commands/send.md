---
description: Post to the current thread. Verbatim by default — instant, no AI thinking.
argument-hint: <message> | ai [stance] <steering> | <channel> [<thread>] <message>
---

# DO NOT THINK. EXECUTE.

The default behaviour is **post the bytes verbatim, immediately, no pre-flight
reads, no rewriting**. Only branch into AI mode when the user explicitly asks
for it.

---

## Fast path — handle this FIRST

If the first token of `$ARGUMENTS` is NOT `ai` or `ai-reply`, just pipe the
whole thing to `pp send`. One tool call:

```
pp send --via human --body-file -
```

Pipe `$ARGUMENTS` verbatim on stdin. `pp send` itself handles:

- channel resolution (state → env → `general`),
- thread lookup (state → fuzzy match by default title → auto-seed),
- channel auto-creation,
- state update so the next `/pp-chat:send` lands on the same thread.

Do NOT read the thread first. Do NOT call `pp list-channels`, `pp list-threads`,
`pp channel ensure`, or `pp status` — `pp send` does all of that internally.
Do NOT paraphrase, polish, or add framing. Echo only the returned `thread_id`
and `post_id`.

The response shape is:

```json
{"ok": true, "kind": "reply" | "seed",
 "server": "...", "channel": "...", "thread_id": "...", "post_id": "..."}
```

If `$ARGUMENTS` contains `@<path>` tokens, replace each with the file's
verbatim contents (text first, blank line, then file body if mixed). Resolve
relative paths against the user's cwd.

If `$ARGUMENTS` contains `@@<path>` tokens (double-at), do NOT inline. Leave
them in the body verbatim — `pp send` itself copies each referenced file
into `channels/<C>/<thread>/attachments/<post-id>/` and rewrites the token
to a relative markdown link. Use `@@<path>` for binaries, large files, or
anything you want preserved as a standalone artifact alongside the post.

If `$ARGUMENTS` contains one or more `--attach <path>` tokens, strip them
out before piping the remainder as the body, and forward each as a real
flag to `pp send`:

```
pp send --via human --attach <path1> [--attach <path2> …] --body-file -
```

The stripped body (everything except the `--attach <path>` pairs) becomes
the post body on stdin. `--attach` appends an `## Attachments` section to
the post; `@@<path>` attaches inline. The two can be combined freely.

If the user explicitly says "attach <file>" (vs. "include" or "paste"),
prefer `--attach <path>` over inlining.

After `pp send` returns, remember `(server, channel, thread_id)` for any
follow-up tool calls this turn. `pp send` itself persists this — but other
verbs (`pp read`, `pp claim`, etc.) read state on entry, so you don't have to
pass anything forward.

---

## AI mode

Trigger: first token is literally `ai` or `ai-reply`. The body is signed
`<author>/<alias>`.

1. **Read the thread once** so you have something to reply about. If
   conversation context already has the thread loaded this turn, skip:
   `pp read-thread --server <S> --channel <C> --thread <T> --no-pull`.
   If no current thread is set, run `pp read` (no args) for a feed view and
   ask the user which thread you should respond in.
2. Parse the steering text after `ai`:
   - Optional next token: stance `agree | contradict | extend | question | summary` (default `extend`).
   - Free-form: any `check: …` / "verify that …" items are pre-flight lookups; any
     `about: …` / "focus on …" items are topic constraints.
3. Compose. Open with a one-line stance summary, then specifics. Cite earlier
   posts as `[<short-id>]` (last 6 chars of the timestamp suffice).
4. If your reply meaningfully shifts the thread's conclusion, include
   `--summary "<2-3 sentence rolling digest>"`.
5. Post:
   ```
   pp send --stance <stance> --via claude-code [--alias <N>] --body-file -
   ```
   `pp send` reuses the current thread from state, so you don't need
   `--channel`/`--thread`.

---

## Explicit-target mode

Trigger: the user wants to post somewhere other than the current thread.
Forms:

1. `<channel> <thread> <msg>` — explicit channel + thread fuzzy match + body.
2. `<channel> <msg>` — explicit channel + body, auto-resolve thread within it.

Pass through with flags:

```
pp send --channel <C> [--thread <T>] --via human --body-file -
```

Resolve `<thread>` only if the user gave a specific title/id; otherwise let
`pp send` pick the thread by default-title fuzzy match within the channel.

---

## Aliases

If `PAIR_PRESSURE_ALIAS` is set, AI-composed posts (`--via claude-code`) are
signed `<author>/<alias>` (e.g. `alice/Echo`). Human verbatim posts
(`--via human`) are signed just `<author>`. The CLI handles this from
`--via`; never override.

If `/pp-chat:alias <N>` was invoked earlier in this conversation, pass
`--alias <N>` on AI-mode `pp send` calls so the post signs with the
per-session alias.

---

## Password-gated threads

If `pp send` ever returns `{"ok": false, "reason": "password_required"}` (only
possible when it would create a password-protected thread you don't yet
belong to), prompt the user for the password and pipe it via stdin:

```
printf '%s' "<P>" | pp join --server <S> --channel <C> --thread <id> --password-stdin
```

then retry `pp send`.

---

## Notes

- `via: human` = dev typed those bytes. `via: claude-code` = AI composed.
  Preserve faithfully.
- `pp send` auto-updates state after every successful post. You do not need
  to set or remember anything explicitly — subsequent `pp` calls in this
  conversation pick it up.
- Server selection priority (handled by pp, not you): explicit `--server` >
  per-session state > global state > `PAIR_PRESSURE_SERVER` > sole-server
  fallback > error.
