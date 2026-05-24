---
description: Show or set pair-pressure offline mode (commits stay local; fetch/pull/push skipped).
argument-hint: [true|false]
model: claude-haiku-4-5-20251001
allowed-tools: Bash(pp *)
---

Parse the first token of `$ARGUMENTS`. No token = show status.

### Show (no args)

```
pp offline
```
Response: `{"offline":<bool>,"source":"env|config|default","env":<...|null>,"config":<...|null>}`
Tell the user whether offline mode is ON or OFF and where it came from. If
`source` is `env`, note `PAIR_PRESSURE_OFFLINE` overrides the saved config.

### Set

```
pp offline true     # go offline: commits still happen locally; no fetch/pull/push
pp offline false    # go online: subsequent verbs sync normally
```
Response: `{"offline":<bool>,"saved":true,...}`. If a `"warning"` field is
present (env override set), relay it — the env var still wins until unset.

Notes:
- Machine-global, persists across sessions (`~/.pair-pressure/config.json`).
  NOT stored in the chat repo.
- While offline, write verbs still `git commit` locally and sync the next
  time you run an online verb (`pp pull` / any push) after `pp offline false`.
- A server whose worktree was never materialized cannot be created while
  offline (needs network) — `pp` returns a clear error if so.
