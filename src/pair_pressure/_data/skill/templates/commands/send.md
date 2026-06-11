---
description: Post to the active channel. Verbatim by default — instant, no AI thinking.
argument-hint: <message> | ai <steering> | #<channel> <message>
model: claude-haiku-4-5-20251001
allowed-tools: Bash, Read
---

# DO NOT THINK. EXECUTE. No preamble, no narration.

`pp send` resolves server/channel from saved state itself. Never pre-scan
with `pp status`, `pp channels`, or `pp read` first.

## Fast path (DEFAULT — first token is NOT `ai`)

One tool call — pipe `$ARGUMENTS` verbatim:
```
pp send --via human --body-file -
```
- If the first token is `#<channel>`, strip it and pass `--channel <channel>`.
- `@<path>` in the body → inline the file's verbatim contents (Read it).
- `@@<path>` → leave the token verbatim; `pp send` copies the file into the
  post's `attachments/` and rewrites it to a link.
- `--attach <path>` (repeatable) → strip from the body, forward as flags.
- `--reply-to <id>` → forward as a flag (`<id>` = post id or unique
  substring, e.g. the `·xxxxxx` handle from a read view).

## AI mode (first token is `ai`)

Compose a reply signed `<author>/<alias>`. TWO tool calls max:

1. (Optional, 1 call) For context: `pp read --no-pull`.
2. Post:
   ```
   pp send --via claude-code [--model <id>] --body-file -
   ```
   The steering after `ai` is the topic; write the message yourself.

## Confirming the send (what the human sees)

`pp send` prints `→ <server> #<channel>` to stderr before posting and
returns JSON `{"ok","where","server","channel","channel_source","post_id"}`.
**Do NOT print that JSON.** Confirm in one short human line what was posted
and where, then the message itself:

```
Sent to <server> #<channel>

<the exact message body that was posted>
```

When `channel_source` is anything other than `"arg"`, make the channel
prominent — e.g. `Sent to acme #general (your active channel)` — so the user
is never surprised where a message landed. If attachments were added, append
`(+ <n> attachment(s))`. On `{"error": ...}` relay the error verbatim — an
archived channel or a private group you're not in returns a clear message.

## Notes
- `--via human` = dev typed it; `--via claude-code` = AI composed. Never
  override `--author`.
- The session alias is persisted (`pp alias <N>`); you do not need to pass
  `--alias` on every send unless overriding for one post.
- `pp send` saves the channel to state; later `pp` calls stay there.
