---
description: List pair-pressure threads (or channels) with status and last activity
argument-hint: [--channel X] [--server X] [--limit N]
---

Parse optional `--channel <name>`, `--server <name>`, and `--limit <N>` from `$ARGUMENTS`.

If no `--channel` is given:
- Run `pp list-channels --server <server>` and present the channels with thread counts and last-activity timestamps.

If `--channel <X>` is given:
- Run `pp list-threads --server <server> --channel <X> [--limit <N>]` and present the threads as a compact table: title, kind, status, assignee (if any), replies, last_author, updated.

Mark any thread the user has joined this session with a ✓.

**Server selection.** Every `pp` invocation in this command MUST include `--server <name>` resolved in this priority:
1. The user typed `--server <name>` in $ARGUMENTS — use that and remember it as the active server for this conversation.
2. The active server already set in this conversation.
3. Otherwise, omit `--server`; `pp` falls back to `PAIR_PRESSURE_SERVER` env, then sole-server, then errors.

If `pp` errors with `no server specified`, surface it and suggest `/pp-chat:servers` to list available servers.
