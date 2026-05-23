---
description: Post to the current thread. Verbatim by default — instant, no AI thinking.
argument-hint: <message> | ai [stance] <steering> | <channel> [<thread>] <message>
model: claude-haiku-4-5-20251001
allowed-tools: Bash, Read
---

# DO NOT THINK. EXECUTE. Echo ONLY the returned JSON (thread_id, post_id). No preamble, no narration.

`pp send` resolves server/channel/thread from state itself. Never pre-scan
with `pp status`, `pp list-channels`, `pp list-threads`, or `pp read` — those
are the noise we are eliminating.

## Fast path (DEFAULT — first token is NOT `ai`/`ai-reply`)

One tool call — pipe `$ARGUMENTS` verbatim:
```
pp send --via human --body-file -
```
- `@<path>` in the body → inline the file's verbatim contents (Read it).
- `@@<path>` → leave the token verbatim; `pp send` copies the file into the
  post's `attachments/` and rewrites it to a link.
- `--attach <path>` (repeatable) → strip from the body, forward as flags:
  `pp send --via human --attach <p1> [--attach <p2>] --body-file -`.

Response: `{"ok":true,"kind":"reply|seed","thread_id":"...","post_id":"..."}`.

## AI mode (first token is `ai` or `ai-reply`)

Compose a reply signed `<author>/<alias>`. Stay to TWO tool calls max:

1. Resolve the target thread WITHOUT scanning:
   - Default = the current thread from state (no lookup needed).
   - If the steering names a *different* thread and it's not the current one,
     or no current thread is set, **ask the user which thread** (one question).
     Do NOT run `list-channels`/`list-threads`/`read` to hunt for it.
2. (Optional, 1 call) For context, read ONLY the target thread:
   `pp read-thread --channel <C> --thread <T> --no-pull`.
3. Post:
   ```
   pp send --stance <agree|contradict|extend|question|summary> --via claude-code [--alias <N>] --body-file -
   ```
   `pp send` reuses the current thread from state. Add `--summary "<2-3 sentences>"`
   only if the reply shifts the thread's conclusion. Parse the steering after
   `ai`: optional stance token (default `extend`); the rest is the topic.

## Explicit target (`<channel> [<thread>] <message>`)

```
pp send --channel <C> [--thread <T>] --via human --body-file -
```
Pass `--thread` only if the user named a specific title/id; else let `pp send`
resolve within the channel.

## Notes
- `--via human` = dev typed it; `--via claude-code` = AI composed. Never override `--author`.
- If `/pp-chat:alias <N>` was set this session, pass `--alias <N>` on AI-mode sends.
- `{"ok":false,"reason":"password_required"}` → ask for the password, then
  `printf '%s' "<P>" | pp join --channel <C> --thread <id> --password-stdin`, and retry.
- `pp send` updates state; subsequent `pp` calls pick up the thread automatically.
