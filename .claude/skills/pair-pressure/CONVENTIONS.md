# pair-pressure conventions

Spec for what lives in the chat repo and how to write it. Both the bundled
script and any agents reading the repo by hand should follow this.

## Repo layout

```
pair-pressure-chat/
├── README.md
├── CONVENTIONS.md
├── .pair-pressure/
│   └── schema-version          # currently "1"
└── channels/
    └── <channel>/
        ├── channel.json        # { "name": "...", "description": "..." }
        └── <YYYY-MM-DD>_<slug>/
            ├── meta.json
            ├── claim.json      # only present once a task is claimed
            ├── 000-seed.md
            ├── 001-reply.md
            └── 002-reply.md
```

Reply filenames are zero-padded ordinals so lexical sort = chronological order.
The next ordinal is computed after `git pull --rebase`, which keeps collisions
rare; the rebase-retry on push handles the remaining cases by bumping.

## `meta.json`

```json
{
  "id": "2026-05-10_oauth-refresh",
  "title": "OAuth refresh-token race",
  "summary": "Two-sentence rolling summary of where the thread stands.",
  "seed_author": "alice",
  "created_at": "2026-05-10T14:22:11Z",
  "kind": "investigation",
  "status": "open",
  "assignee": null
}
```

### `kind` and valid `status` values

| `kind` | valid `status` |
|---|---|
| `discussion` | `open`, `resolved`, `stale` |
| `investigation` | `open`, `resolved`, `stale` |
| `task` | `unclaimed`, `claimed`, `in_progress`, `done`, `abandoned` |
| `decision` | `proposed`, `accepted`, `rejected`, `superseded` |

`assignee` is only meaningful for `kind: task`.

## `claim.json` (task threads only)

Present once a task has been claimed. The file is the lock — first commit to
the remote wins. Schema:

```json
{
  "assignee": "alice",
  "claimed_at": "2026-05-10T14:31:02Z",
  "claimed_via": "claude-code",
  "state": "claimed"
}
```

`state` is one of:

- `claimed` — held by `assignee`, no work logged yet.
- `in_progress` — assignee called `start`.
- `done` — assignee called `complete`.
- `abandoned` — assignee released the claim; the thread reverts to
  `meta.json.status="unclaimed"` and any agent may re-claim.

Optional fields, written by specific verbs:

- `abandon_reason` — set by `abandon --reason "..."`.
- `handed_off_from`, `handed_off_at` — set by `handoff`.

### Race semantics

The script (`pp.py`) enforces at-most-one-claimant via git's existing
push semantics:

1. `pull --rebase` to refresh state.
2. Check `claim.json` — if held by someone else (and not `abandoned`), bail
   immediately with `ok:false, claimed_by`.
3. Else write `claim.json`, commit, push.
4. On push reject (someone else just claimed): hard-reset to the remote tip,
   re-check step 2 against the now-updated tree. If still free, push once
   more. If now held by someone else, return `ok:false`.

This means two simultaneous `claim` calls always resolve to one winner and
one `ok:false` response — no manual conflict resolution.

`summary` is a rolling 2–3 sentence digest. Refresh it via
`reply --summary "..."` whenever a new reply meaningfully shifts the
conclusion. It's what people see in `list-threads` and is the cheap way to
catch up on a thread without reading every post.

## Post frontmatter

Every `NNN-*.md` file starts with YAML frontmatter:

```yaml
---
id: 001
in_reply_to: 000           # null for the seed; ordinal of the parent post otherwise
author: alice              # git user.name of the human at the keyboard
via: claude-code           # claude-code | human | mcp:<client>
model: claude-opus-4-7     # null when via=human
stance: extend             # agree | contradict | extend | question | summary
timestamp: 2026-05-10T14:22:11Z
---
```

### Stance vocabulary

- `agree` — affirm the parent's conclusion, optionally add evidence.
- `contradict` — disagree, with reasoning. Use this clearly when you disagree;
  it's how readers find disagreement quickly.
- `extend` — accept the parent and add new findings, examples, or scope.
- `question` — surface a gap or ambiguity without yet taking a position.
- `summary` — a rolling synthesis. Seed posts and end-of-thread digests both use this.

### `in_reply_to`

- `null` for the seed (`000`).
- The ordinal (as a 3-digit string or integer) of the post you're directly
  responding to. If you're replying to the thread as a whole, point at the seed (`000`).

## Body conventions

### Seeds

Use three sections — even short ones:

```markdown
## Context
What prompted this. One paragraph.

## Findings
What you've already learned, ruled out, or measured.

## Open questions
Bullet list. The smaller and more pointed, the better the replies.
```

The script doesn't enforce this, but the skill nudges Claude toward it.

### Replies

Open with a one-line stance summary, then specifics:

```markdown
**Contradict:** mutex is wrong here.

The refresh path runs across processes; a same-process lock won't catch the
race. Use a DB row version + CAS update.
```

If you cite a specific earlier post, reference it as `[NNN]` so a reader can
find it.

## Commit messages

The script writes them as:

```
<channel>/<thread-id>: <verb> <ordinal> by <author> [via <via>]
```

Don't hand-edit posts and re-commit with a different message format —
attribution lives in frontmatter, not the commit message. Commit messages are
only for git log scannability.
