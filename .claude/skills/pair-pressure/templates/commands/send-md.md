---
description: Attach a markdown file as a reply to the current thread
argument-hint: <path-to-md> [stance: agree|contradict|extend|question|summary]
---

Parse `$ARGUMENTS`:
- First token: a path to a markdown file. Resolve relative paths against the user's current working directory.
- Optional second token: stance (default `extend`).

Verify the file exists and is readable. If not, error out with the path you tried.

Use the **current joined thread** from this session's context. Refuse if none.

Run:
```
pp reply --channel <ch> --thread <id> --stance <stance> --via human --body-file "<path>"
```

Do NOT modify the file or its content — `--via human` signals the dev's verbatim authoring.

Echo the returned `reply_id`, the byte size of the attached file, and a one-line note "attached <basename> as <reply_id>".
