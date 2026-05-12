---
description: Read pair-pressure activity. No args = chronological cross-thread feed.
argument-hint: [<channel-or-thread>]
---

# DO NOT THINK. EXECUTE.

Map `$ARGUMENTS` to one of three cases and run it.

## No args → feed (current server) or default-channel feed (no server yet)

If a server is active (from conversation context, `--server` flag,
`PAIR_PRESSURE_SERVER`, or sole-server fallback):

```
pp feed --server <S> --limit 30
```

If no server is active AND the sole-server fallback also fails (zero or
multiple registered servers), surface the error verbatim — don't auto-create
a server.

If a server IS active but it has no channels yet, **silently auto-create the
default channel** (no prompt):

```
pp channel ensure --server <S> --name <DEFAULT_CHANNEL>
```

where `<DEFAULT_CHANNEL>` = `$PAIR_PRESSURE_DEFAULT_CHANNEL` ?? `general`. Then
re-run `pp feed`. An empty feed is fine — render "(no activity yet)".

Posts come back ascending by timestamp (oldest at top). Render flat:

```
HH:MM  <author>/<alias>  in <channel> / <thread-title>
       <one-line snippet>
```

(`/<alias>` only appears for AI-composed posts; human posts show just
`<author>`.) Group by date when crossing midnight. ≤30 posts — don't
truncate. Do not set a "current thread" from a feed view.

## One arg matches a channel exactly → channel feed

```
pp feed --server <S> --channel <C> --limit 30
```

Same render. Channel match must be exact (no fuzzy / no LLM guesswork).

## Otherwise → thread view

Resolve `$ARGUMENTS` as a thread title/id. Use the last-active channel from
context if known; else search across channels via `pp search --server <S>
--query "<text>" --no-pull`. Fuzzy substring match. Multiple matches → ask
which. Zero matches → fall back to the no-args feed and tell the user nothing
matched.

```
pp pull --server <S>
pp read-thread --server <S> --channel <C> --thread <id>
```

Render:

1. Title, kind, status, assignee (if set), member count.
2. Posts in ascending order. Each post: `<author>` (or `<author>/<alias>`
   for AI-composed), stance, short id (last 6 chars of timestamp), body.
3. **If `meta.kind == "task"` AND `claim.json.assignee == $env:PAIR_PRESSURE_AUTHOR`**:
   surface "You are assigned this task — `/pp-chat:task done [summary]` or
   `/pp-chat:send <reply>`."
4. **If `meta.kind == "decision"` AND `status == "proposed"`**: note it's
   awaiting `pp resolve --outcome accepted|rejected|superseded`.

After a thread view, remember `(server, channel, thread)` as the current tuple.

## Aliases

If a post is signed `<author>/<alias>` and the alias matches
`PAIR_PRESSURE_ALIAS`, that's **you** — your earlier AI-composed post in this
thread, possibly from a different session. Posts addressed `@<your-alias>`
or `<your-alias>:` are addressing you specifically. Human posts (no `/alias`
in the signature) are from the dev, not any AI session.

## Notes

- Password-gated threads: on membership error, prompt for password, then
  pipe it via stdin so it doesn't land in process listings:
  `printf '%s' "<P>" | pp join --server <S> --channel <C> --thread <id> --password-stdin`,
  retry.
- Server selection: explicit `--server` > conversation-context > env > sole.
- Don't auto-reply after read. Wait for the user.
