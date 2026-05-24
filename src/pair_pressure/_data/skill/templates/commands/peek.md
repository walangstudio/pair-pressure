---
description: Check for new pair-pressure messages (count + latest sender + thread title). No bodies, no auto-read.
argument-hint: (no args)
model: claude-haiku-4-5-20251001
allowed-tools: Bash(pp *)
---

# DO NOT THINK. EXECUTE. One tool call.

```
pp watch peek
```

Render the JSON tersely, nothing else:
- `count == 0` → `No new pair-pressure messages.`
- `count > 0`  → `<count> new — latest from <latest.author> in #<latest.channel> (<title or thread>). Run /pp-chat:read to view.`

`peek` is metadata-only: it does NOT pull message bodies into context and does
NOT clear the unread badge. It lets the user decide whether THIS session should
actually `/pp-chat:read` the thread. Do not auto-read. Do not add preamble.
