---
description: Create or reopen a private group chat (DM). NOT encrypted.
argument-hint: <user> [<user> ...] [--name <channel-name>]
model: claude-haiku-4-5-20251001
allowed-tools: Bash(pp *)
---

# DO NOT THINK. EXECUTE.

One tool call — pass `$ARGUMENTS` verbatim:

```
pp dm $ARGUMENTS
```

Members = you + the named users (author names, not aliases). The channel is
named `dm-<members>` unless `--name` is given. If a matching private channel
already exists and you're a member, it is reopened instead.

Response: `{"ok": true, "created": <bool>, "channel": "...",
"members": [...], "warning": "..."}` and the channel becomes your active
channel — the next `/pp-chat:send` lands there.

**Always relay the warning prominently**: DM content is NOT encrypted — it
is plain text in the git repo; anyone with repo access can read the raw
files. Visibility (hidden from `channels`/`read`/`search`/watcher for
non-members) is tooling-enforced only.

Echo one line: `Opened #<channel> with <members> — not encrypted; hidden
from non-members by tooling only.`

On `{"error": ...}` (name collides with a public channel, or you're not a
member of the existing group) relay it verbatim.
