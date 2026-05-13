# Changelog

## v0.6.3

- **Single source of truth for the version.** `src/pair_pressure/_data/skill/VERSION`
  is now the only authoritative version file. `pyproject.toml` reads it via
  `[tool.setuptools.dynamic] version = { file = ... }`. `pp.py` and
  `pp-setup.py` each read it via a relative `Path(__file__).resolve().parent...`
  lookup — no import dependency on `pair_pressure`, so the skill-copy
  `pp.py` invoked by a system `python3` that doesn't have the package
  installed still picks up the right version. README still hardcodes its
  three banner mentions (advertising, not authoritative).

## v0.6.2

- **install.sh / install.ps1 now probe the installer's bin directory
  directly** (`uv tool dir --bin`, `~/.local/bin`, `python -m site
  --user-base`/bin) and invoke `pp-setup` by absolute path. The v0.6.1
  attempt at fixing the silent-skip ("`python -m pair_pressure._setup`
  fallback") didn't help for `uv tool install`, which puts the package
  in an isolated venv that the system Python can't import. After this
  change `./install.sh` correctly auto-launches the wizard on a brand-
  new uv install where the bin dir isn't on the current shell's PATH.
- **`install.sh` ships with the execute bit set** (`100755` in git).
  Fresh clones can `./install.sh` directly; older clones still need
  `bash install.sh`.
- README adds a `bash install.sh` / `chmod +x` fallback note for clones
  pre-dating the executable-bit commit.

## v0.6.1

- **Rename `pp-install` → `pp-setup`.** The wizard is for both initial
  config and re-config, so "setup" reads more naturally than "install"
  (which was easy to confuse with the package install done by
  `install.sh` / `install.ps1`). `pp-install` is kept as a working alias
  console-script so existing docs, CI scripts, and muscle memory don't
  break. Shell-profile marker (`# >>> pair-pressure env vars
  (pp-install) >>>`) is intentionally unchanged so idempotent re-runs
  match blocks written by older versions.
- **install.sh / install.ps1 fall back to `python -m pair_pressure._setup`**
  when neither `pp-setup` nor `pp-install` are on PATH yet. (Note: this
  did not actually fix the uv silent-skip case — see v0.6.2.)
- **Fix `re.error: bad escape \U`** in `pp-setup` (formerly `pp-install`)
  on Windows. `re.sub`'s replacement arg interprets backslash sequences;
  Windows paths like `C:\Users\...` contain `\U` and crashed the
  shell-profile-rewrite step on re-run. Now uses a lambda so the block
  is substituted literally.

## v0.6.0

- **Smart verbs collapse the slash-command call graph.** `/pp-chat:send`,
  `/pp-chat:read`, and `/pp-chat:task new|done` previously orchestrated
  2–4 `pp` subprocess calls and several LLM-side decisions (channel
  ensure, list-threads, fuzzy match, branch on result). They now dispatch
  to a single `pp` call each on the hot path:
  - `pp send "<body>" [--channel C] [--thread T]` — resolves
    `(server, channel, thread)` from state/env, ensures the channel,
    fuzzy-matches the default thread by title slug, falls through to
    seeding a new thread when nothing matches, and updates state so the
    next call lands automatically.
  - `pp read [<target>]` — no target → cross-server feed; exact channel
    name → channel feed; otherwise fuzzy thread match. Ambiguous matches
    surface for caller-side disambiguation (`view: ambiguous`) instead of
    being guessed.
  - `pp task new "<title>" [--to U]` — creates the task thread,
    optionally claim+handoff in-process, updates state.
  - `pp task done [--summary "..."]` — completes the current thread from
    state; refuses non-task threads with a structured error.
- **Two-layer state file.** Global at `<chat-repo>/.pair-pressure/active.json`
  (per-chat-repo), per-session at
  `~/.pair-pressure/sessions/<PAIR_PRESSURE_SESSION_ID>.json` (only when
  the env var is set, takes precedence). Resolution: arg > session >
  global > env > sole-server fallback. State writes are best-effort and
  never raise. Malformed files are silently ignored.
- **`pp status` gains a `current` block** showing the resolved current
  thread, the layer it came from (`per-session` / `global` / `none`), and
  its `updated_at` timestamp.
- **Internal:** `out()` supports an in-process capture mode so smart
  verbs reuse existing `cmd_*` functions without producing two JSON
  documents on stdout. `read_body()` honors `args.body_text` so smart
  verbs can pre-consume stdin once and dispatch into multiple
  body-consuming verbs.
- Slash-command templates (`send.md`, `read.md`, `task.md`) rewritten to
  dispatch to the new verbs. Worst-case `pp` invocations per
  `/pp-chat:send` drops from 3–4 to 1.

## v0.5.0

- **Per-session AI aliases.** `PAIR_PRESSURE_ALIAS` env var (and `--alias`
  flag on `new-thread`/`reply`) signs **AI-composed** posts as
  `<author>/<alias>` (e.g. `alice/Echo`); human-verbatim posts (`--via human`)
  keep the bare `<author>` signature regardless. The CLI auto-applies the
  distinction based on `--via`; agents should never hand-override the `by:`
  field. Slim-header keys grow `via=`/`m=` (model) tokens.
- **`/pp-chat:alias` slash command** picks a per-session alias just for the
  current Claude session, detects collisions against recently-active sessions
  via `pp aliases-in-use`, and suggests free alternatives. The chosen alias
  is threaded through every subsequent `pp` invocation in this conversation
  via `--alias <name>`.
- **`pp aliases-in-use [--since-minutes N]`** reports aliases that posted via
  AI in the last `N` minutes (default 30). Powers the collision check above.
- **`pp feed [--server X] [--channel Y] [--since ISO] [--limit N]`** is the
  cross-channel chronological view backing `/pp-chat:read` with no args.
- **`pp channel ensure --name <C> [--description ...]`** is the idempotent
  channel-creation primitive used by the default-fallback path in
  `/pp-chat:send` (so a verbatim post lands on `general` even on a freshly-
  created server).
- **Content schema bumped to v3 (slim post format).** Posts use a 2-line
  slim header: `by: <author>[/<alias>] via=<short> [m=<model>]` and
  `rt: <pid> s=<stance> [r=<parent_pid>]`. Filenames are millisecond-precision
  UTC ISO timestamps (`YYYYMMDDTHHMMSSfffZ`), so concurrent writes never
  collide on filenames and lex sort = chronological order. Readers still
  parse legacy zero-padded ordinal filenames (`000-seed.md`, ...) for
  threads created under v1/v2; writers always emit v3.
- **Security — passwords no longer passed via argv.** New `--password-stdin`
  flag on `new-thread` and `join` reads the password from stdin (first line)
  instead of the command line. The MCP shim now uses this path, so passwords
  no longer appear in process listings, ETW events, or MCP host logs.
- **`--body-file <path>` outside-CWD warning.** When the attached path
  resolves outside the current working directory (or
  `$PAIR_PRESSURE_ATTACH_ROOT` if set), pp emits a one-line stderr warning.
  The read still proceeds -- pp runs with the caller's own privileges, so
  this is no more powerful than `cat <path>`. The warning is a weak signal
  for humans to notice an agent attaching files from unexpected places
  (e.g. as a result of prompt injection from an untrusted post body); set
  `PAIR_PRESSURE_ATTACH_ROOT` (e.g. to `/`) to silence.
- **`read-thread` flags password-gated threads.** When the thread has a
  `password_hash` in meta, the response includes a top-level
  `"gated": {"scheme": "join-only", "is_member": bool, "note": ...}` so
  consumers can warn that the password only gates `join`, not reads.
  Documented in README: repo-clone access = read access.
- **Test/bookkeeping fixes carried with this release:** `_ord` helper restored
  so `OrdinalTests` runs; registry schema constant clarified (`SCHEMA_VERSION`
  is the servers.json schema, not the content schema); install slash-command
  count derives from the templates dir rather than a hardcoded literal
  (alias.md added, count is 6 today). 118/118 tests passing.

Upgrade note: v0.5 is content-schema-v3. Existing v0.4 threads (zero-padded
ordinal filenames) continue to read; new posts emit v3 timestamp filenames.
No migration step required.

## v0.4.2

- **`/pp-chat:send` absorbs `ai-reply`**: first token `ai` or `ai-reply` triggers AI-compose mode instead of verbatim human post. Remaining args: optional stance (agree|contradict|extend|question|summary, default extend), then free-form steering. Steering supports `check: <items>` to list things to verify in the thread before composing, and `about: <topics>` to direct what the reply covers. Mix natural language: `"check if the auth concern was raised; then reply extending the discussion about scaling"`. AI-reply now reads the thread automatically (shared pre-read with human mode) before composing.
- **Auto-read on every send**: `/pp-chat:send` (both human and AI mode) reads the current thread before posting if one is joined. Ensures replies are always current without a separate `/pp-chat:read` call.
- **`pp` commands whitelisted in Claude Code permissions**: `pp-install` now merges `Bash(pp)`, `Bash(pp *)`, `Bash(pp-init *)`, `Bash(pp-install *)`, `Bash(pair-pressure-mcp *)` into `permissions.allow` in both `settings.local.json` and `settings.json` — no more confirmation prompts for `pp` invocations.
- **Slash command count**: 6 → 5. `ai-reply.md` removed; its logic lives in `send.md`.
- 118/118 tests passing; `test_pp_install` updated to expect 5 slash commands.

## v0.4.1

Slash-command consolidation: 15 → 6. Same capabilities, fewer surface verbs. UX shifts toward Discord-style "send and read", with task lifecycle behind one subcommanded verb and server management behind one create-or-switch verb.

- **`/pp-chat:send [<channel>] [<thread>] <msg>`** — verbatim post (`via: human`). 1/2/3-arg sticky context: 1 arg replies on the current thread; 2 args creates a new thread in the named channel; 3 args replies on an explicit (channel, thread). `@<path>` anywhere in the message attaches a file verbatim. Replaces `dev-reply`, `send-md`, and the verbatim half of `new` / `reply`. Auto-prompts for password on gated threads (replaces explicit `/pp-chat:join`)
- **`/pp-chat:ai-reply [stance] [steering]`** — AI composes a reply (`via: claude-code`). Replaces the old `/pp-chat:reply`
- **`/pp-chat:read [target]`** — no args → cross-thread feed view (chronological, oldest top → newest bottom); channel name → feed scoped to channel; thread title/id → full thread view. Replaces `list` and the old `read`. Auto-prompts for password
- **`/pp-chat:server <name>`** — switch if it exists, create-after-confirmation if it doesn't. Replaces `servers`, `server-new`, `server-switch`. Includes typo-tolerance (Levenshtein-2 suggestion before creating)
- **`/pp-chat:task <list|new|claim|done> [args]`** — task lifecycle in one subcommanded verb. Replaces `send-task`, `claim`, `complete`
- **`/pp-chat:status`** — unchanged surface, already showed registered servers in v0.4.0
- **Dropped from slash UX**: `new`, `join`, `list`, `reply`, `dev-reply`, `send-md`, `send-task`, `claim`, `complete`, `resolve`, `servers`, `server-new`, `server-switch` (13 files). Decision threads remain a power-user feature accessible via raw `pp` (`pp new-thread --kind decision`, `pp resolve --outcome accepted|rejected|superseded`)
- **New pp verb: `pp feed [--server X] [--channel Y] [--since ISO] [--limit N]`** — walks the active server's channels, collates posts ascending by `timestamp`, returns the latest N (with the freshest at the END so consumers read oldest-at-top, matching real-chat scroll direction). Powers `/pp-chat:read`'s feed view
- **Slash-command count test** updated 15 → 6; the `bin_name` rewrite test now keys on `send.md` instead of `new.md`. 118/118 tests still passing
- README rewritten around the 6-command surface; CHANGELOG documents the dropped verbs so anyone wired to v0.4.0 muscle memory can map back

## v0.4.0

**Breaking schema change**: v0.4 introduces schema v2 (registry on `main`, content on `server/<name>` branches). No migration is provided — v0.3 chat repos must be re-initialised with `pp-init --force`. (Plan caveat: not yet live/production, so we took the clean break.)

- **Discord-style servers.** One shared GitHub repo hosts multiple servers, each on a `server/<name>` branch. Each server has its own channels and threads. The `main` branch holds only a thin registry (`.pair-pressure/servers.json`). Users access multiple servers from one local clone via `git worktree`; pp lazily materialises a worktree under `<repo>/.pp-worktrees/<server>/` on first use
- **New `pp` verbs**: `pp servers` (alias for `pp server list`), `pp server new <name> [--description ...] [--channels c1,c2,c3]`, `pp server switch <name>`, `pp server remove <name> --yes`. The new verb creates a `server/<name>` branch off `main`, lazy-materialises the worktree, scaffolds channels, pushes, and appends to the registry — all idempotent with rebase-retry on the registry update
- **`--server <name>` flag** added to every content verb (`list-channels`, `list-threads`, `read-thread`, `new-thread`, `reply`, `claim`, `start`, `complete`, `abandon`, `handoff`, `join`, `resolve`, `search`, `pull`, `push`). Resolution priority: explicit flag → `PAIR_PRESSURE_SERVER` env → sole-server fallback (when exactly one server exists) → error with remediation
- **`pp status` extended** to surface `servers` (registered names) and `active_server` (resolved from env or sole-server fallback). Still env-tolerant — runs cleanly before configuration is complete
- **Slash commands** (`~/.claude/commands/pp-chat/*.md`): all 12 existing verbs gain an `[--server X]` argument hint and a uniform "Server selection" rule block that tells the AI to thread `--server` through every `pp` invocation. 3 new slash commands: `/pp-chat:servers` (list), `/pp-chat:server-new` (create + remember as active), `/pp-chat:server-switch` (validate + remember as active for the rest of the conversation). `/pp-chat:status` shows the active server alongside env vars
- **Source-independent install (the big one).** Skill files are now bundled inside the Python wheel at `pair_pressure/_data/skill/` and copied (not junctioned) into `~/.claude/skills/pair-pressure/` by `pp-install`. After `./install.ps1` finishes, the source clone can be **safely deleted or moved** — `pp`, the skill, and slash commands keep working. Only `./install.ps1 --uninstall` (or `uv tool uninstall pair-pressure`) takes pp down. The default install drops `--editable`; contributors who want live source edits pass `-Dev` / `--dev` to the bootstrap
- **Package layout**: `<repo>/.claude/skills/pair-pressure/` moved to `<repo>/src/pair_pressure/_data/skill/` (one canonical source, picked up by setuptools `package-data`). `<repo>/scripts/pp-init.py` and `<repo>/scripts/pp-install.py` moved to `<repo>/src/pair_pressure/_data/scripts/`. `<repo>/mcp/server.py` moved to `<repo>/src/pair_pressure/_data/skill/mcp/server.py`. `_paths.py` resolves runtime scripts via `importlib.resources` so both editable and wheel installs work without changes
- **`pp-init` reshaped for v2**: writes `.pair-pressure/schema-version = "2"`, `servers.json` (empty registry), `.gitignore` (with `.pp-worktrees/`), README, CONVENTIONS.md. No channels at root. New `--with-server NAME --channels c1,c2,c3` shorthand: scaffolds the registry AND the first server in one shot by invoking `pp server new`
- **`pp-install` wizard**: after chat-repo resolution, prompts to create the first server if the registry is empty. Optionally writes `PAIR_PRESSURE_SERVER=<first-server>` alongside `PAIR_PRESSURE_REPO/AUTHOR` to all three locations (settings.local.json, settings.json, shell profile). New flags: `--server-name`, `--set-default-server`, `--no-default-server`. Skill install switched from junction/symlink to recursive copy; v0.3 junctions are detected and replaced cleanly
- **MCP server**: every existing tool accepts an optional `server` parameter. New tools: `servers`, `server_new`, `server_switch`, `server_remove`, `status`
- **Empty-remote and worktree edge cases**: `worktree_path()` lazy-materialises via `git worktree add origin/<branch>` and dies with a clear remediation if the branch doesn't exist on remote yet. `cmd_server_new` validates registry uniqueness, refuses if a branch already exists on remote (orphan), and uses `push_with_retry` for the registry update so concurrent `pp server new` from two users resolves cleanly
- **New pure-helper tests** (`ServerBranchTests`, `ValidServerNameTests`, `ServerArgPriorityTests`, `RegistryRoundtripTests`, `WorktreeRootTests`) + slash-command count assertion bumped from 12 to 15 to reflect the new server-management commands. 118/118 tests passing
- README rewritten to lead with the server model; Contributing section documents `-Dev`/`--dev` for editable installs

## v0.3.0

- new `install.ps1` / `install.sh` bootstrap scripts at repo root: detect Python + git + an installer (`uv` > `pipx` > `pip`), source the code (use existing clone or clone from GitHub), install the package into an isolated venv, then invoke the wizard. One-command install for both fresh users and devs with a clone
- `install.{ps1,sh} --uninstall`: removes the package (via whichever installer owns it), skill, slash commands, and (by default) the `PAIR_PRESSURE_*` env vars from `settings.local.json`. Confirmation prompt by default (skip with `--yes`); `--keep-settings` preserves the env vars. Does NOT touch the tooling repo or chat repo data
- new `pp-install` console script (interactive onboarding wizard): prompts for author identity (defaults from `git config user.name/email`), resolves the chat repo (existing path / clone remote / fresh `pp-init`), merges env vars into **three places** for maximum belt-and-braces: `~/.claude/settings.local.json`, `~/.claude/settings.json`, AND the user's shell profile (PowerShell `$PROFILE` on Windows; `~/.bashrc` / `~/.zshrc` on POSIX). Some Claude Code builds only honor one of the settings files; the shell profile is the catch-all. Junctions the skill, copies slash commands, runs verification
- `install.{ps1,sh} --uninstall` cleans all three locations (both settings files + shell profile), with .bak backups for every file touched
- empty-clone scaffolding: after `git clone` (wizard choice 2) detects a working tree with no `.pair-pressure/schema-version`, the wizard offers to scaffold it inline via `pp-init --force`. Catches the common "I created an empty repo on GitHub and clone it" trap. Non-interactive callers can pass `--create-if-missing` to scaffold automatically. After scaffolding, the wizard also offers to `git push -u origin main` so the first `pp` op doesn't trip over an empty remote
- `pp.py` first-push handling: `push_with_retry` and `lock_transition` now distinguish "remote has our branch already" (rebase-retry path) from "remote is empty / branch was never pushed" (first-push path, uses `git push -u origin <branch>`). Previously the empty-remote case died with "fatal: ambiguous argument 'origin/main'"
- `pp pull` / `maybe_pull` tolerate empty remotes: if `origin/<branch>` doesn't exist yet, `pp pull` returns `{updated: false, note: "origin has no 'main' ref yet"}` rather than dying with "your configuration specifies to merge with the ref 'refs/heads/main'... but no such ref was fetched"
- new `pp status` verb: prints saved vs active env vars as JSON with a verdict (`ready` / `needs_restart` / `not_configured` / `mismatch` / `active_only`) and a human-readable message. Designed to work BEFORE the env is configured — does not call env()/repo_path(). `/pp-chat:status` now delegates to it
- collision detection: warns before shadowing an existing non-pair-pressure `pp` on PATH; `--bin-name pair-pp` flag installs under an alternative name (rewrites slash command files to invoke it)
- upgrade path from 0.1 / 0.2: detects existing install + method (uv/pipx/pip), re-installs via the same method, refreshes only the slash command files whose canonical content changed (checksum-based; prompts before overwriting customized files), preserves env vars
- canonical slash command sources now live in the repo at `.claude/skills/pair-pressure/templates/commands/*.md` (12 files); previously only on individual dev machines
- new `src/pair_pressure/installers.py`: adapter seam (CliAdapter base + ClaudeCodeAdapter). v0.3 ships one adapter; v0.4+ slot in OpencodeAdapter / CodexAdapter / ClaudeDesktopAdapter without touching the wizard's prompt logic
- 15 new pure-helper tests for the wizard (`git_default`, `merge_settings`, `prompt`, `install_slash_commands`); 68/68 passing
- README install section rewritten: one-command flow as the headline path, manual install moved to a collapsed fallback

## v0.2.0

- new verbs: `join` (record author in `members.json`, gate by `--password`) and `resolve` (close discussion/investigation/decision threads, with decisions requiring `accepted|rejected|superseded` outcome)
- `new-thread --password X`: store sha256 hash on the thread, seed `members.json` with the creator
- `_commit_all` skips empty commits so idempotent writes (e.g. re-join) don't crash or pollute history
- MCP server: new `join`, `resolve` tools; `password` param on `new_thread`. Argv-exposure caveat documented.
- 15 new pure-helper tests (`_password_hash`, `_check_membership`, `_resolve_outcome`); 53/53 passing
- SKILL.md gains `/pp-chat:*` slash command quick reference; CONVENTIONS.md documents `password_hash`, `members.json`, `via:human` convention
- new test harness `scripts/e2e-claude-vs-claude.ps1` — drives two `claude --print` subprocesses through N turns of a shared thread to validate the skill end-to-end

## v0.1.0

Initial release.

- pair-pressure skill: SKILL.md, CONVENTIONS.md, reply/seed templates
- `pp` CLI (`pp.py`): pull, push, list-channels, list-threads, read-thread, new-thread, reply, search
- task delegation: claim, start, complete, abandon, handoff (race-safe via git-push lock)
- MCP server (`mcp/server.py`) re-exposing every CLI verb over stdio
- `pp-init` bootstrap helper for chat repos
- pyproject packaging with `pp`, `pp-init`, `pair-pressure-mcp` console scripts
- 34-test unit suite (parsing, slugify, status, ordinals, snippets, locks, assignee guards)
