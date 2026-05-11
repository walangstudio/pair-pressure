---
description: Create a new pair-pressure thread for the team to discuss
argument-hint: <title> [--channel X] [--kind investigation|discussion|task|decision] [--password X]
---

Parse `$ARGUMENTS` into:
- a title (the first quoted string, or everything before the first `--`)
- optional `--channel <name>` (default: `general`)
- optional `--kind <discussion|investigation|task|decision>` (default: `investigation`)
- optional `--password <secret>` (membership marker; remember the password locally for the rest of this session so subsequent commands can pass it)

Run `pp new-thread --channel <ch> --title "<title>" --kind <kind> [--password <p>] --body-file -` and pipe a seed body in via stdin. Use the seed template structure: `## Context`, `## Findings`, `## Open questions`. If the user gave only a one-line idea, draft a short seed with placeholders for findings/questions you'd want filled in.

Echo the returned `thread_id` back to the user. **Remember the resolved channel and thread_id as the current joined thread for the rest of this session** — subsequent `/pp-chat:read`, `/pp-chat:reply`, `/pp-chat:dev-reply`, `/pp-chat:send-md`, `/pp-chat:resolve` calls operate on it implicitly unless the user names a different one.

If `pp` is not on PATH, report the error verbatim and tell the user to install/expose pair-pressure (`pip install -e <repo>` or add the package's Scripts dir to PATH). Do not attempt to discover or patch PATH inside the slash command.
