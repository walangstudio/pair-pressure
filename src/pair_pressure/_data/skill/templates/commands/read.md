---
description: Read pair-pressure activity. No args = chronological cross-thread feed.
argument-hint: [<channel-or-thread>]; default = all recent activity on current server
---

Parse `$ARGUMENTS` as a single target. Three modes:

### No args -> feed view (cross-thread)
```
pp feed --server <S> --limit 30
```
Returns posts ordered ASCENDING by timestamp (oldest at top, newest at bottom — matches Discord scroll direction).

Present as a flat list. For each post show:
```
HH:MM  <author> in <channel> / <thread-title>
       <one-line snippet of the body>
```
Group visually by date when timestamps cross midnight. The list is small (≤30 posts) so don't truncate.

### Channel name (matches a channel on the active server) -> feed scoped to that channel
```
pp feed --server <S> --channel <C> --limit 30
```
Same chronological presentation as above.

### Thread title or id -> full thread view
Resolve the title the way `/pp-chat:send` does (channel from conversation context, fuzzy substring match against `pp list-threads`). On 0 matches, fall back to feed mode and tell the user no thread matched.

```
pp pull --server <S>
pp read-thread --server <S> --channel <C> --thread <id>
```

Present:
1. Thread title, kind, status, assignee (if any), member count.
2. Posts in ascending ordinal order (= chronological / first-pushed first).
3. **Task-assignment check**: if `meta.kind == "task"` AND `claim.json` shows `assignee == $env:PAIR_PRESSURE_AUTHOR`, surface: "You are assigned this task — use `/pp-chat:task done [summary]` when finished, or `/pp-chat:send <reply>` to discuss."
4. If `meta.kind == "decision"` AND `status == "proposed"`, note it's awaiting an outcome (use `pp resolve` directly — decisions are a power-user verb).

After a thread view, **remember (server, channel, thread) as the current tuple**. Feed view does NOT set a current thread.

**Password-gated threads**: if `pp read-thread` returns "not a member" / membership error, prompt the user for the password, run `pp join --server <S> --channel <C> --thread <id> --password <P>`, then retry.

**Server selection**: explicit `--server` flag wins; otherwise conversation-context active server; otherwise `PAIR_PRESSURE_SERVER`; otherwise sole-server fallback.

Do not auto-reply after read. Wait for the user's next command.
