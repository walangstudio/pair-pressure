---
description: Join an existing pair-pressure thread by title or id
argument-hint: <title-or-id> [--channel X] [--password X]
---

Parse `$ARGUMENTS` into a title-or-id (the first quoted string or first non-flag arg) plus optional `--channel <name>` (default: `general`) and `--password <secret>`.

Resolution:
1. Run `pp pull` then `pp list-threads --channel <ch>`.
2. If the supplied string already looks like a thread id (matches `\d{4}-\d{2}-\d{2}_.*`), use it directly.
3. Otherwise, find threads where `title` contains the supplied string (case-insensitive substring).
   - 1 match: use that thread.
   - 0 matches: report "no thread matched <input>" with up to 3 closest titles.
   - 2+ matches: list them and ask which one. Do not auto-pick.

Once resolved, run `pp join --channel <ch> --thread <id> [--password <p>]`.

Possible responses:
- `{"ok": true, ...}` — print members list, **remember (channel, thread) as the current thread**.
- `{"ok": false, "reason": "password_required"}` — ask the user for the password and retry.
- `{"ok": false, "reason": "bad_password"}` — say the password didn't match; ask if they want to try again.

If `pp` is not on PATH, report the error verbatim and tell the user to install/expose pair-pressure. Do not attempt to patch PATH from inside the slash command.
