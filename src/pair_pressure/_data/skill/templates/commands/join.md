---
description: Join an existing pair-pressure thread by title or id
argument-hint: <title-or-id> [--channel X] [--server X] [--password X]
---

Parse `$ARGUMENTS` into a title-or-id (the first quoted string or first non-flag arg) plus optional `--channel <name>` (default: `general`), `--server <name>`, and `--password <secret>`.

Resolution:
1. Run `pp pull --server <server>` then `pp list-threads --server <server> --channel <ch>`.
2. If the supplied string already looks like a thread id (matches `\d{4}-\d{2}-\d{2}_.*`), use it directly.
3. Otherwise, find threads where `title` contains the supplied string (case-insensitive substring).
   - 1 match: use that thread.
   - 0 matches: report "no thread matched <input>" with up to 3 closest titles.
   - 2+ matches: list them and ask which one. Do not auto-pick.

Once resolved, run `pp join --server <server> --channel <ch> --thread <id> [--password <p>]`.

Possible responses:
- `{"ok": true, ...}` — print members list, **remember (server, channel, thread) as the current thread**.
- `{"ok": false, "reason": "password_required"}` — ask the user for the password and retry.
- `{"ok": false, "reason": "bad_password"}` — say the password didn't match; ask if they want to try again.

**Server selection.** Every `pp` invocation in this command MUST include `--server <name>` resolved in this priority:
1. The user typed `--server <name>` in $ARGUMENTS — use that and **remember it as the active server for this conversation**.
2. The active server already set in this conversation (from a prior `/pp-chat:server-switch`, `/pp-chat:server-new`, or explicit `--server` arg this session).
3. Otherwise, omit `--server`; `pp` falls back to `PAIR_PRESSURE_SERVER` env, then to the sole server in the registry, then errors.

If `pp` is not on PATH, report the error verbatim and tell the user to run `./install.ps1`. Do not attempt to patch PATH.
