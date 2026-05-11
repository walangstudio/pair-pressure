---
description: Create a task thread, optionally assigning it to a specific dev
argument-hint: <title> [--to <user>] [--channel X] [--password X] [body text]
---

Parse `$ARGUMENTS`:
- Title: first quoted string, or everything before the first `--`.
- Optional `--to <user>`: assignee handle.
- Optional `--channel <name>` (default: `general`).
- Optional `--password <secret>`.
- Remaining tokens (after flags): treated as the body. If absent, draft a short body that states the goal, what "done" looks like, and any known constraints. Use the seed template (`## Context`, `## Findings`, `## Open questions`).

Run `pp new-thread --channel <ch> --title "<title>" --kind task [--password <p>] --body-file -` with the body on stdin.

If `--to <user>` was given:
1. Note the new thread_id from the response.
2. Run `pp claim --channel <ch> --thread <thread_id>` (you must claim before you can hand off). If the claim fails, surface the error.
3. Run `pp handoff --channel <ch> --thread <thread_id> --to <user>` to reassign to the target.

Echo the resulting thread_id, kind=task, and assignee (if set). Remember (channel, thread_id) as the current thread for this session.
