---
description: List pair-pressure threads (or channels) with status and last activity
argument-hint: [--channel X] [--limit N]
---

Parse optional `--channel <name>` and `--limit <N>` from `$ARGUMENTS`.

If no `--channel` is given:
- Run `pp list-channels` and present the channels with thread counts and last-activity timestamps.

If `--channel <X>` is given:
- Run `pp list-threads --channel <X> [--limit <N>]` and present the threads as a compact table: title, kind, status, assignee (if any), replies, last_author, updated.

Mark any thread the user has joined this session with a ✓.
