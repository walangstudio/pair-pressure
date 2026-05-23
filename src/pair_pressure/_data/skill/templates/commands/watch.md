---
description: Background new-message notifier. Auto-starts; zero LLM tokens.
argument-hint: [start|stop|status|unread|ack|interval <Nm>|wire [--nudge|--undo]]
model: claude-haiku-4-5-20251001
allowed-tools: Bash(pp *)
---

Parse the first token of `$ARGUMENTS`. No token = status.

The watcher is a detached, zero-token background process. It auto-starts on
the first `pp` call and re-checks itself on every `pp` call, so you normally
never touch it. It polls for new posts (not authored by you) **in both online
and offline mode** (offline = scan local files; online = fetch then scan
`origin/<branch>` without touching your working tree), fires a native Windows
toast, appends to `~/.pair-pressure/watch.log`, and bumps an unread counter
`~/.pair-pressure/unread.json`.

### How the alert reaches the console

- **Statusline badge — 0 tokens (recommended).** `pp watch wire` sets a
  standalone pair-pressure statusline. The model never sees it, so it costs
  nothing. When idle it renders empty; on unread it shows
  `[pp 3 new alice #general]`; when offline mode is on it shows
  `[pp (offline)]` (or `[pp (offline) 3 new alice #general]`). It
  **replaces** any previous statusline; the prior command is saved so
  `pp watch wire --undo` restores it exactly.
- **In-prompt nudge — INCURS TOKEN COST (opt-in).** `pp watch wire --nudge`
  also adds a `UserPromptSubmit` hook that injects one short line
  (`[pair-pressure] N new messages ... /pp-chat:read`) into your next prompt
  when there are unread messages, then clears. ~15-25 tokens, once per batch,
  only when there is news. Tell the user this has a real usage cost.
- The unread badge **auto-clears when you run `/pp-chat:read`** (any read
  verb), or `pp watch ack`, or the nudge fires.

### `watch status` (default)

```
pp watch status
```
Response: `running`, `pid`, `started_at`, `interval`, `interval_source`,
`offline`, `last_notify`, `unread`, `log_tail`.

### `watch start` / `watch stop`

`start` is idempotent (`--foreground` runs the loop inline, debug only).
`stop` is idempotent; the daemon auto-starts again on the next `pp` call
unless `watch.enabled` is `false` in `~/.pair-pressure/config.json`.

### `watch unread [--format json|line]`

```
pp watch unread            # {"count":N,"latest":{...}}
pp watch unread --format line   # "[pp:N]" or "" (statusline use)
```

### `watch ack`

Clear the unread counter manually (`{"acked":true}`).

### `watch interval [<value>]`

```
pp watch interval          # show resolved seconds + source
pp watch interval 5m       # poll every 5 minutes (accepts 90, 90s, 5m, 1h)
```
Persisted to `~/.pair-pressure/config.json` (`watch.interval`, seconds).
Precedence: env `PAIR_PRESSURE_WATCH_INTERVAL` > config > default 300s
(5 min); min 5s. The running daemon re-reads each tick — no restart needed.

### `watch wire [--nudge] [--undo]`

```
pp watch wire              # statusline badge only (0 tokens)
pp watch wire --nudge      # ALSO add the token-costing prompt nudge
pp watch wire --undo       # restore original statusline + remove the hook
```
Idempotent; backs up `~/.claude/settings.json` to `settings.json.pp.bak`
once; preserves your existing statusline command and any existing
`UserPromptSubmit` hooks. Restart Claude Code (or start a new session) for
the statusline/hook change to load. When `--nudge` is used, relay the
returned `cost_warning` to the user.

Notes:
- Notifications work the same online or offline; debounced to ≤1 toast per
  poll tick, summarizing count + latest sender.
- Durable fallback if toasts fail: `~/.pair-pressure/watch.log` and the
  `last_notify` field of `watch status`.
