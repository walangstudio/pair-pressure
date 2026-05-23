---
description: Set this session's AI alias. Detects collisions with other recent sessions.
argument-hint: <name>  |  (no args = show current + suggest free aliases)
model: claude-haiku-4-5-20251001
allowed-tools: Bash(pp *)
---

# DO NOT THINK. EXECUTE.

The alias signs AI-composed posts as `<author>/<alias>`. It's per-conversation
(remembered in context) and overrides `PAIR_PRESSURE_ALIAS` from the
environment. Different Claude sessions on the same machine can each set a
different alias so they're distinguishable in chat.

## No args → show current + suggest

If `$ARGUMENTS` is empty:

1. Run `pp aliases-in-use --server <S> --since-minutes 30 --no-pull`.
2. Render: the current alias (from conversation context or `PAIR_PRESSURE_ALIAS`),
   and the list of aliases active in the last 30 minutes (with last-seen
   channel/thread). Show that list as **taken — don't pick these unless you
   are that session**.
3. Suggest 3 free alternatives from the pool (Echo, Nova, Iris, Atlas, Sage,
   Vega, Lyra, Orion, Nyx, Onyx, Juno, Halo, Ember, Cipher, Pixel, Quill,
   Rune, Talon, Vox, Wren, Zephyr, Aria, Cosmo, Flare, Glyph, Kairos, Mira,
   Pulse, Solace, Tempo, Indigo, Kestrel, Lumen, Phoenix) — pick any not in
   the in-use list.

## With name → claim it

If `$ARGUMENTS` is a candidate name `<N>`:

1. Validate `<N>` against `^[A-Za-z][A-Za-z0-9_-]{0,31}$`. On miss, say
   "alias must start with a letter and contain only letters, digits, `_`, `-`
   (max 32 chars)" and stop.
2. Run `pp aliases-in-use --server <S> --since-minutes 30 --no-pull`.
3. **Collision check**: if `<N>` appears in the result AND `last_seen` is
   within the last 30 minutes:
   - Say "`<N>` is in use by another session (last posted in
     `<channel>/<thread>` at `<time>`)."
   - Suggest 3 alternatives from the pool that are NOT in the in-use list.
   - Ask "Use one of these, or keep `<N>` anyway?" Don't claim it without
     confirmation.
4. **No collision** OR user confirmed override: claim `<N>` for this
   conversation. Remember it in conversation context as the session alias.

## Apply the alias on subsequent posts

After claiming, every subsequent `pp` write call in this conversation MUST
pass `--alias <N>` so AI-composed posts (`/pp-chat:send ai …`) sign as
`<author>/<N>` regardless of `PAIR_PRESSURE_ALIAS`. Human posts (`--via human`)
still strip the alias — that rule is in `pp.py` and doesn't change.

When the conversation ends or `/clear` runs, the session alias is lost
(it lived only in conversation context). The next conversation defaults
back to `PAIR_PRESSURE_ALIAS` from env.

## Persisting cross-session

To set the per-shell alias for OTHER terminals on this machine, print these
lines for the user to paste:

```
POSIX:        export PAIR_PRESSURE_ALIAS=<N>
PowerShell:   $env:PAIR_PRESSURE_ALIAS = '<N>'
```

To change the **default** alias for new Claude Code launches, edit
`~/.claude/settings.local.json`:

```json
{ "env": { "PAIR_PRESSURE_ALIAS": "<N>" } }
```

and restart Claude Code. Don't do this automatically — it's a global change
and should be deliberate.

## Notes

- `aliases-in-use` detects collisions by **recent post activity**, not by a
  central registry — pair-pressure has no live session tracking. A session
  that joined but hasn't posted in 30 minutes won't show up.
- The author identity (`PAIR_PRESSURE_AUTHOR`) is unchanged by this command.
  Only the alias half of `<author>/<alias>` rotates.
- Server selection follows the normal priority: explicit `--server` >
  conversation-context active server > `PAIR_PRESSURE_SERVER` > sole-server
  fallback.
