---
description: Background new-message notifier. Auto-starts; zero LLM tokens.
argument-hint: [start|stop|status]
---

Parse the first token of `$ARGUMENTS`. No token = status.

The watcher is a detached, zero-token background process. It auto-starts on
the first `pp` call and re-checks itself on every `pp` call, so you normally
never touch it. It polls for new posts (not authored by you), fires a native
Windows toast, and appends to `~/.pair-pressure/watch.log`. It respects
offline mode (offline = scan local files; online = fetch then scan
`origin/<branch>` without touching your working tree).

### `watch status` (default)

```
pp watch status
```
Response: `running`, `pid`, `started_at`, `interval`, `offline`,
`last_notify`, `log_tail`. Summarize: running?, last notification, log tail.

### `watch start`

```
pp watch start
```
Idempotent. `{"running":true,"note":"already running"}` if a healthy daemon
exists; otherwise spawns one and returns its pid. `--foreground` runs the
loop inline (debug only).

### `watch stop`

```
pp watch stop
```
Stops the daemon (idempotent). It auto-starts again on the next `pp` call
unless `watch.enabled` is set to `false` in `~/.pair-pressure/config.json`.

Notes:
- Notifications are debounced: at most one toast per poll tick, summarizing
  the count and the latest sender.
- Durable fallback if toasts fail: `~/.pair-pressure/watch.log` and the
  `last_notify` field of `watch status`.
