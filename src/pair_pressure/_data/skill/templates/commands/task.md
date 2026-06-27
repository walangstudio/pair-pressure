---
description: 'Per-channel task checklist with hand-off: new, list, done, claim, assign, release.'
argument-hint: '<list [--all] | new "<title>" | done|claim|release <ref> | assign <ref> <user>>'
model: claude-haiku-4-5-20251001
allowed-tools: Bash(pp *)
---

# DO NOT THINK. EXECUTE.

Parse the first token of `$ARGUMENTS` as the subcommand. Tasks live in the
active channel's `tasks.json` (pass `--channel <C>` only if the user names
one).

### `task list`

```
pp task list [--all]
```
Open tasks by default; `--all` includes done. Render a compact table:
`#<id>  title  status  assignee  by`. Response:
`{"where": "...", "channel": "...", "tasks": [{"id","title","status","assignee","by","at",...}]}`.

### `task new <title>`

```
pp task new "<title>"
```
Title = the remaining text (strip surrounding quotes). Response includes the
created task with its `id` — echo `Added task #<id>: <title> in <where>`.

### `task done <ref>`

```
pp task done "<ref>"
```
`<ref>` = `#<id>`, `<id>`, or a title substring. On
`{"error": "... matches N tasks ..."}` tell the user to use the `#id`. On
`already_done: true` say it was already done. Otherwise echo
`Done: #<id> <title>`.

### Hand-off: `task claim` / `assign` / `release`

```
pp task claim "<ref>"           # take it yourself   -> status "claimed"
pp task assign "<ref>" <user>   # hand to someone    -> status "claimed"
pp task release "<ref>"         # give it back       -> status "open"
```
`claim` fails if another user holds it (relay the error: ask them to
`release`, or use `assign`). `release` on an unheld task returns
`already_open: true`. Echo the result as `#<id> <title> -> <assignee|open>`.

**Server/channel selection** is internal to `pp`: flag > session state >
global state > default. You rarely pass `--server`/`--channel`.
