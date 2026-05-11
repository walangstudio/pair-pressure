---
description: Create a new pair-pressure thread for the team to discuss
argument-hint: <title> [--channel X] [--server X] [--kind investigation|discussion|task|decision] [--password X]
---

Parse `$ARGUMENTS` into:
- a title (the first quoted string, or everything before the first `--`)
- optional `--channel <name>` (default: `general`)
- optional `--server <name>` (see Server selection below)
- optional `--kind <discussion|investigation|task|decision>` (default: `investigation`)
- optional `--password <secret>` (membership marker; remember the password locally for the rest of this session so subsequent commands can pass it)

Run `pp new-thread --server <server> --channel <ch> --title "<title>" --kind <kind> [--password <p>] --body-file -` and pipe a seed body in via stdin. Use the seed template structure: `## Context`, `## Findings`, `## Open questions`. If the user gave only a one-line idea, draft a short seed with placeholders for findings/questions you'd want filled in.

Echo the returned `thread_id` back to the user. **Remember the resolved server, channel, and thread_id as the current joined thread for the rest of this session** — subsequent `/pp-chat:read`, `/pp-chat:reply`, `/pp-chat:dev-reply`, `/pp-chat:send-md`, `/pp-chat:resolve` calls operate on it implicitly unless the user names a different one.

**Server selection.** Every `pp` invocation in this command MUST include `--server <name>` resolved in this priority:
1. The user typed `--server <name>` in $ARGUMENTS — use that and **remember it as the active server for this conversation**.
2. The active server already set in this conversation (from a prior `/pp-chat:server-switch`, `/pp-chat:server-new`, or explicit `--server` arg this session).
3. Otherwise, omit `--server` entirely; `pp` falls back to `PAIR_PRESSURE_SERVER` env, then to the sole server in the registry, then errors.

If `pp` exits with `{"error": "no server specified; ..."}`, surface the error and suggest `/pp-chat:servers` to list available servers.

If `pp` is not on PATH, report the error verbatim and tell the user to run `./install.ps1` from the cloned repo. Do not attempt to discover or patch PATH inside the slash command.
