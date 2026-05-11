---
description: List all pair-pressure servers in this repo
argument-hint: (no args)
---

Run `pp servers` and present the JSON output as a readable table.

Output shape:
```json
{
  "servers": [
    {"name": "engineering", "description": "...", "channels": [...],
     "on_remote": true, "local_worktree": true, "created_by": "alice"},
    {"name": "design",      "description": "...", "channels": [...],
     "on_remote": true, "local_worktree": false},
    {"name": "lostbranch",  "orphan_branch": true, "on_remote": true,
     "local_worktree": false}
  ],
  "active": "engineering"   // or null
}
```

Present as:

```
Active server: <active or "(none — use /pp-chat:server-switch <name>)">

  NAME          DESCRIPTION                  LOCAL  REMOTE  CREATED BY
  engineering   Backend + infra team chat    yes    yes     alice
  design        Product design discussions   no     yes     bob
  lostbranch    (orphan branch — not in servers.json)  no   yes
```

Servers with `local_worktree: false` will lazily materialize on the first `pp` op that uses them — nothing to do.

Servers with `orphan_branch: true` exist on the remote but aren't in `.pair-pressure/servers.json` on `main`. Mention this; suggest someone with write access run `pp server new <name>` (no-op for the branch but re-registers it) or `pp server remove <name>` if they're stale.

If no servers exist, say "No servers yet — create one with `/pp-chat:server-new <name>`."
