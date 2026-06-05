---
description: Switch the active chat repo for this conversation, add one, or list them
argument-hint: [list] | use <name> | add <name> <git-url> [--with-server <s>] | remove <name> [--yes]
model: claude-haiku-4-5-20251001
allowed-tools: Bash(pp *)
---

A "repo" is a whole chat repo (its own GitHub remote), distinct from a "server"
(a branch inside one repo). Use this to run different conversations against
different chat repos without collision.

Parse the first token of `$ARGUMENTS` as the subcommand. No token = `list`.

**`list` (or no args):**
```
pp repo list
```
Report the registered repos and which is active for this session. Each row has a per-repo `servers` count.

**`use <name>`:**
```
pp repo use <name>
```
On success, **update conversation context**: `<name>` is now the active repo. **Drop any current server/thread** from context — they belonged to the previous repo. Tell the user: "Now on repo `<name>` for this conversation." If the JSON shows `"sticky": false`, the pin needs `PAIR_PRESSURE_SESSION_ID`; tell them to eval the printed `shell_export` line (POSIX) or run the `$env:` line (PowerShell) instead.

**`add <name> <git-url>`** (optional `--with-server <s>`, `--channels a,b`, `--path <dir>`, `--no-clone`):
1. Confirm with the user before cloning (this clones into `~/.pair-pressure/repos/<name>`).
2. Run:
   ```
   pp repo add <name> <git-url> [--with-server <s>] [--channels <list>]
   ```
3. On success, suggest `/pp-chat:repo use <name>` to switch to it.

**`remove <name>`** (hard-gated):
1. Ask for explicit confirmation. Only add `--yes` (and `--delete-clone` if they want the clone deleted) after the user confirms.
   ```
   pp repo remove <name> --yes [--delete-clone]
   ```

Surface failures verbatim:
- `{"error": "repo '<name>' is already registered"}` → suggest `/pp-chat:repo use <name>`.
- `{"error": "repo '<name>' is not registered ..."}` → run `pp repo list` and show the valid names.
- `{"error": "git clone failed: ..."}` → report; the user checks the URL / their git auth.
