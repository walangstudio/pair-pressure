#!/usr/bin/env python3
"""pair-pressure: shared chat among AI agents and humans, backed by a git repo.

Single-file, stdlib-only. All output is JSON on stdout; errors go to stderr
and exit nonzero. Reads PAIR_PRESSURE_REPO and PAIR_PRESSURE_AUTHOR from env.

Day 1 verbs: pull, push, list-channels, list-threads, read-thread,
             new-thread, reply.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SERVER_BRANCH_PREFIX = "server/"
SCHEMA_VERSION = "2"  # servers.json registry schema; matches pp-init.py

_VIA_SHORT = {"claude-code": "cc", "human": "h", "mcp": "mcp"}
_VIA_LONG = {v: k for k, v in _VIA_SHORT.items()}

# Set by _activate_server() so that all downstream code (repo_path(), git()
# default cwd, file paths) automatically scopes to the active worktree.
# None means "registry / main checkout", used by server-management verbs.
_CURRENT_REPO: "Path | None" = None

# Set by _activate_repo() from an explicit --repo flag. When set it wins over
# the session-pinned repo and PAIR_PRESSURE_REPO in _main_repo_path(). None
# means "resolve the repo normally" (session > env > sole registered).
_REPO_OVERRIDE: "Path | None" = None

def _read_version() -> str:
    # Single source of truth: <skill>/VERSION (sibling of scripts/). Works
    # for both the in-tree path (src/pair_pressure/_data/skill/VERSION) and
    # the copied-skill path (~/.claude/skills/pair-pressure/VERSION) since
    # the file rides along with the skill tree.
    try:
        return (Path(__file__).resolve().parent.parent / "VERSION").read_text(
            encoding="utf-8"
        ).strip()
    except OSError:
        return "0.0.0+unknown"


__version__ = _read_version()


def die(msg, code=2):
    print(json.dumps({"error": msg}), file=sys.stderr)
    sys.exit(code)


def env(name):
    val = os.environ.get(name)
    if not val:
        die(f"{name} is not set. Add it to ~/.claude/settings.local.json under env.")
    return val


def _validated_repo(p):
    """Confirm `p` is a git repo or die with a clear message."""
    if not (p / ".git").exists():
        die(f"chat repo {p} is not a git repository.")
    return p


def _main_repo_path():
    """The active chat repo's main checkout (where the registry on `main`
    lives). Resolution priority:

      1. explicit --repo override (`_REPO_OVERRIDE`, set by `_activate_repo`)
      2. the repo pinned to THIS session (per-session state `repo`)
      3. PAIR_PRESSURE_REPO env var (back-compat: unchanged when no registry)
      4. the sole registered repo, when exactly one exists
      5. die with remediation

    Back-compat: with `PAIR_PRESSURE_REPO` set and an empty/absent repo
    registry, step 3 fires and behavior is byte-identical to pre-0.9.
    """
    if _REPO_OVERRIDE is not None:
        return _REPO_OVERRIDE
    # Only consult the per-session pin when a registry exists -- a pin can only
    # name a registered repo (set via `pp repo use`), so with no registry the
    # session read is pure waste. This keeps the env-var-only path (the
    # back-compat common case) to a single cheap repos.json stat.
    repos = _repos_list()
    if repos:
        name = _session_repo_name()
        if name:
            entry = next((r for r in repos if r.get("name") == name), None)
            if entry and entry.get("path"):
                return _validated_repo(Path(entry["path"]).expanduser())
    ev = os.environ.get("PAIR_PRESSURE_REPO")
    if ev:
        return _validated_repo(Path(ev).expanduser())
    if len(repos) == 1 and repos[0].get("path"):
        return _validated_repo(Path(repos[0]["path"]).expanduser())
    die("PAIR_PRESSURE_REPO is not set and no chat repo is registered. "
        "Set the env var, or run `pp repo add <name> <url>` then "
        "`pp repo use <name>`.")


def repo_path():
    """The path the current verb operates against.

    Server-scoped verbs call `_activate_server(args)` which sets the active
    worktree. Server-management verbs (servers, server new/switch) leave it
    unset and operate on the main checkout.
    """
    if _CURRENT_REPO is not None:
        return _CURRENT_REPO
    return _main_repo_path()


def _server_branch(name):
    return SERVER_BRANCH_PREFIX + name


def _worktree_root():
    """Where server worktrees live. Always under the main checkout."""
    return _main_repo_path() / ".pp-worktrees"


def _registry_path():
    return _main_repo_path() / ".pair-pressure" / "servers.json"


def _registry_load():
    """Read .pair-pressure/servers.json off the main checkout.

    Returns an empty registry if the file is missing — server-management
    verbs handle "no servers yet" gracefully rather than dying.
    """
    p = _registry_path()
    if not p.exists():
        return {"schema_version": int(SCHEMA_VERSION), "servers": []}
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as e:
        die(f"servers.json is unreadable: {e}")


def _registry_save(data):
    p = _registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# ---- smart-verb state (active.json + per-session sidecar) ----

STATE_SCHEMA_VERSION = 2  # v2 adds the per-session `repo` field

# Machine-global, NON-git-tracked home for local config + watcher state.
# (active.json deliberately lives inside the chat repo for cross-session
# sharing; offline toggle / watcher state must NOT — they are per-machine.)
_PP_HOME = Path.home() / ".pair-pressure"


def _session_id():
    sid = os.environ.get("PAIR_PRESSURE_SESSION_ID")
    return sid.strip() if sid and sid.strip() else None


def _state_path_global():
    """Per-chat-repo active state, alongside the registry."""
    return _main_repo_path() / ".pair-pressure" / "active.json"


def _state_path_session():
    sid = _session_id()
    if not sid:
        return None
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", sid)[:64] or "anon"
    return Path.home() / ".pair-pressure" / "sessions" / f"{safe}.json"


def _state_load_one(path):
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


# ---- machine-global repo registry (multiple chat repos) ----
#
# Sits ABOVE the per-repo servers.json: a "repo" is a clone with its own
# remote; each repo contains many "servers" (branches). Lives under _PP_HOME
# (NOT inside any chat repo, since it indexes repos). Tolerant load mirrors
# _config_load -- a missing/malformed file yields an empty registry, never a
# die(), so back-compat installs (env-var only) keep working untouched.

def _repos_registry_path():
    return _PP_HOME / "repos.json"


def _repos_load():
    p = _repos_registry_path()
    if not p.exists():
        return {"schema_version": 1, "repos": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 1, "repos": []}
    if not isinstance(data, dict) or not isinstance(data.get("repos"), list):
        return {"schema_version": 1, "repos": []}
    return data


def _repos_save(data):
    """Best-effort write of repos.json. Never raises (same contract as
    _config_save)."""
    try:
        p = _repos_registry_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def _repos_list():
    return _repos_load().get("repos", [])


def _repo_entry(name):
    for r in _repos_list():
        if r.get("name") == name:
            return r
    return None


def _session_repo_name():
    """The repo name pinned to THIS session, if any.

    Reads ONLY the per-session state file under _PP_HOME -- never the global
    state file, whose path goes through _main_repo_path() and would recurse.
    """
    path = _state_path_session()
    data = _state_load_one(path) if path else None
    if data and data.get("repo"):
        return data["repo"]
    return None


def _state_load():
    """Return (per_session, global). Either may be None."""
    sess = _state_load_one(_state_path_session()) if _session_id() else None
    glob = _state_load_one(_state_path_global())
    return sess, glob


def _state_save(server=None, channel=None, thread_id=None, source=None,
                repo=None, session_only=False, clear_repo=False):
    """Best-effort write to per-session (and, unless session_only, global)
    state files. Never raises -- smart verbs must not die on state-write
    failure.

    `repo` pins the session to a registered chat repo. `session_only=True`
    writes ONLY the per-session file -- used by `pp repo use` so switching a
    repo for one conversation never stomps the repo's shared global state.
    `clear_repo=True` unpins the repo (used by `pp repo remove`); without it,
    a None `repo` is treated as "leave the existing pin alone".
    """
    payload = {
        "schema_version": STATE_SCHEMA_VERSION,
        "server": server,
        "channel": channel,
        "thread_id": thread_id,
        "repo": repo,
        "updated_at": now_iso(),
        "source": source or "unknown",
    }
    paths = (
        (_state_path_session(),) if session_only
        else (_state_path_session(), _state_path_global())
    )
    for path in paths:
        if path is None:
            continue
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            merged = dict(payload)
            existing = _state_load_one(path) or {}
            if "task_index" in existing:  # survive routine state writes
                merged["task_index"] = existing["task_index"]
            # Preserve a previously-pinned repo when a routine write omits it,
            # so a `send`/`read` doesn't blank the session's active repo.
            # `clear_repo` opts out (pp repo remove unpins).
            if (not clear_repo and merged.get("repo") is None
                    and existing.get("repo")):
                merged["repo"] = existing["repo"]
            path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
        except OSError:
            continue


# ---- machine-global config (offline toggle, watcher prefs) ----

def _config_path():
    return _PP_HOME / "config.json"


def _config_load():
    """Tolerant read of ~/.pair-pressure/config.json. Never raises; a missing
    or malformed file just yields {}. Mirrors _state_load_one's contract so
    pre-configuration callers (status, offline) stay safe."""
    p = _config_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _config_save(patch):
    """Best-effort merge-write of config.json. Never raises -- callers must
    not die on a config-write failure (same contract as _state_save)."""
    data = _config_load()
    data.update(patch)
    data.setdefault("schema_version", 1)
    try:
        p = _config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass
    return data


def _offline():
    """True when offline mode is on. PAIR_PRESSURE_OFFLINE env (1/true/yes/on)
    overrides the saved config; default is online."""
    ev = os.environ.get("PAIR_PRESSURE_OFFLINE")
    if ev is not None and ev.strip() != "":
        return ev.strip().lower() in ("1", "true", "yes", "on")
    return bool(_config_load().get("offline", False))


def _default_channel():
    return os.environ.get("PAIR_PRESSURE_DEFAULT_CHANNEL") or "general"


def _default_thread_title():
    return os.environ.get("PAIR_PRESSURE_DEFAULT_THREAD_TITLE") or "general-chat"


def resolve_active(args):
    """Fill in args.server/channel/thread from state + env defaults.

    Priority per field: explicit arg > per-session state > global state >
    env var > sole-server fallback (server only) > default (channel/title)
    > None (thread).

    Mutates `args` in place so downstream code that reads args.server etc.
    keeps working. Returns a dict with the resolved values and `sources`
    diagnostics."""
    sess, glob = _state_load()
    sources = {}

    # Repo is already pinned by _activate_repo (--repo) / _main_repo_path
    # (session > env > sole) before we get here; this block is diagnostic.
    repo_name = getattr(args, "repo", None)
    if repo_name:
        sources["repo"] = "arg"
    elif _session_repo_name():
        repo_name, sources["repo"] = _session_repo_name(), "session"
    elif os.environ.get("PAIR_PRESSURE_REPO"):
        sources["repo"] = "env"
    else:
        repos = _repos_list()
        if len(repos) == 1:
            repo_name, sources["repo"] = repos[0].get("name"), "sole-repo"

    server = getattr(args, "server", None)
    if server:
        sources["server"] = "arg"
    elif sess and sess.get("server"):
        server, sources["server"] = sess["server"], "session"
    elif glob and glob.get("server"):
        server, sources["server"] = glob["server"], "global"
    elif os.environ.get("PAIR_PRESSURE_SERVER"):
        server, sources["server"] = os.environ["PAIR_PRESSURE_SERVER"], "env"
    else:
        regs = _registry_load().get("servers", [])
        if len(regs) == 1:
            server, sources["server"] = regs[0]["name"], "sole-server"
    if not server:
        die("no server specified; pass --server <name> or set "
            "PAIR_PRESSURE_SERVER (try `pp servers`)")
    args.server = server

    channel = getattr(args, "channel", None)
    if channel:
        sources["channel"] = "arg"
    elif sess and sess.get("channel"):
        channel, sources["channel"] = sess["channel"], "session"
    elif glob and glob.get("channel"):
        channel, sources["channel"] = glob["channel"], "global"
    else:
        channel, sources["channel"] = _default_channel(), "default"
    args.channel = channel

    thread = getattr(args, "thread", None)
    if thread:
        sources["thread"] = "arg"
    elif sess and sess.get("thread_id"):
        thread, sources["thread"] = sess["thread_id"], "session"
    elif glob and glob.get("thread_id"):
        thread, sources["thread"] = glob["thread_id"], "global"
    else:
        sources["thread"] = "none"
    args.thread = thread

    return {
        "repo": repo_name,
        "server": server,
        "channel": channel,
        "thread": thread,
        "title": _default_thread_title(),
        "sources": sources,
    }


def worktree_path(server):
    """Resolve (and lazy-create) the worktree dir for a server.

    Idempotent: re-running on an existing worktree returns its path. If the
    worktree dir is missing but the remote branch exists, it's materialized
    via `git worktree add` from origin/<branch>.
    """
    main = _main_repo_path()
    wt = main / ".pp-worktrees" / server
    if wt.exists() and (wt / ".git").exists():
        return wt
    branch = _server_branch(server)
    if _offline():
        # Offline does no network: skip the fetch only. Materialize from
        # whatever is ALREADY in the local object store -- the local
        # branch, else the cached origin/<branch> ref left by the clone /
        # prior fetch during setup. _origin_branch_exists is a local
        # rev-parse, NOT a network call. This makes a repo cloned from a
        # remote at setup fully usable offline, and the reverse (toggling
        # offline off) just restores the fetch path below unchanged.
        wt.parent.mkdir(parents=True, exist_ok=True)
        if _local_branch_exists(branch, cwd=main):
            git("worktree", "add", str(wt), branch, cwd=main)
            return wt
        if _origin_branch_exists(branch, cwd=main):
            git("worktree", "add", str(wt), f"origin/{branch}", cwd=main)
            git("checkout", "-B", branch, f"origin/{branch}", cwd=wt)
            return wt
        die(f"server '{server}' has no local branch {branch} and no cached "
            f"origin/{branch}; offline mode is on. Run `pp offline false` "
            f"to fetch it, or `pp server new {server}` while online.")
    git("fetch", "origin", branch, cwd=main, check=False)
    wt.parent.mkdir(parents=True, exist_ok=True)
    if _origin_branch_exists(branch, cwd=main):
        git("worktree", "add", str(wt), f"origin/{branch}", cwd=main)
        # Detach from origin/<branch> onto a local tracking branch so writes work.
        git("checkout", "-B", branch, f"origin/{branch}", cwd=wt)
    else:
        die(
            f"server '{server}' does not exist on remote (no branch {branch}). "
            f"Use `pp server new {server}` to create it."
        )
    return wt


def _server_arg(args):
    """Resolve the active server name in priority order.

    1. explicit args.server (--server flag)
    2. PAIR_PRESSURE_SERVER env var
    3. sole server in registry (when exactly one exists)
    4. die() with remediation
    """
    if getattr(args, "server", None):
        return args.server
    env_s = os.environ.get("PAIR_PRESSURE_SERVER")
    if env_s:
        return env_s
    servers = _registry_load().get("servers", [])
    if len(servers) == 1:
        return servers[0]["name"]
    die(
        "no server specified; pass --server <name> or set "
        "PAIR_PRESSURE_SERVER (try `pp servers` to list)"
    )


def _activate_server(args):
    """Resolve the target server and pin _CURRENT_REPO to its worktree.

    Called at the top of every content verb so the rest of the code (which
    calls repo_path() and lets git() default cwd) operates on the right
    worktree without per-callsite changes.
    """
    global _CURRENT_REPO
    name = _server_arg(args)
    _CURRENT_REPO = worktree_path(name)
    return name


def _add_server_arg(sp):
    """Attach the standard --server flag to a subparser."""
    sp.add_argument(
        "--server", default=None,
        help="server name (see `pp servers`); overrides "
             "PAIR_PRESSURE_SERVER. If exactly one server exists in the "
             "registry, it is used by default.",
    )


def _add_repo_arg(sp):
    """Attach the standard --repo flag to a subparser."""
    sp.add_argument(
        "--repo", default=None,
        help="chat repo name (see `pp repo list`) or a path to a clone; "
             "overrides the session-pinned repo and PAIR_PRESSURE_REPO for "
             "this call.",
    )


def _activate_repo(args):
    """Pin `_REPO_OVERRIDE` from an explicit --repo flag.

    Accepts a registered repo name or a path to an existing clone. Called
    once in main() right after arg parsing, before any verb runs, so that
    _main_repo_path() (and everything anchored to it) targets the right repo.
    No-op when --repo is absent.
    """
    global _REPO_OVERRIDE
    name = getattr(args, "repo", None)
    if not name:
        return
    entry = _repo_entry(name)
    if entry and entry.get("path"):
        _REPO_OVERRIDE = _validated_repo(Path(entry["path"]).expanduser())
        return
    p = Path(name).expanduser()
    if (p / ".git").exists():
        _REPO_OVERRIDE = p
        return
    die(f"repo '{name}' is not registered (try `pp repo list`) and is not a "
        "path to a git repository.")


def author():
    return env("PAIR_PRESSURE_AUTHOR")


def alias():
    a = os.environ.get("PAIR_PRESSURE_ALIAS")
    return a.strip() if a and a.strip() else None


def effective_alias(args=None):
    """Resolve the alias for THIS call.

    Priority: explicit --alias flag (per-session/per-call override) > env var.
    Two Claude sessions on the same machine can each pass --alias to
    distinguish themselves even though they share PAIR_PRESSURE_ALIAS.
    """
    flag = getattr(args, "alias", None) if args is not None else None
    if flag and flag.strip():
        return flag.strip()
    return alias()


def by_token():
    """Default `<author>` or `<author>/<alias>` token. Use `by_for_via` when
    you have the `via` context — that's the rule that hides alias on human posts."""
    a = alias()
    return f"{author()}/{a}" if a else author()


def by_for_via(via, args=None):
    """The `by:` value to write into a post, honoring via and the per-call alias.

    Rule: human-typed posts (via=human) carry only the author identity, never
    the AI alias. AI-composed posts (via=claude-code / mcp) carry
    `<author>/<alias>` when an alias is configured. The alias resolves via
    `effective_alias(args)`, so `--alias <name>` on the command line beats
    `PAIR_PRESSURE_ALIAS` from env — that's the per-session override.
    """
    if via == "human":
        return author()
    a = effective_alias(args)
    return f"{author()}/{a}" if a else author()


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def post_id():
    n = datetime.now(timezone.utc)
    return n.strftime("%Y%m%dT%H%M%S") + f"{n.microsecond // 1000:03d}Z"


def _id_to_iso(pid):
    """`YYYYMMDDTHHMMSSfffZ` (19 chars) -> `YYYY-MM-DDTHH:MM:SS.fffZ`. None if the
    input doesn't match the v3 timestamp shape (e.g. legacy `001` ordinals)."""
    if isinstance(pid, str) and len(pid) == 19 and pid.endswith("Z") and pid[8:9] == "T":
        return f"{pid[0:4]}-{pid[4:6]}-{pid[6:8]}T{pid[9:11]}:{pid[11:13]}:{pid[13:15]}.{pid[15:18]}Z"
    return None


def _short_model(m):
    if not m:
        return None
    s = m
    if s.startswith("claude-"):
        s = s[len("claude-"):]
    return s.replace("-", "")


def _short_via(v):
    if not v:
        return "cc"
    return _VIA_SHORT.get(v, v)


def _long_via(v):
    if not v:
        return "claude-code"
    return _VIA_LONG.get(v, v)


def slugify(s):
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")[:48] or "untitled"


def git(*args, cwd=None, check=True):
    cwd = cwd or repo_path()
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=check
    )


def has_remote():
    # Offline mode is the single lever: with no "remote", maybe_pull() and
    # push_with_retry() degrade to local-only by construction, while
    # _commit_all() (remote-independent) keeps committing.
    if _offline():
        return False
    res = git("remote", check=False)
    return bool(res.stdout.strip())


# Tiny frontmatter parser/serializer. Subset of YAML: flat key:value, scalar
# values only. Sufficient for our schema (ids, authors, timestamps, stances).
_FM_RE = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)


def parse_fm(text):
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    fm = {}
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        k, _, v = line.partition(":")
        v = v.strip()
        if v.startswith('"') and v.endswith('"'):
            v = v[1:-1].replace('\\"', '"')
        elif v.lower() in ("null", "~", ""):
            v = None
        fm[k.strip()] = v
    return fm, m.group(2)


def dump_fm(meta, body):
    lines = ["---"]
    for k, v in meta.items():
        if v is None:
            lines.append(f"{k}: null")
        elif isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, (int, float)):
            lines.append(f"{k}: {v}")
        else:
            s = str(v)
            needs_quote = (
                not s
                or s.strip() != s
                or any(c in s for c in ':#"\n')
                or s.lower() in ("null", "true", "false", "~")
            )
            if needs_quote:
                s = '"' + s.replace('"', '\\"') + '"'
            lines.append(f"{k}: {s}")
    lines.append("---")
    return "\n".join(lines) + "\n" + body


def parse_slim(text):
    """Parse the v3 slim post header. Returns (fm_dict, body) or (None, None).

    Layout:
        ---
        by: alice/Echo via=cc m=opus47
        rt: 20260512T143022123Z s=extend r=20260512T142811007Z
        ---
        <body>
    """
    m = _FM_RE.match(text)
    if not m:
        return None, None
    lines = m.group(1).splitlines()
    if len(lines) < 2:
        return None, None
    by_line, rt_line = lines[0].strip(), lines[1].strip()
    if not by_line.startswith("by:") or not rt_line.startswith("rt:"):
        return None, None

    by_parts = by_line[3:].strip().split()
    if not by_parts:
        return None, None
    by_value = by_parts[0]
    if "/" in by_value:
        author_, _, alias_ = by_value.partition("/")
    else:
        author_, alias_ = by_value, None
    via, model = "claude-code", None
    for kv in by_parts[1:]:
        if "=" not in kv:
            continue
        k, v = kv.split("=", 1)
        if k == "via":
            via = _long_via(v)
        elif k == "m":
            model = v

    rt_parts = rt_line[3:].strip().split()
    if not rt_parts:
        return None, None
    pid = rt_parts[0]
    stance, irt = "extend", None
    for kv in rt_parts[1:]:
        if "=" not in kv:
            continue
        k, v = kv.split("=", 1)
        if k == "s":
            stance = v
        elif k == "r":
            irt = v

    return {
        "id": pid,
        "in_reply_to": irt,
        "author": author_,
        "alias": alias_,
        "via": via,
        "model": model,
        "stance": stance,
        "timestamp": _id_to_iso(pid) or pid,
    }, m.group(2)


def parse_post(text):
    """Parse either v3 slim format or v1/v2 legacy YAML. Always returns a dict
    with keys: id, in_reply_to, author, alias, via, model, stance, timestamp."""
    fm, body = parse_slim(text)
    if fm is not None:
        return fm, body
    fm, body = parse_fm(text)
    fm.setdefault("alias", None)
    return fm, body


# ---- prompt-injection mitigation for post bodies (read-time) ----
#
# Post bodies are written by other humans / agents and may contain text that
# the model would otherwise interpret as instructions: Claude Code control
# tags, ChatML markers, etc. The read verbs wrap every body so the model
# sees external content as data, and defang known tag names so they survive
# as readable text but lose their special meaning. Tag names are stored
# bare and the angle brackets are added at runtime so this source file
# itself contains no literal control tags.
_DEFANG_TAG_NAMES = (
    "system-reminder", "system", "system-prompt",
    "command-name", "command-message", "command-args",
    "command-stdout", "command-stderr",
    "local-command-caveat",
    "bash-input", "bash-stdout", "bash-stderr",
    "task-notification", "user-prompt-submit-hook",
    "untrusted-content",  # block nesting attempts
)
_LT = chr(0x3C)
_GT = chr(0x3E)
_FW_LT = "＜"  # fullwidth `<` lookalike
_FW_GT = "＞"  # fullwidth `>` lookalike


def _defang(body):
    """Replace open/close instances of known control tags inside an untrusted
    body with fullwidth-bracket lookalikes. Content stays readable; tag
    recognition is broken. Cheap: skips early when the body has no `<`."""
    if not isinstance(body, str) or _LT not in body:
        return body
    for name in _DEFANG_TAG_NAMES:
        for raw in (f"{_LT}{name}{_GT}", f"{_LT}/{name}{_GT}"):
            if raw in body:
                fw = raw.replace(_LT, _FW_LT).replace(_GT, _FW_GT)
                body = body.replace(raw, fw)
    return body


def _wrap_untrusted(body, author):
    """Wrap a defanged post body in a marker the model can recognize as
    external data. `templates/commands/read.md` instructs the model on the
    contract: content inside this marker is to be summarized/quoted, never
    executed as instructions."""
    a = (author or "unknown").replace("'", "")
    open_tag = f"{_LT}untrusted-content from='{a}'{_GT}"
    close_tag = f"{_LT}/untrusted-content{_GT}"
    return f"{open_tag}\n{_defang(body or '')}\n{close_tag}"


def _unwrap_untrusted(body):
    """Strip the `<untrusted-content ...>` frame added by `_wrap_untrusted`,
    keeping the (still-defanged) inner text. Used by the --pretty renderer:
    the colored author header is the visual external-data boundary, so the
    textual wrapper is redundant noise on screen."""
    if not isinstance(body, str):
        return body
    lines = body.split("\n")
    if lines and lines[0].startswith(f"{_LT}untrusted-content"):
        lines = lines[1:]
    if lines and lines[-1].strip() == f"{_LT}/untrusted-content{_GT}":
        lines = lines[:-1]
    return "\n".join(lines).strip()


# Distinct 256-color foreground codes for per-author coloring. Red (1/9) is
# deliberately omitted -- it reads as error/trust-banner. Deterministic: the
# same author always maps to the same slot.
_AUTHOR_PALETTE = (39, 208, 40, 170, 214, 51, 205, 118, 147, 220, 81, 213)
_C_RST = "\033[0m"
_C_DIM = "\033[2m"
_C_BOLD = "\033[1m"


def _author_color(name):
    """Stable per-author 256-color SGR prefix. Dependency-free hash so the
    mapping is identical across processes/machines (Python's built-in hash()
    is salted per-run and would not be stable)."""
    key = (name or "unknown").encode("utf-8", "replace")
    idx = sum(key) % len(_AUTHOR_PALETTE)
    return f"\033[38;5;{_AUTHOR_PALETTE[idx]}m"


def _sanitize_terminal(s):
    """Neutralize terminal control characters in UNTRUSTED text before it is
    printed in --pretty mode. Post bodies/titles/aliases come from other
    chatters and are hostile-capable; printed raw they enable escape-sequence
    injection (ESC \\033 → window-title spoof, screen clear, line forgery,
    terminal-specific clipboard/DECRQSS payloads). Drop C0 controls (incl.
    ESC, the vector), DEL, and C1 controls (0x80-0x9f); keep tab + every
    printable/Unicode char (em-dashes etc. survive). pp's own color codes are
    applied AFTER this, so legitimate styling renders while the content's
    escapes are inert. The JSON path is already safe via json.dumps."""
    if not isinstance(s, str):
        return s
    return "".join(
        c for c in s
        if c == "\t" or (0x20 <= ord(c) <= 0x7e) or ord(c) >= 0xa0
    )


def _render_chat(payload):
    """Print a read payload as ANSI-colored human chat (NOT JSON). Author
    name + timestamp in the author's bold color; message body dim/neutral so
    the colored nick stands out -- the standard IRC/Discord look. Forced
    color: this is only ever called for `--pretty`, whose output renders in
    the terminal/command panel."""
    # Post bodies carry non-ASCII (em-dashes, etc.); the default Windows
    # cp1252 stdout would raise UnicodeEncodeError. Force UTF-8 with graceful
    # degradation. JSON output is unaffected (it's ensure_ascii).
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    view = payload.get("view") if isinstance(payload, dict) else None

    san = _sanitize_terminal

    if view == "ambiguous":
        print(f"{_C_BOLD}Multiple threads match - pick one:{_C_RST}")
        for m in payload.get("matches", []):
            print(f"  #{san(str(m.get('channel')))}  {san(str(m.get('thread_id')))}")
        return

    if view == "message":
        if payload.get("matched") is False:
            print(f"{_C_DIM}(no post matched "
                  f"'{san(str(payload.get('query')))}'){_C_RST}")
            return
        _render_posts([payload.get("post") or {}], show_channel=True)
        return

    if view == "ambiguous_message":
        print(f"{_C_BOLD}Multiple posts match "
              f"'{san(str(payload.get('query')))}' - use a longer id:{_C_RST}")
        for m in payload.get("matches", []):
            print(f"  {san(str(m.get('id')))}  {_C_DIM}#"
                  f"{san(str(m.get('channel')))} / "
                  f"{san(str(m.get('thread_title')))}{_C_RST}")
        return

    if view == "thread":
        meta = payload.get("meta", {}) or {}
        ch = san(str(payload.get("channel", "?")))
        title = san(str(meta.get("title") or payload.get("thread_id", "?")))
        head = f"#{ch} > {title}"
        bits = [san(str(meta.get("kind", "discussion")))]
        if meta.get("status"):
            bits.append(san(str(meta["status"])))
        if meta.get("assignee"):
            bits.append(f"@{san(str(meta['assignee']))}")
        print(f"{_C_BOLD}{head}{_C_RST}  {_C_DIM}({', '.join(bits)}){_C_RST}")
        print(f"{_C_DIM}{'-' * min(len(head) + 8, 60)}{_C_RST}")
        _render_posts(payload.get("posts", []), show_channel=False)
        return

    # feed / channel (bare list under "posts") or no-match feed
    active = payload.get("active_channel")
    if view == "channel":
        print(f"{_C_BOLD}#{san(str(payload.get('channel')))}{_C_RST}")
        # Reading a channel does NOT change where you send -- say so when the
        # active channel differs from the one being viewed.
        if active and active != payload.get("channel"):
            print(f"{_C_DIM}(active: #{san(str(active))} - your next send "
                  f"lands here){_C_RST}")
    else:
        if payload.get("matched") is False:
            print(f"{_C_DIM}(nothing matched "
                  f"'{san(str(payload.get('query')))}' - showing recent "
                  f"feed){_C_RST}")
        if active:
            print(f"{_C_DIM}(active: #{san(str(active))} - your next send "
                  f"lands here){_C_RST}")
    _render_posts(payload.get("posts", []), show_channel=(view != "channel"))


def _render_posts(posts, show_channel):
    san = _sanitize_terminal
    for p in posts:
        author = san(p.get("author") or "unknown")
        alias = san(p.get("alias") or "")
        who = f"{author}/{alias}" if alias else author
        # Color by displayed identity: distinct AI aliases under one git
        # author are distinct chatters and must read as distinct colors.
        col = _author_color(who)
        ts = san((p.get("timestamp") or "")[11:16])  # HH:MM from ISO
        loc = ""
        if show_channel:
            loc = (f" {_C_DIM}#{san(p.get('channel') or '')} / "
                   f"{san(p.get('thread_title') or '')}{_C_RST}")
        stance = san(p.get("stance") or "")
        stance_s = f" {_C_DIM}[{stance}]{_C_RST}" if stance else ""
        sid = san(p.get("id") or "")
        # Short id handle (last 6 chars) so a truncated post can be fetched in
        # full via `pp read --message <id>`. Matches read.md's convention.
        ids = f" {_C_DIM}·{sid[-6:]}{_C_RST}" if sid else ""
        print(f"{_C_DIM}{ts}{_C_RST}  {col}{_C_BOLD}{who}{_C_RST}"
              f"{stance_s}{loc}{ids}")
        body = _unwrap_untrusted(p.get("body") or "")
        for line in body.split("\n"):
            print(f"   {col}{san(line)}{_C_RST}")
        print()


def dump_slim(by, via, model, pid, stance, in_reply_to, body):
    by_line = f"by: {by} via={_short_via(via)}"
    sm = _short_model(model)
    if sm and via != "human":
        by_line += f" m={sm}"
    rt_line = f"rt: {pid} s={stance}"
    if in_reply_to:
        rt_line += f" r={in_reply_to}"
    if not body.endswith("\n"):
        body = body + "\n"
    return f"---\n{by_line}\n{rt_line}\n---\n\n{body}"


_OUT_CAPTURE = None


def out(obj):
    """Emit a JSON payload to stdout, or capture it for in-process callers.

    When `_OUT_CAPTURE` is a list, payloads are appended instead of printed --
    this lets smart verbs (pp send, pp task new, ...) reuse existing cmd_*
    functions without producing two JSON documents on stdout."""
    if _OUT_CAPTURE is not None:
        _OUT_CAPTURE.append(obj)
        return
    print(json.dumps(obj, indent=2, sort_keys=True))


def _capture(func, args):
    """Run a cmd_* and return the last payload it emitted via out()."""
    global _OUT_CAPTURE
    saved = _OUT_CAPTURE
    _OUT_CAPTURE = []
    try:
        func(args)
        return _OUT_CAPTURE[-1] if _OUT_CAPTURE else None
    finally:
        _OUT_CAPTURE = saved


def read_json(path, default=None):
    if not path.exists():
        return default if default is not None else {}
    return json.loads(path.read_text())


def write_json(path, obj):
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")


# ---- verbs ----

def _maybe_activate_server(args):
    """Activate a worktree only when --server is explicitly resolvable.

    Used by pull/push/status -- they default to the main checkout if no
    server was specified. The sole-server fallback in `_activate_server`
    is deliberately skipped here: silently pulling the only server's
    worktree on a bare `pp pull` would be surprising.
    """
    global _CURRENT_REPO
    name = getattr(args, "server", None) or os.environ.get("PAIR_PRESSURE_SERVER")
    if name:
        _CURRENT_REPO = worktree_path(name)
    return name


def cmd_pull(args):
    _maybe_activate_server(args)
    if _offline():
        out({"updated": False, "offline": True,
             "head": git("rev-parse", "HEAD", check=False).stdout.strip(),
             "note": "offline mode on; skipped fetch/pull"})
        return
    if not has_remote():
        out({"updated": False, "head": git("rev-parse", "HEAD", check=False).stdout.strip(), "note": "no remote configured"})
        return
    head_before = git("rev-parse", "HEAD", check=False).stdout.strip()
    branch = _current_branch()
    # Check ONCE whether origin already has our branch. If not, try a
    # fetch (which is a no-op if the ref still doesn't exist), then
    # re-check. If still missing, the remote is empty / our branch was
    # never pushed -- nothing to pull, treat as success.
    if not _origin_branch_exists(branch):
        git("fetch", "origin", branch, check=False)
        if not _origin_branch_exists(branch):
            out({
                "updated": False,
                "head": head_before,
                "note": f"origin has no {branch!r} ref yet (push it once with "
                        f"`git push -u origin {branch}`)",
            })
            return
    res = git("pull", "--rebase", "--autostash", check=False)
    if res.returncode != 0:
        die(f"git pull failed: {res.stderr.strip() or res.stdout.strip()}")
    head_after = git("rev-parse", "HEAD").stdout.strip()
    out({"updated": head_before != head_after, "head": head_after})


def cmd_push(args):
    _maybe_activate_server(args)
    if _offline():
        out({"pushed": False, "offline": True,
             "note": "offline mode on; commit(s) kept locally, will sync "
                     "when online"})
        return
    if not has_remote():
        out({"pushed": False, "note": "no remote configured"})
        return
    res = git("push", check=False)
    if res.returncode != 0:
        die(f"git push failed: {res.stderr.strip()}")
    out({"pushed": True})


def maybe_pull():
    """Auto-pull before reads. Tolerant of every realistic failure mode:
    no remote, empty remote (no origin/<branch>), transient network error.
    Never raises -- the worst case is reading slightly stale local state.
    """
    if not has_remote():
        return
    branch = _current_branch()
    if not _origin_branch_exists(branch):
        # Try fetching; the branch may have been pushed by someone else
        # while we worked. After the fetch, if origin/<branch> still
        # doesn't exist, the remote is empty -- nothing to pull.
        git("fetch", "origin", branch, check=False)
        if not _origin_branch_exists(branch):
            return
    git("pull", "--rebase", "--autostash", check=False)


def cmd_list_channels(args):
    _activate_server(args)
    if not args.no_pull:
        maybe_pull()
    root = repo_path() / "channels"
    channels = []
    show_all = getattr(args, "all", False)
    if root.exists():
        for ch in sorted(p for p in root.iterdir() if p.is_dir()):
            meta = read_json(ch / "channel.json", {"name": ch.name, "description": ""})
            archived = bool(meta.get("archived"))
            if archived and not show_all:
                continue
            threads = [t for t in ch.iterdir() if t.is_dir()]
            last_ts = max((t.stat().st_mtime for t in threads), default=ch.stat().st_mtime)
            channels.append({
                "name": meta.get("name", ch.name),
                "description": meta.get("description", ""),
                "thread_count": len(threads),
                "last_activity": datetime.fromtimestamp(last_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "archived": archived,
            })
    out(channels)


def _safe_subpath(parent: Path, name: str) -> Path:
    """Resolve parent/name, confining the result strictly under parent.

    Rejects `..`, absolute paths, and any name that resolves outside parent.
    Use whenever a user/LLM-supplied identifier becomes a filesystem segment.
    """
    parent_r = parent.resolve()
    target = (parent_r / name).resolve()
    if parent_r not in target.parents:
        die(f"invalid name: {name!r}")
    return target


def channel_dir(name):
    p = _safe_subpath(repo_path() / "channels", name)
    if not p.is_dir():
        die(f"channel '{name}' does not exist")
    return p


def thread_dir(channel, thread):
    p = _safe_subpath(channel_dir(channel), thread)
    if not p.is_dir():
        die(f"thread '{thread}' not found in channel '{channel}'")
    return p


def _stem_id(path):
    """Post id from filename: '001' for legacy, '20260512T143022123Z' for v3."""
    return path.name.split("-", 1)[0]


def _ord(path):
    """Numeric prefix of a legacy `NNN-*.md` post filename, as int."""
    return int(path.name.split("-", 1)[0])


def _post_files(tdir):
    """All post files sorted lexically by stem.

    Legacy `NNN-*.md` (start with `0`-`9`) lex-precedes v3
    `<timestampZ>-*.md` (start with `2`), so mixed-format threads sort
    in chronological order by construction.
    """
    legacy = list(tdir.glob("[0-9][0-9][0-9]-*.md"))
    v3 = list(tdir.glob("[12][0-9][0-9][0-9][01][0-9][0-3][0-9]T*.md"))
    return sorted(legacy + v3, key=lambda p: p.name)


def resolve_short_ref(tdir, short):
    """Resolve a short body-citation like `143022` to a full post id within
    a thread. Returns the unique full id, or None on no/ambiguous match."""
    s = str(short)
    hits = []
    for p in _post_files(tdir):
        sid = _stem_id(p)
        if sid == s or s in sid:
            hits.append(sid)
    if len(hits) == 1:
        return hits[0]
    return None


def cmd_list_threads(args):
    _activate_server(args)
    if not args.no_pull:
        maybe_pull()
    ch = channel_dir(args.channel)
    threads = []
    for t in sorted(p for p in ch.iterdir() if p.is_dir()):
        meta = read_json(t / "meta.json", {})
        posts = _post_files(t)
        last_author = ""
        if posts:
            fm, _ = parse_post(posts[-1].read_text())
            au = fm.get("author", "") or ""
            al = fm.get("alias")
            last_author = f"{au}/{al}" if al else au
        threads.append({
            "id": meta.get("id", t.name),
            "title": meta.get("title", t.name),
            "summary": meta.get("summary", ""),
            "kind": meta.get("kind", "discussion"),
            "status": meta.get("status", "open"),
            "assignee": meta.get("assignee"),
            "replies": max(0, len(posts) - 1),
            "last_author": last_author,
            "updated": datetime.fromtimestamp(t.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
    threads.sort(key=lambda x: x["updated"], reverse=True)
    if args.limit:
        threads = threads[: args.limit]
    out(threads)


def cmd_read_thread(args):
    _watch_ack()  # reading clears the unread badge (additive, best-effort)
    _activate_server(args)
    if not args.no_pull:
        maybe_pull()
    t = thread_dir(args.channel, args.thread)
    meta = read_json(t / "meta.json", {})
    posts = []
    for p in _post_files(t):
        sid = _stem_id(p)
        if args.since:
            try:
                if sid.isdigit() and int(sid) < args.since:
                    continue
            except ValueError:
                pass
        fm, body = parse_post(p.read_text())
        pid = fm.get("id", sid)
        att_dir = t / "attachments" / pid
        attachments = []
        if att_dir.is_dir():
            for af in sorted(att_dir.iterdir()):
                if af.is_file():
                    try:
                        size = af.stat().st_size
                    except OSError:
                        size = None
                    attachments.append({
                        "name": af.name,
                        "path": f"attachments/{pid}/{af.name}",
                        "size": size,
                    })
        posts.append({
            "id": pid,
            "filename": p.name,
            "in_reply_to": fm.get("in_reply_to"),
            "author": fm.get("author"),
            "alias": fm.get("alias"),
            "via": fm.get("via"),
            "model": fm.get("model"),
            "stance": fm.get("stance"),
            "timestamp": fm.get("timestamp"),
            "body": _wrap_untrusted(body.strip(), fm.get("author")),
            "attachments": attachments,
        })
    payload = {"meta": meta, "posts": posts}
    # Advisory: thread carries a password_hash but reads aren't actually
    # gated by it -- the repo clone is the only confidentiality boundary.
    # Surface this so consumers (UIs, agents) can warn the caller.
    if meta.get("password_hash"):
        members = read_json(t / "members.json", {"members": []}).get("members", [])
        is_member = any(m.get("author") == author() for m in members)
        payload["gated"] = {
            "scheme": "join-only",
            "is_member": is_member,
            "note": "password gates `join`, not read. Repo clone access = read access.",
        }
    out(payload)


def _attach_root():
    """Directory whose contents we consider 'expected' for --body-file attachments.

    $PAIR_PRESSURE_ATTACH_ROOT if set, else CWD. Used today only to decide
    whether to emit a stderr warning when an attached path resolves outside
    it; left in place so a future strict-mode (refuse, not warn) can hang
    off the same definition.
    """
    return Path(os.environ.get("PAIR_PRESSURE_ATTACH_ROOT") or os.getcwd()).resolve()


def _warn_if_outside_attach_root(path_str):
    """Emit a stderr warning when `path_str` resolves outside the attach root.

    Non-blocking by design: pp runs with the caller's own privileges, so
    --body-file is no more powerful than `cat`. The warning exists so a
    human watching the session can spot an agent attaching files from
    unexpected places (a weak signal for prompt-injection misuse).
    """
    try:
        root = _attach_root()
        p = Path(path_str)
        if not p.is_absolute():
            p = Path.cwd() / p
        resolved = p.resolve()
        resolved.relative_to(root)
    except FileNotFoundError:
        return  # let read_text() raise the real error
    except ValueError:
        print(
            f"pp: warning: --body-file '{path_str}' resolves outside the attach root "
            f"({_attach_root()}). Set PAIR_PRESSURE_ATTACH_ROOT to silence.",
            file=sys.stderr,
        )


def read_body(args):
    # Smart verbs pre-read the body once and shove it into args.body_text so
    # downstream cmd_* calls don't re-consume stdin.
    if getattr(args, "body_text", None) is not None:
        return args.body_text
    if args.body_file == "-":
        return sys.stdin.read()
    _warn_if_outside_attach_root(args.body_file)
    return Path(args.body_file).read_text()


_ATTACH_TOKEN_RE = re.compile(r"@@(\S+)")
_ATTACH_TRAILING_PUNCT = ".,;:!?)\"'`"


def _resolve_attach_path(raw):
    """Resolve an attachment path (relative -> CWD). Returns Path or None
    if it doesn't exist / isn't a regular file. Emits the same outside-root
    warning the inline `--body-file` path uses."""
    _warn_if_outside_attach_root(raw)
    src = Path(raw)
    if not src.is_absolute():
        src = Path.cwd() / src
    try:
        src = src.resolve(strict=True)
    except (OSError, FileNotFoundError):
        return None
    if not src.is_file():
        return None
    return src


def _process_attachments(body, tdir, pid, extra_paths):
    """Copy any `@@<path>` tokens in `body` and any --attach paths into
    `<tdir>/attachments/<pid>/`, then return the rewritten body.

    `@@<path>` tokens are replaced inline with a relative markdown link
    `[<basename>](attachments/<pid>/<basename>)`. Tokens whose path does
    not resolve to a real file are left untouched (so prose containing a
    stray `@@` doesn't fail the post). `--attach` paths must exist and are
    appended as an `## Attachments` bullet list.

    Runs inside the write_payload closure, so a rebase-retry replays it
    cleanly: the attach dir lives under the post and is recreated alongside
    a fresh post-id.
    """
    attach_dir = tdir / "attachments" / pid
    used = set()

    def _place(src_path):
        base = src_path.name
        target = attach_dir / base
        n = 2
        while target.exists() or base in used:
            stem, dot, ext = src_path.name.partition(".")
            base = f"{stem}-{n}" + ((dot + ext) if dot else "")
            target = attach_dir / base
            n += 1
        used.add(base)
        attach_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, target)
        return base

    def _maybe_replace(match):
        raw = match.group(1)
        trailing = ""
        while raw and raw[-1] in _ATTACH_TRAILING_PUNCT:
            trailing = raw[-1] + trailing
            raw = raw[:-1]
        if not raw:
            return match.group(0)
        src = _resolve_attach_path(raw)
        if src is None:
            return match.group(0)
        base = _place(src)
        return f"[{base}](attachments/{pid}/{base}){trailing}"

    new_body = _ATTACH_TOKEN_RE.sub(_maybe_replace, body)

    appended = []
    for raw in (extra_paths or []):
        src = _resolve_attach_path(raw)
        if src is None:
            die(f"--attach: file not found or not a regular file: {raw}")
        base = _place(src)
        appended.append(base)

    if appended:
        section_lines = ["", "", "## Attachments", ""]
        section_lines += [f"- [{b}](attachments/{pid}/{b})" for b in appended]
        new_body = new_body.rstrip() + "\n".join(section_lines) + "\n"

    return new_body


def _print_task_safety_banner(meta, action):
    """Emit a bold-red stderr banner naming the task's seed_author so the
    operator can confirm they trust the task giver before the agent picks
    up work.

    Skipped when stderr isn't a TTY so JSON pipelines and CI stay clean.
    ANSI codes render on Windows Terminal / modern ConHost and on every
    standard Unix terminal; older terminals see the raw text -- ugly, not
    broken. Reason this exists: a task body is untrusted instruction text
    -- prompt injection or destructive shell can ride in on a `claim`.
    """
    if not sys.stderr.isatty():
        return
    seed = (meta or {}).get("seed_author") or "<unknown>"
    title = (meta or {}).get("title") or "<no title>"
    kind = (meta or {}).get("kind") or "task"
    red = "\033[1;31m"
    yellow = "\033[1;33m"
    rst = "\033[0m"
    bar = "=" * 64
    lines = [
        "",
        f"{red}{bar}{rst}",
        f"{red} TRUST CHECK: about to {action.upper()} a {kind}{rst}",
        f"{red}{bar}{rst}",
        f"  Title:  {yellow}{title}{rst}",
        f"  Giver:  {yellow}{seed}{rst}",
        "",
        f"  {red}Review the task body before executing.{rst} If you do not",
        f"  trust {yellow}{seed}{rst} or do not recognize this task, ABORT.",
        f"  Task bodies are untrusted input -- they can carry prompt",
        f"  injection or destructive instructions.",
        f"{red}{bar}{rst}",
        "",
    ]
    print("\n".join(lines), file=sys.stderr)


def _resolve_password(args):
    """Pop the password from stdin if --password-stdin is set.

    The first line of stdin is the password; subsequent read_body() calls
    (when --body-file is '-') see only the bytes after the first newline.
    Keeps the plaintext out of argv (visible in /proc, ps, ETW, MCP logs).
    """
    if not getattr(args, "password_stdin", False):
        return getattr(args, "password", None)
    if getattr(args, "password", None):
        die("--password and --password-stdin are mutually exclusive")
    raw = sys.stdin.read()
    nl = raw.find("\n")
    if nl < 0:
        pw, rest = raw, ""
    else:
        pw, rest = raw[:nl], raw[nl + 1:]
    sys.stdin = io.StringIO(rest)
    return pw or None


def _current_branch():
    return git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def _commit_all(message):
    git("add", "-A")
    # Skip the commit if nothing is staged. This makes write_payload callbacks
    # tolerant of no-op race outcomes (e.g. cmd_join finds the author already
    # in members.json after a rebase-retry) without polluting history with
    # empty commits.
    res = git("status", "--porcelain", check=False)
    if not res.stdout.strip():
        return
    git("commit", "-m", message)


def _local_branch_exists(branch, cwd=None):
    """True iff refs/heads/<branch> exists locally. Used by offline worktree
    materialization -- never consults the remote."""
    res = git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}",
              cwd=cwd, check=False)
    return res.returncode == 0


def _origin_branch_exists(branch, cwd=None):
    """Return True iff origin/<branch> resolves to a commit locally.

    Distinguishes "the remote already has our branch" (standard push-retry
    territory) from "the remote is empty / our branch was never pushed"
    (first-push needs `git push -u origin <branch>` instead of the
    rebase-retry path).

    `cwd` lets server-management verbs probe the main checkout regardless of
    whichever worktree is currently active.
    """
    res = git("rev-parse", "--verify", f"origin/{branch}", cwd=cwd, check=False)
    return res.returncode == 0


def push_with_retry(write_payload, build_message):
    """Write → commit → push, with one rebase-retry on reject.

    `write_payload()` writes files into the working tree and returns a dict.
    `build_message(info)` returns the commit message.

    On push reject:
      - if origin/<branch> exists: abort any in-progress rebase, hard-reset
        to the remote tip, re-invoke `write_payload()` (which recomputes
        ordinals/dir-names from the fresh tree), re-commit, push again.
      - if origin/<branch> does NOT exist (empty remote, first push):
        retry with `git push -u origin <branch>` to set upstream. No
        rebase needed — there's nothing to rebase onto.
    One retry only either way.
    """
    info = write_payload()
    _commit_all(build_message(info))
    if not has_remote():
        return info
    res = git("push", check=False)
    if res.returncode == 0:
        return info
    branch = _current_branch()
    git("fetch", "origin", branch, check=False)
    if not _origin_branch_exists(branch):
        # First push to an empty remote -- set upstream and retry once.
        res2 = git("push", "-u", "origin", branch, check=False)
        if res2.returncode != 0:
            die(f"first push to empty remote failed: {res2.stderr.strip()}")
        return info
    # Push rejected against an existing remote branch. Rebase-retry path.
    git("rebase", "--abort", check=False)
    git("reset", "--hard", f"origin/{branch}")
    info = write_payload()
    _commit_all(build_message(info))
    res2 = git("push", check=False)
    if res2.returncode != 0:
        die(f"push rejected after rebase-retry: {res2.stderr.strip()}")
    return info


def _initial_status(kind):
    return {
        "task": "unclaimed",
        "decision": "proposed",
    }.get(kind, "open")


def _password_hash(password):
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _check_membership(members, me):
    """Return None if `me` may act on a thread with these members, else an
    `{ok: False, ...}` dict suitable for direct emission.

    A thread with no `members.json` (or an empty list) is treated as open —
    membership is advisory only when someone has joined.
    """
    if not members:
        return None
    if any(m.get("author") == me for m in members):
        return None
    return {"ok": False, "reason": "not_a_member"}


_DECISION_OUTCOMES = ("accepted", "rejected", "superseded")


def _resolve_outcome(kind, outcome):
    """Decide what `pp resolve` should set / write for (kind, outcome).

    Returns either an `{ok: False, ...}` dict (caller should emit and bail)
    or a `(new_status, outcome_body)` tuple. `outcome_body` is None for the
    decision case — decisions encode the outcome in `status` directly and
    don't get a separate summary post.

    Decision threads MUST use one of `accepted|rejected|superseded`;
    free-text outcomes would silently produce a status that violates the
    schema in CONVENTIONS.md.
    """
    if kind == "decision":
        if outcome not in _DECISION_OUTCOMES:
            return {
                "ok": False,
                "reason": "decision_needs_enum_outcome",
                "valid": list(_DECISION_OUTCOMES),
            }
        return (outcome, None)
    return ("resolved", outcome)


def cmd_new_thread(args):
    _activate_server(args)
    maybe_pull()
    ch = channel_dir(args.channel)
    args.password = _resolve_password(args)
    body = read_body(args)

    def write_payload():
        slug = slugify(args.title)
        base = f"{today()}_{slug}"
        tid = base
        i = 2
        while (ch / tid).exists():
            tid = f"{base}-{i}"
            i += 1
        tdir = ch / tid
        tdir.mkdir(parents=True)
        pid = post_id()
        attached_body = _process_attachments(
            body, tdir, pid, getattr(args, "attachments", None) or [],
        )
        (tdir / f"{pid}-seed.md").write_text(dump_slim(
            by=by_for_via(args.via, args), via=args.via, model=args.model,
            pid=pid, stance="summary", in_reply_to=None, body=attached_body,
        ))
        meta = {
            "id": tid,
            "title": args.title,
            "summary": args.summary or "",
            "seed_author": author(),
            "created_at": now_iso(),
            "kind": args.kind,
            "status": _initial_status(args.kind),
            "assignee": None,
        }
        if args.password:
            meta["password_hash"] = _password_hash(args.password)
            # Seed members.json so the creator is automatically a member —
            # otherwise they couldn't resolve their own thread.
            write_json(tdir / "members.json", {"members": [
                {"author": author(), "joined_at": now_iso()},
            ]})
        write_json(tdir / "meta.json", meta)
        return {"thread_id": tid, "path": str(tdir.relative_to(repo_path()))}

    def msg(info):
        return f"{args.channel}/{info['thread_id']}: new-thread by {by_for_via(args.via, args)} [via {_short_via(args.via)}]"

    out(push_with_retry(write_payload, msg))


def cmd_reply(args):
    _activate_server(args)
    maybe_pull()
    t = thread_dir(args.channel, args.thread)
    body = read_body(args)

    irt = args.in_reply_to
    if irt:
        # Allow short --in-reply-to (e.g. `143022`) to resolve to a full
        # timestamp id within this thread. Legacy `--in-reply-to 001` passes
        # through unchanged (the resolver is a no-op when it can't disambiguate).
        resolved = resolve_short_ref(t, irt)
        if resolved:
            irt = resolved

    def write_payload():
        # Stamp a fresh post id per attempt -- a rebase-retry produces a new
        # millisecond and so a new filename, no collision possible.
        pid = post_id()
        fname = f"{pid}-reply.md"
        attached_body = _process_attachments(
            body, t, pid, getattr(args, "attachments", None) or [],
        )
        (t / fname).write_text(dump_slim(
            by=by_for_via(args.via, args), via=args.via, model=args.model,
            pid=pid, stance=args.stance, in_reply_to=irt, body=attached_body,
        ))
        if args.summary is not None:
            meta_p = t / "meta.json"
            meta = read_json(meta_p, {})
            meta["summary"] = args.summary
            write_json(meta_p, meta)
        return {"reply_id": pid, "filename": fname}

    def msg(info):
        return f"{args.channel}/{args.thread}: reply {info['reply_id']} by {by_for_via(args.via, args)} [via {_short_via(args.via)}]"

    out(push_with_retry(write_payload, msg))


def _read_claim(t):
    return read_json(t / "claim.json", None)


def _is_locked_by_other(claim, me):
    """Claim is held if claim.json exists, assignee != me, and not abandoned."""
    if not claim:
        return False
    if claim.get("state") == "abandoned":
        return False
    return claim.get("assignee") != me


def _require_task(t, meta):
    if meta.get("kind") != "task":
        die(f"thread is kind={meta.get('kind')!r}, claim verbs only apply to kind=task", code=3)


def lock_transition(t, precheck, apply_fn, success_response, commit_message):
    """Run a task-thread state transition with one rebase-retry on push reject.

    `precheck()` returns either a dict (an `ok:false` response — printed and
    bail) or `None` to proceed. It is re-invoked after a hard-reset on push
    rejection, so the precondition is checked against the freshly-pulled tree
    before we re-apply our mutation.
    """
    err = precheck()
    if err is not None:
        out(err)
        return
    apply_fn()
    _commit_all(commit_message)
    if not has_remote():
        out(success_response)
        return
    res = git("push", check=False)
    if res.returncode == 0:
        out(success_response)
        return
    branch = _current_branch()
    git("fetch", "origin", branch, check=False)
    if not _origin_branch_exists(branch):
        # Empty remote -- first push needs --set-upstream and skips the
        # rebase entirely (nothing to rebase onto).
        res2 = git("push", "-u", "origin", branch, check=False)
        if res2.returncode != 0:
            die(f"first push to empty remote failed: {res2.stderr.strip()}")
        out(success_response)
        return
    git("rebase", "--abort", check=False)
    git("reset", "--hard", f"origin/{branch}")
    err = precheck()
    if err is not None:
        out(err)
        return
    apply_fn()
    _commit_all(commit_message)
    res2 = git("push", check=False)
    if res2.returncode != 0:
        die(f"push rejected after rebase-retry: {res2.stderr.strip()}")
    out(success_response)


def cmd_claim(args):
    _activate_server(args)
    maybe_pull()
    t = thread_dir(args.channel, args.thread)
    me = author()
    success = {"ok": True, "assignee": me, "state": "claimed"}
    _print_task_safety_banner(read_json(t / "meta.json", {}), action="claim")

    def precheck():
        meta = read_json(t / "meta.json", {})
        _require_task(t, meta)
        claim = _read_claim(t)
        if _is_locked_by_other(claim, me):
            return {
                "ok": False,
                "claimed_by": claim.get("assignee"),
                "state": claim.get("state"),
                "claimed_at": claim.get("claimed_at"),
            }
        return None

    def apply_fn():
        existing = _read_claim(t) or {}
        # Preserve the original claimed_at iff this is a re-claim of an
        # abandoned task by us; otherwise stamp fresh.
        claim = {
            "assignee": me,
            "claimed_at": now_iso(),
            "claimed_via": args.via,
            "state": "claimed",
        }
        if existing.get("state") == "abandoned" and existing.get("assignee") == me:
            claim["claimed_at"] = existing.get("claimed_at", claim["claimed_at"])
        write_json(t / "claim.json", claim)
        meta_p = t / "meta.json"
        meta = read_json(meta_p, {})
        meta["assignee"] = me
        meta["status"] = "claimed"
        write_json(meta_p, meta)

    lock_transition(
        t, precheck, apply_fn, success,
        f"{args.channel}/{args.thread}: claim by {me} [via {args.via}]",
    )


def _require_assignee(t, me):
    """Return an ok:false dict if claim.json is missing or owned by someone else."""
    claim = _read_claim(t)
    if not claim:
        return {"ok": False, "error": "task is not claimed"}
    if claim.get("assignee") != me:
        return {"ok": False, "error": "not assignee", "claimed_by": claim.get("assignee")}
    if claim.get("state") == "abandoned":
        return {"ok": False, "error": "task was abandoned; re-claim first"}
    return None


def _set_state(t, new_claim_state, new_meta_status, **extra_meta):
    claim_p = t / "claim.json"
    claim = read_json(claim_p, {})
    claim["state"] = new_claim_state
    write_json(claim_p, claim)
    meta_p = t / "meta.json"
    meta = read_json(meta_p, {})
    meta["status"] = new_meta_status
    for k, v in extra_meta.items():
        meta[k] = v
    write_json(meta_p, meta)


def cmd_start(args):
    _activate_server(args)
    maybe_pull()
    t = thread_dir(args.channel, args.thread)
    me = author()
    _print_task_safety_banner(read_json(t / "meta.json", {}), action="start")

    def precheck():
        meta = read_json(t / "meta.json", {})
        _require_task(t, meta)
        return _require_assignee(t, me)

    def apply_fn():
        _set_state(t, "in_progress", "in_progress")

    lock_transition(
        t, precheck, apply_fn, {"ok": True, "state": "in_progress"},
        f"{args.channel}/{args.thread}: start by {me}",
    )


def cmd_complete(args):
    _activate_server(args)
    maybe_pull()
    t = thread_dir(args.channel, args.thread)
    me = author()

    def precheck():
        meta = read_json(t / "meta.json", {})
        _require_task(t, meta)
        return _require_assignee(t, me)

    def apply_fn():
        extra = {"summary": args.summary} if args.summary is not None else {}
        _set_state(t, "done", "done", **extra)

    lock_transition(
        t, precheck, apply_fn, {"ok": True, "state": "done"},
        f"{args.channel}/{args.thread}: complete by {me}",
    )


def cmd_abandon(args):
    _activate_server(args)
    maybe_pull()
    t = thread_dir(args.channel, args.thread)
    me = author()

    def precheck():
        meta = read_json(t / "meta.json", {})
        _require_task(t, meta)
        claim = _read_claim(t)
        if not claim:
            return {"ok": False, "error": "task is not claimed"}
        if claim.get("assignee") != me and not args.force:
            return {
                "ok": False,
                "error": "not assignee; pass --force to override",
                "claimed_by": claim.get("assignee"),
            }
        return None

    def apply_fn():
        claim_p = t / "claim.json"
        claim = read_json(claim_p, {})
        claim["state"] = "abandoned"
        if args.reason:
            claim["abandon_reason"] = args.reason
        write_json(claim_p, claim)
        meta_p = t / "meta.json"
        meta = read_json(meta_p, {})
        meta["status"] = "unclaimed"
        meta["assignee"] = None
        write_json(meta_p, meta)

    lock_transition(
        t, precheck, apply_fn, {"ok": True, "state": "abandoned"},
        f"{args.channel}/{args.thread}: abandon by {me}",
    )


def cmd_handoff(args):
    _activate_server(args)
    maybe_pull()
    t = thread_dir(args.channel, args.thread)
    me = author()

    def precheck():
        meta = read_json(t / "meta.json", {})
        _require_task(t, meta)
        return _require_assignee(t, me)

    def apply_fn():
        claim_p = t / "claim.json"
        claim = read_json(claim_p, {})
        claim["assignee"] = args.to
        claim["state"] = "claimed"
        claim["handed_off_from"] = me
        claim["handed_off_at"] = now_iso()
        write_json(claim_p, claim)
        meta_p = t / "meta.json"
        meta = read_json(meta_p, {})
        meta["assignee"] = args.to
        meta["status"] = "claimed"
        write_json(meta_p, meta)

    lock_transition(
        t, precheck, apply_fn, {"ok": True, "new_assignee": args.to},
        f"{args.channel}/{args.thread}: handoff {me} -> {args.to}",
    )


def cmd_join(args):
    """Record current author as a member of a thread.

    Idempotent: re-joining when already a member is a no-op success.
    Password is enforced at join time only — reads/replies are NOT gated in
    v1 (advisory only). See plan doc for the v0.2 enforcement story.

    Precheck-then-write is safe here because `password_hash` is write-once
    in v1 (only `new-thread --password` sets it, no API to mutate later).
    A concurrent rebase-retry can only add more members, never invalidate
    our password verification.
    """
    _activate_server(args)
    maybe_pull()
    t = thread_dir(args.channel, args.thread)
    me = author()
    meta = read_json(t / "meta.json", {})
    ph = meta.get("password_hash")
    args.password = _resolve_password(args)
    if ph:
        if not args.password:
            out({"ok": False, "reason": "password_required"})
            return
        if _password_hash(args.password) != ph:
            out({"ok": False, "reason": "bad_password"})
            return

    def write_payload():
        members_p = t / "members.json"
        data = read_json(members_p, {"members": []})
        members = data.get("members", [])
        if not any(m.get("author") == me for m in members):
            members.append({"author": me, "joined_at": now_iso()})
            data["members"] = members
            write_json(members_p, data)
        return {
            "ok": True,
            "thread": args.thread,
            "members": [m.get("author") for m in members],
        }

    def msg(info):
        return f"{args.channel}/{args.thread}: join {me}"

    out(push_with_retry(write_payload, msg))


def cmd_resolve(args):
    """Mark a discussion/investigation/decision thread resolved.

    Rejects task threads (use `complete`). Enforces members.json membership
    when present — the only v1 read/write check that consults membership.
    For decision threads, `--outcome accepted|rejected|superseded` is used
    as the new status; for other kinds, `--outcome` is appended as a
    free-text summary post.

    Precheck-then-write is safe here because `kind` and `members.json` are
    write-once in v1 (no API mutates either after thread creation / join).
    A concurrent rebase-retry can only add more members, never remove our
    own membership or change the thread's kind.
    """
    _activate_server(args)
    maybe_pull()
    t = thread_dir(args.channel, args.thread)
    me = author()
    meta = read_json(t / "meta.json", {})
    kind = meta.get("kind", "discussion")
    if kind == "task":
        out({"ok": False, "reason": "use_complete_for_tasks"})
        return
    members = read_json(t / "members.json", {"members": []}).get("members", [])
    err = _check_membership(members, me)
    if err is not None:
        out(err)
        return

    routed = _resolve_outcome(kind, args.outcome)
    if isinstance(routed, dict):
        out(routed)
        return
    new_status, outcome_body = routed

    def write_payload():
        meta_p = t / "meta.json"
        meta_now = read_json(meta_p, {})
        meta_now["status"] = new_status
        write_json(meta_p, meta_now)
        if outcome_body:
            pid = post_id()
            fname = f"{pid}-reply.md"
            (t / fname).write_text(dump_slim(
                by=by_for_via(args.via, args), via=args.via, model=None,
                pid=pid, stance="summary", in_reply_to=None, body=outcome_body,
            ))
        return {"ok": True, "status": new_status, "thread": args.thread}

    def msg(info):
        return f"{args.channel}/{args.thread}: resolve by {by_for_via(args.via, args)} -> {new_status}"

    out(push_with_retry(write_payload, msg))


# ---- smart verbs (state-aware, single-call composers) ----


def _thread_exists(channel, thread):
    if not channel or not thread:
        return False
    try:
        return (repo_path() / "channels" / channel / thread).is_dir()
    except (OSError, ValueError):
        return False


def _channel_archived(ch_dir):
    """True when a channel dir is archived (channel.json `archived: true`).
    Archived channels keep their history but drop out of list/feed/read/watch."""
    try:
        meta = read_json(ch_dir / "channel.json", {})
    except (OSError, ValueError):
        return False
    return bool(isinstance(meta, dict) and meta.get("archived"))


def _active_channel_dirs(channels_root):
    """Sorted, non-archived channel subdirs under a `channels/` root."""
    if not channels_root.is_dir():
        return []
    return [p for p in sorted(channels_root.iterdir())
            if p.is_dir() and not _channel_archived(p)]


def _channel_active(name):
    """True when channel `name` exists AND is not archived."""
    if not name:
        return False
    d = repo_path() / "channels" / name
    return d.is_dir() and not _channel_archived(d)


def _archived_names(wt):
    """Set of archived channel names from a worktree's channels/*/channel.json.
    The watcher's git-ls-tree scan lists committed paths but not their content,
    so it filters with this (read off the local worktree)."""
    names = set()
    ch_root = wt / "channels"
    if not ch_root.is_dir():
        return names
    for ch_dir in ch_root.iterdir():
        if ch_dir.is_dir() and _channel_archived(ch_dir):
            names.add(ch_dir.name)
    return names


def _find_thread_by_title_slug(channel, title):
    """Return the most-recently-modified thread id in `channel` whose id ends
    with `_<slug(title)>`, or None."""
    slug = slugify(title)
    ch_root = repo_path() / "channels" / channel
    if not ch_root.is_dir():
        return None
    candidates = []
    for tdir in ch_root.iterdir():
        if not tdir.is_dir():
            continue
        # IDs look like `YYYY-MM-DD_<slug>` or `YYYY-MM-DD_<slug>-N`. We accept
        # either, preferring the freshest.
        name = tdir.name
        base = name.split("_", 1)[1] if "_" in name else name
        # Strip trailing "-N" disambiguators
        stem = re.sub(r"-\d+$", "", base)
        if stem == slug:
            candidates.append(tdir)
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0].name


def _ensure_channel_inline(server, name):
    """Make sure the channel exists AND is active in the active server's
    worktree. Sending to an archived channel revives it. Idempotent."""
    if _channel_active(name):
        return
    ns = argparse.Namespace(name=name, description=None, server=server)
    _capture(cmd_channel_ensure, ns)


def cmd_send(args):
    """Smart post: resolve (server, channel, thread, title), ensure channel,
    pick or create the thread, then post the body.

    Replaces 2-4 LLM-orchestrated `pp` calls with one. State is updated on
    success so the next call lands on the same thread automatically."""
    resolved = resolve_active(args)
    title = resolved["title"]

    # Pre-read body and password ONCE -- downstream cmd_* will see body_text
    # and skip stdin. (Avoids double-reading or losing bytes after the
    # password line is consumed.)
    args.password = _resolve_password(args)
    body = read_body(args)
    args.body_text = body
    args.password_stdin = False

    _activate_server(args)
    maybe_pull()
    _ensure_channel_inline(args.server, args.channel)

    # Always-on awareness: when the channel was resolved implicitly (state/env/
    # default, not an explicit --channel), tell the user where this lands.
    csource = resolved["sources"].get("channel")
    if csource != "arg":
        print(f"(active channel: #{args.channel} [from {csource}])",
              file=sys.stderr)

    # Stored thread might point at one that no longer exists or moved
    # channels; fall through to title-match in that case.
    target = args.thread if _thread_exists(args.channel, args.thread) else None
    if not target:
        target = _find_thread_by_title_slug(args.channel, title)

    if target:
        # Reply path
        reply_args = argparse.Namespace(
            server=args.server,
            channel=args.channel,
            thread=target,
            stance=getattr(args, "stance", None) or "extend",
            in_reply_to=getattr(args, "in_reply_to", None),
            body_file="-",
            body_text=body,
            summary=getattr(args, "summary", None),
            via=getattr(args, "via", None) or "claude-code",
            model=getattr(args, "model", None),
            alias=getattr(args, "alias", None),
            attachments=getattr(args, "attachments", None) or [],
        )
        payload = _capture(cmd_reply, reply_args) or {}
        _state_save(server=args.server, channel=args.channel,
                    thread_id=target, source="send")
        out({
            "ok": True,
            "kind": "reply",
            "server": args.server,
            "channel": args.channel,
            "channel_source": csource,
            "thread_id": target,
            "post_id": payload.get("reply_id"),
        })
        return

    # Create thread path -- body becomes the seed.
    nt_args = argparse.Namespace(
        server=args.server,
        channel=args.channel,
        title=title,
        kind="discussion",
        body_file="-",
        body_text=body,
        summary=getattr(args, "summary", None) or "",
        via=getattr(args, "via", None) or "claude-code",
        model=getattr(args, "model", None),
        alias=getattr(args, "alias", None),
        password=args.password,
        password_stdin=False,
        attachments=getattr(args, "attachments", None) or [],
    )
    payload = _capture(cmd_new_thread, nt_args) or {}
    new_tid = payload.get("thread_id")
    _state_save(server=args.server, channel=args.channel,
                thread_id=new_tid, source="send")
    out({
        "ok": True,
        "kind": "seed",
        "server": args.server,
        "channel": args.channel,
        "channel_source": csource,
        "thread_id": new_tid,
        "post_id": None,
    })


def _emit_read(args, payload):
    """Emit a read payload: ANSI-colored chat when --pretty (and not being
    captured in-process), else the JSON contract. Keeps every machine
    consumer (MCP shim, _capture) on JSON untouched."""
    if getattr(args, "pretty", False) and _OUT_CAPTURE is None:
        _render_chat(payload)
    else:
        out(payload)


def cmd_read(args):
    """Smart read: no target → feed; exact channel name → channel feed;
    otherwise fuzzy thread match → read-thread + update state. Ambiguous
    matches surface for caller-side disambiguation rather than guessing."""
    _watch_ack()  # reading clears the unread badge (additive, best-effort)
    # We only need server resolution here; channel comes from target or state.
    sess, glob = _state_load()
    server_arg = getattr(args, "server", None)
    if not server_arg:
        if sess and sess.get("server"):
            args.server = sess["server"]
        elif glob and glob.get("server"):
            args.server = glob["server"]
    _activate_server(args)
    if not getattr(args, "no_pull", False):
        maybe_pull()

    # Where the user's next /pp-chat:send will land -- surfaced in every
    # feed/channel view so they're never unsure of their active channel.
    active_ch = (
        (sess.get("channel") if sess else None)
        or (glob.get("channel") if glob else None)
        or _default_channel()
    )

    mid = getattr(args, "message_id", None)
    if mid:
        hit = _find_post_by_id(mid)
        if hit is None:
            _emit_read(args, {"view": "message", "matched": False,
                              "query": mid})
        elif isinstance(hit, dict) and "ambiguous" in hit:
            _emit_read(args, {"view": "ambiguous_message", "query": mid,
                              "matches": hit["ambiguous"]})
        else:
            _emit_read(args, {"view": "message", "post": hit})
        return

    target = (args.target or "").strip()

    if not target:
        feed_args = argparse.Namespace(
            server=args.server, channel=None, since=None,
            limit=args.limit or 30, no_pull=True,
        )
        _emit_read(args, {"view": "feed", "active_channel": active_ch,
                          "posts": _capture(cmd_feed, feed_args) or []})
        return

    # Exact channel match? (archived channels are hidden -- fall through.)
    if _channel_active(target):
        feed_args = argparse.Namespace(
            server=args.server, channel=target, since=None,
            limit=args.limit or 30, no_pull=True,
        )
        _emit_read(args, {"view": "channel", "channel": target,
                          "active_channel": active_ch,
                          "posts": _capture(cmd_feed, feed_args) or []})
        return

    # Fuzzy thread match. Prefer the channel-context candidate; otherwise
    # search across channels.
    ctx_channel = (sess or {}).get("channel") if sess else None
    if not ctx_channel and glob:
        ctx_channel = glob.get("channel")

    matches = _fuzzy_thread_candidates(target, ctx_channel)
    if len(matches) == 1:
        ch, tid = matches[0]
        rt_args = argparse.Namespace(
            server=args.server, channel=ch, thread=tid, since=0, no_pull=True,
        )
        payload = _capture(cmd_read_thread, rt_args) or {}
        _state_save(server=args.server, channel=ch, thread_id=tid, source="read")
        _emit_read(args, {"view": "thread", "server": args.server,
                          "channel": ch, "thread_id": tid, **payload})
        return

    if len(matches) > 1:
        _emit_read(args, {"view": "ambiguous", "matches": [
            {"channel": c, "thread_id": t} for c, t in matches[:20]
        ]})
        return

    # No match: fall back to feed and tell the caller nothing matched.
    feed_args = argparse.Namespace(
        server=args.server, channel=None, since=None,
        limit=args.limit or 30, no_pull=True,
    )
    _emit_read(args, {"view": "feed", "matched": False, "query": target,
                      "active_channel": active_ch,
                      "posts": _capture(cmd_feed, feed_args) or []})


def _post_row(post_file, channel, thread_dir, thread_title):
    """Feed-row dict for a single post with the FULL (untruncated) body.
    Mirrors a `_collect_feed_posts` row so the --pretty/JSON renderers handle
    it unchanged."""
    fm, body = parse_post(post_file.read_text())
    meta = read_json(thread_dir / "meta.json", {})
    return {
        "channel": channel,
        "thread": thread_dir.name,
        "thread_title": thread_title,
        "thread_kind": meta.get("kind", "discussion"),
        "thread_status": meta.get("status"),
        "id": fm.get("id", _stem_id(post_file)),
        "author": fm.get("author"),
        "alias": fm.get("alias"),
        "via": fm.get("via"),
        "stance": fm.get("stance"),
        "timestamp": fm.get("timestamp") or "",
        "body": _wrap_untrusted(body.strip(), fm.get("author")),
        "filename": post_file.name,
    }


def _find_post_by_id(post_id):
    """Locate one post by id across the active server's (non-archived)
    channels. `post_id` may be a full id (`YYYYMMDDTHHMMSSfffZ` or legacy
    `NNN`) or a short handle (e.g. the last 6 chars shown in --pretty).
    Returns the full-body post row on a unique match, `{"ambiguous": [...]}`
    when a short handle hits >1 post, or None on no match. An exact full-id
    match always wins over partial hits."""
    q = str(post_id or "").strip()
    if not q:
        return None
    ch_root = repo_path() / "channels"
    if not ch_root.is_dir():
        return None
    partial = []
    for ch_dir in _active_channel_dirs(ch_root):
        for tdir in sorted(p for p in ch_dir.iterdir() if p.is_dir()):
            meta = read_json(tdir / "meta.json", {})
            title = meta.get("title", tdir.name)
            for pf in _post_files(tdir):
                sid = _stem_id(pf)
                if sid == q:
                    return _post_row(pf, ch_dir.name, tdir, title)
                if q in sid:
                    partial.append((pf, ch_dir.name, tdir, title, sid))
    if len(partial) == 1:
        pf, ch, tdir, title, _sid = partial[0]
        return _post_row(pf, ch, tdir, title)
    if len(partial) > 1:
        return {"ambiguous": [
            {"channel": ch, "thread": tdir.name, "id": sid,
             "thread_title": title}
            for (pf, ch, tdir, title, sid) in partial[:20]
        ]}
    return None


def _fuzzy_thread_candidates(query, preferred_channel):
    """Substring match against thread ids and titles. Returns list of
    (channel, thread_id) tuples, with `preferred_channel` matches first."""
    q = query.lower()
    ch_root = repo_path() / "channels"
    if not ch_root.is_dir():
        return []
    hits = []
    for ch_dir in _active_channel_dirs(ch_root):
        for tdir in ch_dir.iterdir():
            if not tdir.is_dir():
                continue
            meta = read_json(tdir / "meta.json", {})
            title = (meta.get("title") or "").lower()
            tid = tdir.name.lower()
            if q in tid or q in title:
                hits.append((ch_dir.name, tdir.name))
    if preferred_channel:
        hits.sort(key=lambda x: 0 if x[0] == preferred_channel else 1)
    return hits


def cmd_task_new(args):
    """Smart task-new: resolve server/channel, create the task, optionally
    claim+handoff to --to, update state."""
    resolved = resolve_active(args)
    title = args.title

    args.password = _resolve_password(args)
    body = read_body(args) if getattr(args, "body_file", None) else (
        "## Context\n\n## What \"done\" looks like\n\n## Constraints\n"
    )
    args.body_text = body
    args.password_stdin = False

    _activate_server(args)
    maybe_pull()
    _ensure_channel_inline(args.server, args.channel)

    nt_args = argparse.Namespace(
        server=args.server,
        channel=args.channel,
        title=title,
        kind="task",
        body_file="-",
        body_text=body,
        summary="",
        via=getattr(args, "via", None) or "human",
        model=None,
        alias=None,
        password=args.password,
        password_stdin=False,
        attachments=getattr(args, "attachments", None) or [],
    )
    nt_payload = _capture(cmd_new_thread, nt_args) or {}
    tid = nt_payload.get("thread_id")
    assignee = None

    if args.to:
        cl_args = argparse.Namespace(server=args.server, channel=args.channel,
                                     thread=tid, via=(args.via or "human"))
        _capture(cmd_claim, cl_args)
        ho_args = argparse.Namespace(server=args.server, channel=args.channel,
                                     thread=tid, to=args.to)
        _capture(cmd_handoff, ho_args)
        assignee = args.to

    _state_save(server=args.server, channel=args.channel,
                thread_id=tid, source="task_new")
    out({
        "ok": True,
        "server": args.server,
        "channel": args.channel,
        "thread_id": tid,
        "assignee": assignee,
    })


def cmd_task_done(args):
    """Smart task-done: complete the current thread from state. Refuses if
    no current thread or if the resolved thread is not kind=task."""
    resolve_active(args)
    if not args.thread:
        out({"ok": False, "error": "no current thread in state; pass "
             "--channel and --thread, or use /pp-chat:read to set context"})
        return
    _activate_server(args)
    maybe_pull()
    if not _thread_exists(args.channel, args.thread):
        out({"ok": False, "error": "current thread not found on this server",
             "server": args.server, "channel": args.channel,
             "thread_id": args.thread})
        return
    meta = read_json(repo_path() / "channels" / args.channel / args.thread / "meta.json", {})
    if meta.get("kind") != "task":
        out({"ok": False, "error": "current thread is not a task",
             "kind": meta.get("kind")})
        return
    comp_args = argparse.Namespace(
        server=args.server, channel=args.channel, thread=args.thread,
        summary=getattr(args, "summary", None),
    )
    payload = _capture(cmd_complete, comp_args) or {}
    out({"ok": payload.get("ok", True), "state": payload.get("state", "done"),
         "server": args.server, "channel": args.channel,
         "thread_id": args.thread})


# ---- indexed task verbs (#n resolves against the last `pp task list`) ----

def _task_index_save(server, items):
    """Persist the #n -> (channel, thread_id) map into both state files,
    preserving the routine server/channel/thread fields. Best-effort."""
    block = {"server": server, "built_at": now_iso(), "items": items}
    for path in (_state_path_session(), _state_path_global()):
        if path is None:
            continue
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = _state_load_one(path) or {}
            data["task_index"] = block
            path.write_text(json.dumps(data, indent=2) + "\n",
                            encoding="utf-8")
        except OSError:
            continue


def _task_index_load():
    """Session-first, then global (mirrors _state_load precedence)."""
    if _session_id():
        sess = _state_load_one(_state_path_session())
        if sess and isinstance(sess.get("task_index"), dict):
            return sess["task_index"]
    glob = _state_load_one(_state_path_global())
    if glob and isinstance(glob.get("task_index"), dict):
        return glob["task_index"]
    return {}


def _find_thread_channel(tid):
    """Channel dir containing thread id `tid`, or None. Intentionally includes
    archived channels: explicit task/thread refs must stay resolvable even when
    their channel is archived (archive declutters browsing, not actioning)."""
    ch_root = repo_path() / "channels"
    if not ch_root.is_dir():
        return None
    for ch_dir in sorted(p for p in ch_root.iterdir() if p.is_dir()):
        if (ch_dir / tid).is_dir():
            return ch_dir.name
    return None


def _resolve_task_ref(args, ref):
    """Resolve `#n` / thread-id / fuzzy-title to (channel, thread_id).

    Returns the tuple, or None after emitting an ambiguous/no-match payload
    (caller must `return`). Hard index errors die() with remediation."""
    ref = (ref or "").strip()
    if re.match(r"^#?\d+$", ref):
        n = ref.lstrip("#")
        idx = _task_index_load()
        if not idx:
            die("no task index yet; run `pp task list` first")
        if idx.get("server") != args.server:
            die(f"task index is for server '{idx.get('server')}', not "
                f"'{args.server}'; run `pp task list` again")
        it = (idx.get("items") or {}).get(str(n))
        if not it:
            die(f"no task #{n} in the current index; run `pp task list`")
        return it["channel"], it["thread_id"]
    if re.match(r"\d{4}-\d{2}-\d{2}_.*", ref):
        ch = _find_thread_channel(ref)
        if not ch:
            out({"ok": False, "error": f"thread id '{ref}' not found"})
            return None
        return ch, ref
    # Fuzzy substring over kind=task threads.
    q = ref.lower()
    ch_root = repo_path() / "channels"
    hits = []
    if ch_root.is_dir():
        for ch_dir in sorted(p for p in ch_root.iterdir() if p.is_dir()):
            for tdir in ch_dir.iterdir():
                if not tdir.is_dir():
                    continue
                meta = read_json(tdir / "meta.json", {})
                if meta.get("kind") != "task":
                    continue
                title = (meta.get("title") or "").lower()
                if q in tdir.name.lower() or q in title:
                    hits.append((ch_dir.name, tdir.name,
                                 meta.get("title", tdir.name)))
    if len(hits) == 1:
        return hits[0][0], hits[0][1]
    if len(hits) > 1:
        out({"ok": False, "ambiguous": [
            {"channel": c, "thread_id": t, "title": ti} for c, t, ti in hits[:20]
        ]})
        return None
    out({"ok": False, "error": f"no task matched '{ref}'"})
    return None


def cmd_task_list(args):
    resolve_active(args)
    _activate_server(args)
    if not getattr(args, "no_pull", False):
        maybe_pull()
    ch_root = repo_path() / "channels"
    rows = []
    if ch_root.is_dir():
        # The task board hides archived channels (declutter); explicit task
        # refs (#n / id / fuzzy) still resolve via _resolve_task_ref so an
        # archived channel never strands a task.
        for ch_dir in _active_channel_dirs(ch_root):
            for tdir in ch_dir.iterdir():
                if not tdir.is_dir():
                    continue
                meta = read_json(tdir / "meta.json", {})
                if meta.get("kind") != "task":
                    continue
                posts = _post_files(tdir)
                rows.append({
                    "channel": ch_dir.name,
                    "thread_id": tdir.name,
                    "title": meta.get("title", tdir.name),
                    "status": meta.get("status", "unclaimed"),
                    "assignee": meta.get("assignee"),
                    "last_id": _stem_id(posts[-1]) if posts else "",
                    "updated": datetime.fromtimestamp(
                        tdir.stat().st_mtime, tz=timezone.utc
                    ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                })
    rows.sort(key=lambda r: r["last_id"], reverse=True)
    items, tasks = {}, []
    for i, r in enumerate(rows, 1):
        items[str(i)] = {"channel": r["channel"], "thread_id": r["thread_id"],
                         "title": r["title"], "status": r["status"]}
        tasks.append({"n": i, **r})
    _task_index_save(args.server, items)
    out({"ok": True, "server": args.server, "count": len(tasks),
         "tasks": tasks})


def cmd_task_claim(args):
    resolve_active(args)
    _activate_server(args)
    res = _resolve_task_ref(args, args.ref)
    if res is None:
        return
    ch, tid = res
    cl = argparse.Namespace(server=args.server, channel=ch, thread=tid,
                            via=getattr(args, "via", "claude-code"))
    payload = _capture(cmd_claim, cl) or {}
    _state_save(server=args.server, channel=ch, thread_id=tid,
                source="task_claim")
    out({**payload, "server": args.server, "channel": ch, "thread_id": tid})


def cmd_task_update(args):
    resolve_active(args)
    _activate_server(args)
    res = _resolve_task_ref(args, args.ref)
    if res is None:
        return
    ch, tid = res
    st = args.status
    if st == "claimed":
        ns = argparse.Namespace(server=args.server, channel=ch, thread=tid,
                                via=getattr(args, "via", "claude-code"))
        payload = _capture(cmd_claim, ns) or {}
    elif st == "in_progress":
        ns = argparse.Namespace(server=args.server, channel=ch, thread=tid)
        payload = _capture(cmd_start, ns) or {}
    elif st == "done":
        ns = argparse.Namespace(server=args.server, channel=ch, thread=tid,
                                summary=getattr(args, "summary", None))
        payload = _capture(cmd_complete, ns) or {}
    else:  # abandoned
        ns = argparse.Namespace(server=args.server, channel=ch, thread=tid,
                                reason=getattr(args, "reason", None),
                                force=False)
        payload = _capture(cmd_abandon, ns) or {}
    _state_save(server=args.server, channel=ch, thread_id=tid,
                source="task_update")
    out({**payload, "server": args.server, "channel": ch, "thread_id": tid})


def cmd_task_show(args):
    resolve_active(args)
    _activate_server(args)
    res = _resolve_task_ref(args, args.ref)
    if res is None:
        return
    ch, tid = res
    ns = argparse.Namespace(server=args.server, channel=ch, thread=tid,
                            since=0, no_pull=True)
    payload = _capture(cmd_read_thread, ns) or {}
    _state_save(server=args.server, channel=ch, thread_id=tid,
                source="task_show")
    out({"view": "thread", "server": args.server, "channel": ch,
         "thread_id": tid, **payload})


def cmd_task_handoff(args):
    resolve_active(args)
    _activate_server(args)
    res = _resolve_task_ref(args, args.ref)
    if res is None:
        return
    ch, tid = res
    ns = argparse.Namespace(server=args.server, channel=ch, thread=tid,
                            to=args.to)
    payload = _capture(cmd_handoff, ns) or {}
    out({**payload, "server": args.server, "channel": ch, "thread_id": tid})


def cmd_task_abandon(args):
    resolve_active(args)
    _activate_server(args)
    res = _resolve_task_ref(args, args.ref)
    if res is None:
        return
    ch, tid = res
    ns = argparse.Namespace(server=args.server, channel=ch, thread=tid,
                            reason=getattr(args, "reason", None),
                            force=getattr(args, "force", False))
    payload = _capture(cmd_abandon, ns) or {}
    out({**payload, "server": args.server, "channel": ch, "thread_id": tid})


def _read_saved_env():
    """Walk both Claude Code settings files for saved PAIR_PRESSURE_* values.

    Designed never to die: malformed JSON / missing files just contribute
    nothing. Returns a dict of the keys that were found (settings.local.json
    wins over settings.json when both define the same key).
    """
    saved = {}
    candidates = [
        Path.home() / ".claude" / "settings.local.json",
        Path.home() / ".claude" / "settings.json",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8-sig").strip()
        except OSError:
            continue
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        env_block = data.get("env") or {}
        if not isinstance(env_block, dict):
            continue
        for k in ("PAIR_PRESSURE_AUTHOR", "PAIR_PRESSURE_REPO", "PAIR_PRESSURE_ALIAS"):
            if k in env_block and k not in saved:
                saved[k] = env_block[k]
    return saved


def cmd_offline(args):
    """Show or set offline mode. Offline = skip fetch/pull/push; commits stay
    local and sync on the next online verb. Persisted machine-globally in
    ~/.pair-pressure/config.json; PAIR_PRESSURE_OFFLINE env overrides it."""
    ev = os.environ.get("PAIR_PRESSURE_OFFLINE")
    cfg = _config_load().get("offline", None)
    state = getattr(args, "state", None)
    if state is None:
        if ev is not None and ev.strip() != "":
            source = "env"
        elif cfg is not None:
            source = "config"
        else:
            source = "default"
        out({"offline": _offline(), "source": source,
             "env": ev, "config": cfg})
        return
    want = state == "true"
    _config_save({"offline": want})
    payload = {"offline": want, "saved": True,
               "note": ("offline: commits stay local, no fetch/pull/push"
                        if want else
                        "online: verbs sync normally again")}
    if ev is not None and ev.strip() != "":
        payload["warning"] = ("PAIR_PRESSURE_OFFLINE env override is set and "
                              "still wins until you unset it")
    out(payload)


def cmd_status(args):
    """Print pair-pressure identity status (saved vs active env vars).

    Works even when env vars aren't loaded yet -- that's the main use case
    (diagnosing "saved but not active" right after pp-setup, before a
    Claude Code restart). Does NOT call env() / repo_path() / author(),
    all of which die() if PAIR_PRESSURE_REPO / _AUTHOR are unset.

    Output schema:
        {
          "saved":   {"PAIR_PRESSURE_AUTHOR": "...", "PAIR_PRESSURE_REPO": "..."},
          "active":  {"PAIR_PRESSURE_AUTHOR": "...", "PAIR_PRESSURE_REPO": "..."},
          "verdict": "ready" | "needs_restart" | "not_configured" |
                     "mismatch" | "active_only",
          "message": "<one-line summary suitable for direct display>"
        }
    """
    saved = _read_saved_env()
    active = {
        "PAIR_PRESSURE_AUTHOR": os.environ.get("PAIR_PRESSURE_AUTHOR") or None,
        "PAIR_PRESSURE_REPO":   os.environ.get("PAIR_PRESSURE_REPO")   or None,
        "PAIR_PRESSURE_ALIAS":  os.environ.get("PAIR_PRESSURE_ALIAS")  or None,
    }
    # ALIAS is optional -- it doesn't gate readiness; only AUTHOR + REPO do.
    keys = ("PAIR_PRESSURE_AUTHOR", "PAIR_PRESSURE_REPO")
    saved_full  = all(saved.get(k)  for k in keys)
    active_full = all(active.get(k) for k in keys)

    if not saved_full and not active_full:
        verdict = "not_configured"
        message = "Not configured. Run `pp-setup` or `./install.ps1` to set up."
    elif saved_full and not active_full:
        verdict = "needs_restart"
        message = ("Saved but not yet loaded -- fully quit and reopen Claude Code "
                   "(not /clear) to pick up these env vars.")
    elif saved_full and active_full:
        if all(saved.get(k) == active.get(k) for k in keys):
            verdict = "ready"
            message = "Ready."
        else:
            verdict = "mismatch"
            message = ("Saved and active values differ -- restart Claude Code to "
                       "sync to the saved values.")
    else:
        verdict = "active_only"
        message = ("Env vars set in shell but not in settings.local.json. "
                   "Run `pp-setup` to persist them.")
    # Server info: tolerant of missing repo/registry. The status verb is the
    # main thing users run before they have anything set up, so failures
    # here become null fields rather than die().
    servers_list = []
    active_server = None
    try:
        servers_list = [s.get("name") for s in _registry_load().get("servers", [])]
    except SystemExit:
        pass
    active_server = (
        os.environ.get("PAIR_PRESSURE_SERVER")
        or (servers_list[0] if len(servers_list) == 1 else None)
    )
    # Smart-verb state block. Reads are tolerant of missing files / missing
    # PAIR_PRESSURE_REPO -- status must work pre-configuration.
    current = {"source": "none", "server": None, "channel": None,
               "thread_id": None, "updated_at": None}
    try:
        sess, glob = _state_load()
    except SystemExit:
        sess, glob = None, None
    if sess:
        current = {"source": "per-session", **{k: sess.get(k) for k in
                   ("server", "channel", "thread_id", "updated_at")}}
    elif glob:
        current = {"source": "global", **{k: glob.get(k) for k in
                   ("server", "channel", "thread_id", "updated_at")}}
    # Repo registry block (multi-repo). Tolerant: empty when no registry.
    repos_list = [r.get("name") for r in _repos_list()]
    active_repo = _session_repo_name()
    if not active_repo and os.environ.get("PAIR_PRESSURE_REPO"):
        active_repo = "(PAIR_PRESSURE_REPO env)"
    elif not active_repo and len(repos_list) == 1:
        active_repo = repos_list[0]
    out({
        "saved": saved,
        "active": active,
        "verdict": verdict,
        "message": message,
        "alias": active.get("PAIR_PRESSURE_ALIAS") or saved.get("PAIR_PRESSURE_ALIAS"),
        "repos": repos_list,
        "active_repo": active_repo,
        "servers": servers_list,
        "active_server": active_server,
        "current": current,
        "offline": {
            "active": _offline(),
            "config": _config_load().get("offline", False),
            "env": os.environ.get("PAIR_PRESSURE_OFFLINE"),
        },
    })


def _snippet(text, query, width=160):
    """Return a one-line snippet from `text` containing `query` (case-insensitive),
    or the first non-frontmatter line if no match."""
    lower = text.lower()
    q = query.lower()
    idx = lower.find(q)
    if idx < 0:
        # Fall back to first content line outside frontmatter.
        _, body = parse_fm(text)
        for line in body.splitlines():
            line = line.strip()
            if line:
                return line[:width]
        return ""
    line_start = text.rfind("\n", 0, idx) + 1
    line_end = text.find("\n", idx)
    if line_end < 0:
        line_end = len(text)
    line = text[line_start:line_end].strip()
    if len(line) <= width:
        return line
    # Center the snippet on the match.
    rel = idx - line_start
    half = width // 2
    start = max(0, rel - half)
    end = min(len(line), start + width)
    out_str = line[start:end]
    if start > 0:
        out_str = "…" + out_str
    if end < len(line):
        out_str = out_str + "…"
    return out_str


def cmd_search(args):
    _activate_server(args)
    if not args.no_pull:
        maybe_pull()
    repo = repo_path()

    # Use git grep -l for speed when available; fall back to a manual walk if
    # the tree has no commits yet (rare, only on a freshly-init'd repo).
    ql = args.query.lower()
    paths = set()
    if repo.joinpath(".git").exists():
        res = git("grep", "-l", "-i", "-F", "-e", args.query, "--", "channels/", check=False)
        if res.returncode == 0:
            paths.update(p for p in res.stdout.splitlines() if p.endswith(".md"))
    if not paths:
        # Manual walk fallback (e.g. unborn branch with no commits).
        for ch_dir in (repo / "channels").iterdir() if (repo / "channels").exists() else []:
            if not ch_dir.is_dir():
                continue
            for tdir in ch_dir.iterdir():
                if not tdir.is_dir():
                    continue
                for p in _post_files(tdir):
                    try:
                        if ql in p.read_text().lower():
                            paths.add(str(p.relative_to(repo)))
                    except OSError:
                        continue

    # Also surface threads whose meta.json title/summary matches — users
    # naturally search by topic, and the topic often lives only in the title.
    channels_root = repo / "channels"
    if channels_root.exists():
        for meta_p in channels_root.rglob("meta.json"):
            try:
                meta = read_json(meta_p, {})
            except (OSError, ValueError):
                continue
            haystack = (meta.get("title", "") + "\n" + meta.get("summary", "")).lower()
            if ql in haystack:
                seed = meta_p.parent / "000-seed.md"
                if seed.exists():
                    paths.add(str(seed.relative_to(repo)))

    paths = sorted(paths)

    results = []
    channels_root = repo / "channels"
    for rel in paths:
        p = repo / rel
        try:
            parts = p.relative_to(channels_root).parts
        except ValueError:
            continue
        if len(parts) < 3:
            continue
        channel, thread, _ = parts[0], parts[1], parts[-1]
        if args.channel and channel != args.channel:
            continue

        meta = read_json(repo / "channels" / channel / thread / "meta.json", {})
        if args.kind and meta.get("kind") != args.kind:
            continue
        if args.status and meta.get("status") != args.status:
            continue
        if args.assignee and meta.get("assignee") != args.assignee:
            continue

        text = p.read_text()
        fm, _ = parse_post(text)
        if args.author and fm.get("author") != args.author:
            continue
        if args.stance and fm.get("stance") != args.stance:
            continue

        # Distinguish where the hit actually came from. The seed-post path is
        # also added when only the thread's title or summary matches, so a
        # body-level snippet would be misleading in that case.
        title = meta.get("title", "")
        summary = meta.get("summary", "")
        if ql in text.lower():
            match = "body"
            snippet = _snippet(text, args.query)
        elif ql in title.lower():
            match = "title"
            snippet = title
        elif ql in summary.lower():
            match = "summary"
            snippet = summary
        else:
            # Path was added but nothing matches anymore (e.g. meta changed
            # after git grep). Skip rather than emit a confusing result.
            continue
        results.append({
            "channel": channel,
            "thread": thread,
            "thread_title": title or thread,
            "thread_kind": meta.get("kind", "discussion"),
            "thread_status": meta.get("status"),
            "post_id": fm.get("id", _stem_id(p)),
            "filename": p.name,
            "author": fm.get("author"),
            "alias": fm.get("alias"),
            "stance": fm.get("stance"),
            "timestamp": fm.get("timestamp"),
            "match": match,
            "snippet": snippet,
        })

    results.sort(key=lambda r: r.get("timestamp") or "", reverse=True)
    if args.limit:
        results = results[: args.limit]
    out(results)


def _snippet_len():
    """List-view body truncation length. Configurable so long threads stay
    scannable while `pp read --message <id>` fetches the full text.
    env PAIR_PRESSURE_SNIPPET_LEN > config snippet_len > 240."""
    raw = os.environ.get("PAIR_PRESSURE_SNIPPET_LEN")
    if raw is None or raw.strip() == "":
        raw = _config_load().get("snippet_len")
    try:
        n = int(raw)
        return n if n > 0 else 240
    except (TypeError, ValueError):
        return 240


def _collect_feed_posts(channels_root, since, channel=None,
                        server=None, repo=None):
    """Build feed-post dicts for one repo/server's channels tree.

    Tags each post with `server`/`repo` when provided (for cross-scope feeds).
    Body is truncated to `_snippet_len()` chars for scanability and wrapped as
    untrusted. Archived channels are skipped (history kept, off the feed).
    """
    if not channels_root.is_dir():
        return []
    slen = _snippet_len()
    if channel:
        ch_dir = channels_root / channel
        # An explicitly-named archived channel still stays off the feed.
        targets = [] if _channel_archived(ch_dir) else [ch_dir]
    else:
        targets = _active_channel_dirs(channels_root)
    posts = []
    for ch_dir in targets:
        if not ch_dir.is_dir():
            continue
        ch_name = ch_dir.name
        for thread_dir in sorted(p for p in ch_dir.iterdir() if p.is_dir()):
            meta = read_json(thread_dir / "meta.json", {})
            title = meta.get("title", thread_dir.name)
            for post_file in _post_files(thread_dir):
                fm, body = parse_post(post_file.read_text())
                ts = fm.get("timestamp") or ""
                if since and ts < since:
                    continue
                snippet = body.strip()
                if len(snippet) > slen:
                    snippet = snippet[:slen].rstrip() + "..."
                row = {
                    "channel": ch_name,
                    "thread": thread_dir.name,
                    "thread_title": title,
                    "thread_kind": meta.get("kind", "discussion"),
                    "thread_status": meta.get("status"),
                    "id": fm.get("id", _stem_id(post_file)),
                    "author": fm.get("author"),
                    "alias": fm.get("alias"),
                    "via": fm.get("via"),
                    "stance": fm.get("stance"),
                    "timestamp": ts,
                    "body": _wrap_untrusted(snippet, fm.get("author")),
                    "filename": post_file.name,
                }
                if server is not None:
                    row["server"] = server
                if repo is not None:
                    row["repo"] = repo
                posts.append(row)
    return posts


def _materialize_server_wt(srv, no_pull):
    """Resolve the worktree for `srv` on the active repo, pinning _CURRENT_REPO
    and pulling (unless no_pull). Materializes when online; returns None when
    offline and the worktree isn't local yet (never blocks on the network)."""
    global _CURRENT_REPO
    main = _main_repo_path()
    wt = main / ".pp-worktrees" / srv
    if not (wt.exists() and (wt / ".git").exists()):
        if _offline():
            return None
        try:
            wt = worktree_path(srv)
        except SystemExit:
            return None
    _CURRENT_REPO = wt
    if not no_pull:
        maybe_pull()
    return wt


def _iter_server_worktrees(no_pull):
    """Yield (server_name, worktree_path) for every registered server of the
    currently-active repo, materializing worktrees when online. Skips servers
    with no local worktree in offline mode (never blocks on the network)."""
    global _CURRENT_REPO
    try:
        servers = [s.get("name") for s in _registry_load().get("servers", [])]
    except SystemExit:
        servers = []
    for srv in servers:
        if not srv:
            continue
        wt = _materialize_server_wt(srv, no_pull)
        if wt is not None:
            yield srv, wt
    _CURRENT_REPO = None


def _repo_scopes(all_repos):
    """[(repo_name, repo_root)] to iterate over: every registered repo when
    `all_repos` and a registry exists, else just the active repo (name None).
    Shared by the cross-scope feed/unread verbs."""
    if all_repos and _repos_list():
        return [(r.get("name"), Path(r["path"]).expanduser())
                for r in _repos_list() if r.get("path")]
    return [(None, _main_repo_path())]


def cmd_feed(args):
    """Cross-thread feed view: posts ordered ASCENDING by timestamp.

    Replaces the need for the user to read each thread separately to catch
    up. Returns at most --limit posts, with the most recent at the END of
    the list (chronological / first-pushed first, matching real chat scroll
    direction).

    --channel narrows to a single channel; --since trims to posts at or
    after the given ISO timestamp. --all-servers spans every server on the
    active repo; --all-repos spans every registered repo (implies
    --all-servers). Body is truncated to 240 chars per post.
    """
    _watch_ack()  # reading clears the unread badge (additive, best-effort)
    all_repos = getattr(args, "all_repos", False)
    all_servers = getattr(args, "all_servers", False) or all_repos

    posts = []
    if not all_servers:
        _activate_server(args)
        if not args.no_pull:
            maybe_pull()
        posts = _collect_feed_posts(repo_path() / "channels", args.since,
                                    channel=args.channel)
    else:
        global _REPO_OVERRIDE, _CURRENT_REPO
        saved_override = _REPO_OVERRIDE
        try:
            for repo_name, repo_root in _repo_scopes(all_repos):
                _REPO_OVERRIDE = repo_root
                _CURRENT_REPO = None
                try:
                    for srv, wt in _iter_server_worktrees(args.no_pull):
                        posts.extend(_collect_feed_posts(
                            wt / "channels", args.since, channel=args.channel,
                            server=srv, repo=repo_name))
                except SystemExit:
                    continue  # skip an unreadable repo, never abort the feed
        finally:
            _REPO_OVERRIDE = saved_override
            _CURRENT_REPO = None

    # Ascending by timestamp (oldest first). For ties (same second), fall
    # back to (repo, server, channel, thread, ordinal) for determinism.
    posts.sort(key=lambda p: (
        p.get("timestamp") or "",
        p.get("repo") or "",
        p.get("server") or "",
        p.get("channel") or "",
        p.get("thread") or "",
        p.get("id") or "",
    ))
    if args.limit:
        # Keep the LAST `limit` (newest), but preserve chronological order
        # so the consumer reads oldest-at-top.
        posts = posts[-args.limit:]
    out(posts)


def cmd_unread(args):
    """New posts not authored by you, across servers (and repos).

    Default mode reuses the watcher's baseline (watch-state.json), so it
    reports posts newer than the last time the daemon -- or a prior `pp
    unread` -- saw each thread, WITHOUT clearing the badge (the in-memory
    markers are advanced but never persisted). `--since <ISO>` instead counts
    every post at/after a timestamp (useful for MCP clients that never run the
    daemon). `--all` spans all servers of the active repo; `--all-repos` spans
    every registered repo.
    """
    me = os.environ.get("PAIR_PRESSURE_AUTHOR")
    all_repos = getattr(args, "all_repos", False)
    all_servers = getattr(args, "all", False) or all_repos
    items = []

    global _REPO_OVERRIDE, _CURRENT_REPO
    saved_override = _REPO_OVERRIDE
    # Baseline markers (default mode); a fresh dict each call -- we never save
    # it, so advancing markers here is non-destructive to the daemon's badge.
    base_state = None if args.since else _watch_state_load()
    try:
        for repo_name, repo_root in _repo_scopes(all_repos):
            _REPO_OVERRIDE = repo_root
            _CURRENT_REPO = None
            try:
                if all_servers:
                    servers = [s.get("name")
                               for s in _registry_load().get("servers", [])]
                else:
                    servers = [_server_arg(args)]
            except SystemExit:
                continue
            # Per-repo copy of the baseline so two repos sharing a
            # server/channel/thread name don't suppress each other's posts via
            # a shared marker key (which is not repo-namespaced).
            repo_state = None if args.since else dict(base_state)
            for srv in servers:
                if not srv:
                    continue
                try:
                    if args.since:
                        wt = _materialize_server_wt(srv, args.no_pull)
                        if wt is None:
                            continue
                        for p in _collect_feed_posts(wt / "channels",
                                                     args.since, server=srv,
                                                     repo=repo_name):
                            au = p.get("author")
                            if au and au != me:
                                items.append({
                                    "repo": repo_name, "server": srv,
                                    "channel": p["channel"],
                                    "thread": p["thread"],
                                    "post_id": p["id"], "author": au})
                        _CURRENT_REPO = None
                    else:
                        for n in _scan_server_new(srv, repo_state):
                            row = dict(n)
                            row["repo"] = repo_name
                            items.append(row)
                except SystemExit:
                    continue
                except Exception:
                    continue
    finally:
        _REPO_OVERRIDE = saved_override
        _CURRENT_REPO = None

    res = {"count": len(items), "items": items}
    try:
        buckets = read_json(_watch_unread_path(), {})
        if buckets:
            res["buckets"] = buckets
    except Exception:
        pass
    out(res)


def cmd_aliases_in_use(args):
    """Report aliases that have posted recently on this server.

    Used by `/pp-chat:alias <name>` to detect "another live session is already
    using this nickname" — there's no central session registry, so we proxy
    activity by scanning posts within the last N minutes (default 30). An
    alias counts as "in use" if any AI-composed post (via != human) carrying
    that alias appears in the window.

    Output:
        [
          {"alias": "Echo",  "author": "alice", "last_seen": "...", "last_channel": "general",
           "last_thread": "2026-05-12_oauth-race", "post_count": 3},
          ...
        ]
    """
    _activate_server(args)
    if not args.no_pull:
        maybe_pull()
    channels_root = repo_path() / "channels"
    if not channels_root.is_dir():
        out([])
        return

    cutoff = datetime.now(timezone.utc).timestamp() - (args.since_minutes * 60)
    seen = {}  # alias -> {alias, author, last_seen, last_channel, last_thread, post_count}
    for ch_dir in channels_root.iterdir():
        if not ch_dir.is_dir():
            continue
        for tdir in ch_dir.iterdir():
            if not tdir.is_dir():
                continue
            for p in _post_files(tdir):
                try:
                    fm, _ = parse_post(p.read_text())
                except OSError:
                    continue
                al = fm.get("alias")
                if not al:
                    continue
                # The mtime is a reliable activity signal even if the
                # post's frontmatter timestamp is older (e.g. amended
                # commits, history rewrites).
                try:
                    ts = p.stat().st_mtime
                except OSError:
                    continue
                if ts < cutoff:
                    continue
                iso = datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                hit = seen.setdefault(al, {
                    "alias": al,
                    "author": fm.get("author"),
                    "last_seen": iso,
                    "last_channel": ch_dir.name,
                    "last_thread": tdir.name,
                    "post_count": 0,
                })
                hit["post_count"] += 1
                if iso > hit["last_seen"]:
                    hit["last_seen"] = iso
                    hit["last_channel"] = ch_dir.name
                    hit["last_thread"] = tdir.name
                    hit["author"] = fm.get("author")
    out(sorted(seen.values(), key=lambda r: r["last_seen"], reverse=True))


_CHANNEL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def cmd_channel_ensure(args):
    """Idempotently create a channel if it doesn't exist.

    Used by the default-fallback path in /pp-chat:send and /pp-chat:read so a
    message can land in a sensible place even on a freshly-cloned server with
    no channels beyond what server-new scaffolded. Cheap no-op when the
    channel already exists.
    """
    _activate_server(args)
    maybe_pull()
    name = args.name
    if not _CHANNEL_NAME_RE.match(name):
        die(f"channel name must match {_CHANNEL_NAME_RE.pattern}")

    ch = repo_path() / "channels" / name
    if ch.is_dir():
        existing = read_json(ch / "channel.json", {"name": name, "description": ""})
        if existing.get("archived"):
            # ensure must yield a USABLE channel -- revive an archived one.
            def unarchive_payload():
                meta = read_json(ch / "channel.json",
                                 {"name": name, "description": ""})
                meta.pop("archived", None)
                write_json(ch / "channel.json", meta)
                return {"ok": True, "created": False, "unarchived": True,
                        "channel": meta.get("name", name)}

            def unarchive_msg(info):
                return f"{name}: unarchive-channel by " \
                       f"{by_for_via('claude-code', args)}"

            out(push_with_retry(unarchive_payload, unarchive_msg))
            return
        out({"ok": True, "created": False, "channel": existing.get("name", name)})
        return

    def write_payload():
        # Re-check inside the retry: a parallel agent might have created the
        # channel during our rebase. If so, treat as success.
        if ch.is_dir():
            return {"ok": True, "created": False, "channel": name}
        ch.mkdir(parents=True)
        write_json(ch / "channel.json", {
            "name": name,
            "description": args.description or "",
        })
        return {"ok": True, "created": True, "channel": name}

    def msg(info):
        return f"{name}: ensure-channel by {by_for_via('claude-code', args)}"

    out(push_with_retry(write_payload, msg))


def _set_channel_archived(args, archived):
    """Flip channel.json `archived` for an existing channel and push.
    Archiving hides a channel from list/feed/read/watch while keeping every
    post; unarchiving restores it. Idempotent (a no-op write yields no
    commit)."""
    _activate_server(args)
    maybe_pull()
    name = args.name
    ch = repo_path() / "channels" / name
    if not ch.is_dir():
        die(f"channel '{name}' does not exist")

    def write_payload():
        meta = read_json(ch / "channel.json", {"name": name, "description": ""})
        if archived:
            meta["archived"] = True
        else:
            meta.pop("archived", None)
        write_json(ch / "channel.json", meta)
        return {"ok": True, "channel": meta.get("name", name),
                "archived": bool(archived)}

    verb = "archive" if archived else "unarchive"

    def msg(info):
        return f"{name}: {verb}-channel by {by_for_via('claude-code', args)}"

    out(push_with_retry(write_payload, msg))


def cmd_channel_archive(args):
    _set_channel_archived(args, True)


def cmd_channel_unarchive(args):
    _set_channel_archived(args, False)


def _valid_server_name(name):
    return bool(re.match(r"^[a-z0-9][a-z0-9._-]{0,63}$", name))


def cmd_servers(args):
    """List servers in the registry, cross-checked against remote branches.

    Reports for each server whether it has a local worktree materialized
    and whether the branch is on the remote. Surfaces orphan branches
    (on remote but absent from the registry).
    """
    main = _main_repo_path()
    git("fetch", "origin", "main", cwd=main, check=False)
    git("fetch", "origin", "--prune", cwd=main, check=False)
    git("pull", "--rebase", "--autostash", cwd=main, check=False)

    reg = _registry_load()
    res = git("branch", "-r", "--list", f"origin/{SERVER_BRANCH_PREFIX}*",
              cwd=main, check=False)
    remote_servers = set()
    prefix = f"origin/{SERVER_BRANCH_PREFIX}"
    for line in res.stdout.splitlines():
        line = line.strip()
        if line.startswith(prefix):
            remote_servers.add(line[len(prefix):])

    wt_root = main / ".pp-worktrees"
    local_worktrees = (
        {p.name for p in wt_root.iterdir() if p.is_dir()}
        if wt_root.exists() else set()
    )

    rows = []
    in_registry = set()
    for s in reg.get("servers", []):
        name = s["name"]
        in_registry.add(name)
        rows.append({
            "name": name,
            "description": s.get("description", ""),
            "created_by": s.get("created_by", ""),
            "created_at": s.get("created_at", ""),
            "channels": s.get("channels", []),
            "on_remote": name in remote_servers,
            "local_worktree": name in local_worktrees,
        })
    for r in sorted(remote_servers - in_registry):
        rows.append({
            "name": r,
            "orphan_branch": True,
            "on_remote": True,
            "local_worktree": r in local_worktrees,
        })
    out({
        "servers": rows,
        "active": os.environ.get("PAIR_PRESSURE_SERVER"),
    })


def cmd_server_new(args):
    """Create a server: branch off main + worktree + channels + registry append."""
    name = args.name
    if not _valid_server_name(name):
        die("server name must match ^[a-z0-9][a-z0-9._-]{0,63}$")

    main = _main_repo_path()
    branch = _server_branch(name)

    git("fetch", "origin", "main", cwd=main, check=False)
    git("pull", "--rebase", "--autostash", cwd=main, check=False)

    reg = _registry_load()
    if any(s.get("name") == name for s in reg.get("servers", [])):
        die(f"server '{name}' already in registry")
    if _origin_branch_exists(branch, cwd=main):
        die(f"branch {branch} already exists on remote -- "
            "either someone else just created it (try `pp servers`) "
            "or it's an orphan; resolve manually")

    wt = main / ".pp-worktrees" / name
    git("worktree", "add", "-b", branch, str(wt), "main", cwd=main)

    # The registry lives ONLY on main -- strip it from server branches so it
    # can't drift between branches.
    pp_dir = wt / ".pair-pressure"
    if pp_dir.exists():
        shutil.rmtree(pp_dir)

    channels = [c.strip() for c in (args.channels or "general").split(",") if c.strip()]
    if not channels:
        channels = ["general"]
    for ch in channels:
        chdir = wt / "channels" / ch
        chdir.mkdir(parents=True, exist_ok=True)
        write_json(chdir / "channel.json", {"name": ch, "description": ""})

    git("add", "-A", cwd=wt)
    if git("status", "--porcelain", cwd=wt, check=False).stdout.strip():
        git("commit", "-m", f"init server {name}", cwd=wt)
    if has_remote():
        push = git("push", "-u", "origin", branch, cwd=wt, check=False)
        if push.returncode != 0:
            die(f"failed to push {branch}: {push.stderr.strip()}")

    # Update registry on main, with rebase-retry if someone raced us.
    global _CURRENT_REPO
    _CURRENT_REPO = main

    def write_payload():
        cur = _registry_load()
        if not any(s.get("name") == name for s in cur.get("servers", [])):
            cur.setdefault("servers", []).append({
                "name": name,
                "description": args.description or "",
                "created_at": now_iso(),
                "created_by": author(),
                "channels": channels,
            })
            _registry_save(cur)
        return {
            "ok": True,
            "name": name,
            "branch": branch,
            "worktree": str(wt),
            "channels": channels,
        }

    def msg(info):
        return f"register server {name}"

    out(push_with_retry(write_payload, msg))


def cmd_server_switch(args):
    """Validate target server and lazy-materialize its worktree.

    Prints both POSIX and PowerShell export lines for CLI ergonomics.
    Slash commands read the JSON and update conversation context instead.
    """
    name = args.name
    reg = _registry_load()
    in_registry = any(s.get("name") == name for s in reg.get("servers", []))
    if not in_registry:
        # Allow switching to a remote-only orphan branch (real branch, not in
        # registry yet) -- still useful. But typos must fail cleanly.
        main = _main_repo_path()
        git("fetch", "origin", _server_branch(name), cwd=main, check=False)
        if not _origin_branch_exists(_server_branch(name), cwd=main):
            die(f"server '{name}' not in registry (try `pp servers`)")
    worktree_path(name)
    out({
        "ok": True,
        "active_server": name,
        "shell_export": f"export PAIR_PRESSURE_SERVER={name}",
        "powershell": f"$env:PAIR_PRESSURE_SERVER = '{name}'",
        "hint": "CLI: eval the shell_export line. Claude Code slash command: "
                "remember this in conversation context for subsequent /pp-chat:* calls.",
    })


def cmd_server_remove(args):
    """Delete worktree + local + remote branch + registry entry. --yes required."""
    name = args.name
    if not args.yes:
        die("refusing to remove without --yes (this deletes the branch and worktree)")
    main = _main_repo_path()
    branch = _server_branch(name)
    wt = main / ".pp-worktrees" / name
    if wt.exists():
        git("worktree", "remove", "--force", str(wt), cwd=main, check=False)
    git("branch", "-D", branch, cwd=main, check=False)
    if has_remote():
        git("push", "origin", "--delete", branch, cwd=main, check=False)

    global _CURRENT_REPO
    _CURRENT_REPO = main

    def write_payload():
        cur = _registry_load()
        cur["servers"] = [s for s in cur.get("servers", []) if s.get("name") != name]
        _registry_save(cur)
        return {"ok": True, "removed": name}

    def msg(info):
        return f"unregister server {name}"

    out(push_with_retry(write_payload, msg))


# ---- repo management (multiple chat repos, switchable per session) ----

def _default_repo_clone_dir(name):
    return _PP_HOME / "repos" / name


def _is_chat_repo(path):
    """A path is an initialized chat repo if it has the registry markers."""
    pp = Path(path) / ".pair-pressure"
    return (pp / "servers.json").exists() or (pp / "schema-version").exists()


def _ensure_git_identity(cwd):
    """Give a freshly-cloned chat repo a local commit identity when none is
    configured (global or local). pair-pressure carries real identity in each
    post's `by:` field, so the git author is incidental -- but `git commit`
    still refuses without one. Never overrides an existing identity."""
    have = git("config", "user.email", cwd=cwd, check=False)
    if have.returncode == 0 and have.stdout.strip():
        return
    au = author()
    git("config", "user.name", au, cwd=cwd, check=False)
    git("config", "user.email", f"{au}@pair-pressure.local", cwd=cwd, check=False)


def _pp_init_argv():
    """argv prefix that runs pp-init. Prefer the console script on PATH (the
    robust path for wheel installs); fall back to the bundled script next to
    this file (_data/scripts/pp-init.py)."""
    if shutil.which("pp-init"):
        return ["pp-init"]
    bundled = Path(__file__).resolve().parent.parent.parent / "scripts" / "pp-init.py"
    return [sys.executable, str(bundled)]


def _pp_self_argv():
    """argv prefix that re-invokes THIS pp.py. Always works regardless of
    whether the `pp` console script is on PATH (it may not be when pp is run
    as `python pp.py` or via an absolute path)."""
    return [sys.executable, str(Path(__file__).resolve())]


def cmd_repo_add(args):
    """Register a chat repo: clone (or adopt --path) + optional init + record.

    `pp repo add <name> <url> [--path DIR] [--no-clone] [--with-server NAME]
    [--channels a,b]`

    A "repo" is a clone with its own remote; it hosts many servers. The clone
    lands in ~/.pair-pressure/repos/<name> by default. If the remote is an
    uninitialized repo, it is bootstrapped with `pp-init` and main is pushed.
    """
    name = args.name
    if not _valid_server_name(name):
        die("repo name must match ^[a-z0-9][a-z0-9._-]{0,63}$")
    if _repo_entry(name):
        die(f"repo '{name}' is already registered (try `pp repo list`)")

    dest = Path(args.path).expanduser() if args.path else _default_repo_clone_dir(name)
    adopt = bool(args.path) or args.no_clone

    if adopt:
        if not (dest / ".git").exists():
            die(f"--path {dest} is not a git repository")
    else:
        if _offline():
            die("cannot clone in offline mode; run `pp offline false` first, "
                "or register an existing clone with `pp repo add <name> "
                "<url> --path <dir> --no-clone`.")
        if dest.exists() and any(dest.iterdir()):
            die(f"clone target {dest} already exists and is not empty")
        dest.parent.mkdir(parents=True, exist_ok=True)
        res = git("clone", args.url, str(dest), cwd=dest.parent, check=False)
        if res.returncode != 0:
            # Leave no partial clone behind, and do NOT touch the registry.
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            die(f"git clone failed: {res.stderr.strip() or res.stdout.strip()}")

    # Bootstrap the chat structure if the remote was empty / not a chat repo.
    if not _is_chat_repo(dest):
        # We're about to commit (pp-init), so the clone needs a git identity.
        _ensure_git_identity(dest)
        argv = _pp_init_argv() + [str(dest), "--force"]
        if args.with_server:
            argv += ["--with-server", args.with_server]
            if args.channels:
                argv += ["--channels", args.channels]
        sub_env = os.environ.copy()
        sub_env["PAIR_PRESSURE_REPO"] = str(dest)
        sub_env.setdefault("PAIR_PRESSURE_AUTHOR", author())
        ir = subprocess.run(argv, env=sub_env, capture_output=True, text=True)
        if ir.returncode != 0:
            die(f"pp-init failed: {ir.stderr.strip() or ir.stdout.strip()}")
        if not _offline():
            git("push", "-u", "origin", "main", cwd=dest, check=False)
    elif args.with_server and not _origin_branch_exists(
            _server_branch(args.with_server), cwd=dest):
        # Existing chat repo, but the requested server doesn't exist yet:
        # create it via `pp server new` against this repo (a commit, so ensure
        # identity). Re-invoke THIS pp.py so it works off-PATH, and surface a
        # failure instead of silently registering a serverless repo.
        _ensure_git_identity(dest)
        sub_env = os.environ.copy()
        sub_env["PAIR_PRESSURE_REPO"] = str(dest)
        sub_env.setdefault("PAIR_PRESSURE_AUTHOR", author())
        sv = _pp_self_argv() + ["server", "new", args.with_server]
        if args.channels:
            sv += ["--channels", args.channels]
        sv_res = subprocess.run(sv, env=sub_env, capture_output=True, text=True)
        if sv_res.returncode != 0:
            die(f"server '{args.with_server}' could not be created (repo not "
                f"registered): {sv_res.stderr.strip() or sv_res.stdout.strip()}")

    reg = _repos_load()
    reg.setdefault("repos", []).append({
        "name": name,
        "path": str(dest.resolve()),
        "remote": args.url,
        "added_at": now_iso(),
        "added_by": author(),
    })
    reg.setdefault("schema_version", 1)
    _repos_save(reg)
    out({"ok": True, "name": name, "path": str(dest.resolve()),
         "remote": args.url})


def cmd_repo_list(args):
    """List registered chat repos + which one is active for this session."""
    active = _session_repo_name()
    if not active and os.environ.get("PAIR_PRESSURE_REPO"):
        active = "(PAIR_PRESSURE_REPO env)"
    rows = []
    for r in _repos_list():
        path = r.get("path")
        server_count = None
        try:
            reg = Path(path) / ".pair-pressure" / "servers.json"
            if reg.exists():
                data = json.loads(reg.read_text(encoding="utf-8-sig"))
                server_count = len(data.get("servers", []))
        except (OSError, json.JSONDecodeError, TypeError):
            server_count = None
        rows.append({
            "name": r.get("name"),
            "path": path,
            "remote": r.get("remote"),
            "servers": server_count,
            "active": r.get("name") == active,
        })
    out({"repos": rows, "active": active})


def cmd_repo_use(args):
    """Pin this session to a registered repo.

    Writes ONLY the per-session state file (never the shared global state),
    and clears the active server/channel/thread -- those are repo-scoped, so a
    server name from the previous repo is meaningless in the new one.

    Like `pp server switch`, does NOT mutate process env; it returns export
    hints for plain CLI shells. The per-session pin requires
    PAIR_PRESSURE_SESSION_ID to be set (otherwise eval the shell_export line).
    """
    name = args.name
    entry = _repo_entry(name)
    if not entry:
        die(f"repo '{name}' is not registered (try `pp repo list`)")
    path = Path(entry["path"]).expanduser()
    _validated_repo(path)
    _state_save(repo=name, server=None, channel=None, thread_id=None,
                source="repo_use", session_only=True)
    sticky = _session_id() is not None
    out({
        "ok": True,
        "active_repo": name,
        "path": str(path),
        "sticky": sticky,
        "shell_export": f"export PAIR_PRESSURE_REPO={path}",
        "powershell": f"$env:PAIR_PRESSURE_REPO = '{path}'",
        "hint": ("Pinned for this session." if sticky else
                 "No PAIR_PRESSURE_SESSION_ID -- eval the shell_export line, "
                 "or pass --repo per call. Claude Code slash command: remember "
                 "this in conversation context for subsequent /pp-chat:* calls."),
    })


def cmd_repo_remove(args):
    """Unregister a repo. --yes required. --delete-clone also removes the
    on-disk clone, but ONLY when it lives under ~/.pair-pressure/repos/."""
    name = args.name
    entry = _repo_entry(name)
    if not entry:
        die(f"repo '{name}' is not registered")
    if not args.yes:
        die("refusing to remove without --yes")
    if args.delete_clone:
        raw = entry.get("path") or ""
        if not raw:
            die("refusing --delete-clone: registry entry has no path")
        path = Path(raw).resolve()
        repos_root = (_PP_HOME / "repos").resolve()
        if repos_root in path.parents:
            shutil.rmtree(path, ignore_errors=True)
        else:
            die(f"refusing --delete-clone: {path} is outside "
                f"{repos_root} (remove it manually)")
    reg = _repos_load()
    reg["repos"] = [r for r in reg.get("repos", []) if r.get("name") != name]
    _repos_save(reg)
    # If this session was pinned to the removed repo, unpin it.
    if _session_repo_name() == name:
        _state_save(source="repo_remove", session_only=True, clear_repo=True)
    out({"ok": True, "removed": name})


# ---- watcher daemon (zero-token background new-message notifier) ----

def _watch_pid_path():       return _PP_HOME / "watch.pid"
def _watch_lock_path():      return _PP_HOME / "watch.lock"
def _watch_state_path():     return _PP_HOME / "watch-state.json"
def _watch_log_path():       return _PP_HOME / "watch.log"
def _watch_notify_path():    return _PP_HOME / "watch-last-notify.json"
def _watch_unread_path():    return _PP_HOME / "unread.json"

_WATCH_LOG_CAP = 256 * 1024


_SHARED_BUCKET = "__shared__"


def _watch_unread_key():
    """Bucket key for the CURRENT session: PAIR_PRESSURE_SESSION_ID if set,
    else `__shared__`. Lets two Claude Code instances have independent
    inboxes when each sets its own session id; single-instance users keep
    the historical shared behavior with no opt-in."""
    return _session_id() or _SHARED_BUCKET


def _watch_unread_load_all():
    """Load the full bucket dict. Auto-migrates the v0.8.1-initial flat
    shape ({count,latest,updated_at}) into {__shared__: {...}} so old
    state still works without manual reset."""
    p = _watch_unread_path()
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(d, dict):
        return {}
    # Legacy flat shape -> wrap in __shared__.
    if "count" in d and _SHARED_BUCKET not in d:
        return {_SHARED_BUCKET: {
            "count": int(d.get("count", 0) or 0),
            "latest": d.get("latest"),
            "updated_at": d.get("updated_at"),
        }}
    return d


def _watch_unread_save_all(buckets):
    try:
        _PP_HOME.mkdir(parents=True, exist_ok=True)
        _watch_unread_path().write_text(json.dumps(buckets, indent=2),
                                        encoding="utf-8")
    except OSError:
        pass


def _watch_unread_load(key=None):
    """Bucket for `key` (default = current session). Empty default."""
    key = key or _watch_unread_key()
    return _watch_unread_load_all().get(key) or {}


def _watch_unread_bump(fresh):
    """Increment EVERY existing bucket (each session sees the news once).
    First-time daemons with no buckets seed __shared__. Best-effort."""
    if not fresh:
        return
    try:
        buckets = _watch_unread_load_all()
        if not buckets:
            buckets = {_SHARED_BUCKET: {"count": 0, "latest": None,
                                        "updated_at": None}}
        last = fresh[-1]
        latest = {"author": last["author"], "channel": last["channel"],
                  "thread": last["thread"], "at": now_iso()}
        now = now_iso()
        for k, b in list(buckets.items()):
            if not isinstance(b, dict):
                b = {}
            buckets[k] = {
                "count": int(b.get("count", 0) or 0) + len(fresh),
                "latest": latest,
                "updated_at": now,
            }
        _watch_unread_save_all(buckets)
    except (OSError, KeyError, TypeError):
        pass


def _watch_ack(key=None):
    """Clear ONE bucket (current session by default). Best-effort. Other
    sessions' counters are untouched -- reading in instance A does not
    clear instance B's badge when each has its own session id."""
    key = key or _watch_unread_key()
    try:
        buckets = _watch_unread_load_all()
        buckets[key] = {"count": 0, "latest": None, "updated_at": now_iso()}
        _watch_unread_save_all(buckets)
    except OSError:
        pass


_WATCH_INTERVAL_MIN = 5
_WATCH_INTERVAL_DEFAULT = 300  # 5 minutes


def _parse_interval(v):
    """Accept '90', '90s', '5m', '5min', '1h' -> seconds int, or None."""
    if v is None:
        return None
    s = str(v).strip().lower()
    if not s:
        return None
    m = re.fullmatch(r"(\d+)\s*(s|sec|secs|m|min|mins|h|hr|hrs)?", s)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2) or "s"
    mult = 3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1
    return n * mult


def _resolve_interval():
    """Poll interval in seconds. Precedence (mirrors _offline): env
    PAIR_PRESSURE_WATCH_INTERVAL > config watch.interval > default 300.
    Clamped to >= 5s."""
    source = "default"
    secs = _WATCH_INTERVAL_DEFAULT
    cfg = _config_load().get("watch")
    if isinstance(cfg, dict) and _parse_interval(cfg.get("interval")) is not None:
        secs, source = _parse_interval(cfg.get("interval")), "config"
    ev = _parse_interval(os.environ.get("PAIR_PRESSURE_WATCH_INTERVAL"))
    if ev is not None:
        secs, source = ev, "env"
    return max(_WATCH_INTERVAL_MIN, secs), source


def _watch_log(line):
    try:
        _PP_HOME.mkdir(parents=True, exist_ok=True)
        lp = _watch_log_path()
        if lp.exists() and lp.stat().st_size > _WATCH_LOG_CAP:
            tail = lp.read_text(encoding="utf-8", errors="replace")[-_WATCH_LOG_CAP // 2:]
            lp.write_text(tail, encoding="utf-8")
        with lp.open("a", encoding="utf-8") as fh:
            fh.write(f"{now_iso()} {line}\n")
    except OSError:
        pass


def _pid_alive(pid):
    """True iff `pid` is a running process. Windows-safe: never uses
    os.kill(pid,0) (which TerminateProcess-es on Windows)."""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            k = ctypes.windll.kernel32
            h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not h:
                return False
            try:
                code = wintypes.DWORD()
                if not k.GetExitCodeProcess(h, ctypes.byref(code)):
                    return False
                return code.value == 259  # STILL_ACTIVE
            finally:
                k.CloseHandle(h)
        except Exception:
            try:
                r = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
                    capture_output=True, text=True, timeout=10)
                return str(pid) in r.stdout
            except Exception:
                return False
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _read_watch_pid():
    try:
        d = json.loads(_watch_pid_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _watch_running():
    d = _read_watch_pid()
    if not d:
        return None
    return d if _pid_alive(d.get("pid")) else None


def _watch_interp():
    """pythonw.exe (no console flash) if present, else the current python."""
    exe = Path(sys.executable)
    if os.name == "nt":
        cand = exe.with_name("pythonw.exe")
        if cand.exists():
            return str(cand)
    return str(exe)


def _spawn_watch_daemon():
    _PP_HOME.mkdir(parents=True, exist_ok=True)
    script = str(Path(__file__).resolve())
    cmd = [_watch_interp(), script, "_watch-daemon"]
    flags = 0
    for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
        flags |= getattr(subprocess, name, 0)
    env = dict(os.environ)
    env["PAIR_PRESSURE_IS_WATCH_DAEMON"] = "1"
    logf = open(_watch_log_path(), "a", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=logf, stderr=logf,
            close_fds=True, creationflags=flags, env=env,
            cwd=str(_PP_HOME))
    finally:
        logf.close()
    try:
        _watch_pid_path().write_text(json.dumps({
            "pid": proc.pid, "started_at": now_iso(),
            "python": _watch_interp(),
        }), encoding="utf-8")
    except OSError:
        pass
    _watch_log(f"daemon spawned pid={proc.pid}")
    return proc.pid


def _watcher_repo_available():
    """True when a chat repo resolves for the watcher to scan, without ever
    dying. Covers env-var installs and the registry (session-pinned or sole)."""
    if os.environ.get("PAIR_PRESSURE_REPO"):
        return True
    name = _session_repo_name()
    if name and _repo_entry(name):
        return True
    return len(_repos_list()) == 1


def _ensure_watcher(args):
    """Auto-start hook. Called once per `pp` invocation. Hot path is two tiny
    file reads + one liveness check -- no git, no network, no subprocess.
    Wrapped by the caller so a watcher bug can never break a normal `pp`."""
    cmd = getattr(args, "cmd", None)
    if cmd in ("_watch-daemon", "watch", "offline"):
        return
    if os.environ.get("PAIR_PRESSURE_IS_WATCH_DAEMON") == "1":
        return
    if not _watcher_repo_available():
        return
    cfg = _config_load()
    wcfg = cfg.get("watch") if isinstance(cfg.get("watch"), dict) else {}
    if wcfg.get("enabled", True) is False:
        return
    if _watch_running():
        return
    lock = _watch_lock_path()
    try:
        if lock.exists() and (time.time() - lock.stat().st_mtime) > 30:
            lock.unlink(missing_ok=True)  # stale lock, ignore
    except OSError:
        pass
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return  # another pp is mid-spawn
    except OSError:
        return
    try:
        if _watch_running():
            return
        _spawn_watch_daemon()
    finally:
        os.close(fd)
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            pass


def _autowire_sentinel():
    return _PP_HOME / "autowire.done"


def _statusline_is_pp(data):
    sl = data.get("statusLine") if isinstance(data, dict) else None
    return isinstance(sl, dict) and "pp-statusline.ps1" in str(
        sl.get("command", ""))


def _wire_statusline_quiet():
    """Non-noisy core of `pp watch wire`: point statusLine at pp-statusline.ps1,
    preserving any prior command in `_pp_prev_statusline` so the wrapper can
    chain to it. Returns True only if it NEWLY wired. Never prints, never
    raises -- auto-wire runs on ordinary `pp` calls, so it must stay invisible
    and never break them."""
    sp = _claude_settings_path()
    try:
        raw = sp.read_text(encoding="utf-8-sig")
        data = json.loads(raw or "{}")
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict) or _statusline_is_pp(data):
        return False
    sl = data.get("statusLine")
    data["_pp_prev_statusline"] = (
        str(sl["command"]) if isinstance(sl, dict) and sl.get("command")
        else "")
    sl_ps1 = _skill_scripts_dir() / "pp-statusline.ps1"
    data["statusLine"] = {"type": "command", "command": _ps_invoke(sl_ps1)}
    try:
        bak = sp.with_suffix(".json.pp.bak")
        if not bak.exists():
            bak.write_text(raw, encoding="utf-8")
    except OSError:
        pass
    try:
        sp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return False
    return True


def _ensure_wired(args):
    """Auto-wire the 0-token statusline badge on any `pp` call, Claude Code
    only. Idempotent, best-effort, one-shot (a durable sentinel means we
    attempt at most once and respect a prior `wire --undo`). Opt out with
    config watch.autowire=false or env PAIR_PRESSURE_NO_AUTOWIRE. The OS toast
    is the cross-CLI notifier; this badge is the Claude-Code bonus. Wrapped by
    the caller so a wiring bug can never break a normal `pp`."""
    cmd = getattr(args, "cmd", None)
    if cmd in ("_watch-daemon", "watch", "offline"):
        return
    if os.environ.get("PAIR_PRESSURE_IS_WATCH_DAEMON") == "1":
        return
    ev = os.environ.get("PAIR_PRESSURE_NO_AUTOWIRE")
    if ev and ev.strip().lower() in ("1", "true", "yes", "on"):
        return
    cfg = _config_load()
    wcfg = cfg.get("watch") if isinstance(cfg.get("watch"), dict) else {}
    if wcfg.get("autowire", True) is False:
        return
    if not _claude_settings_path().exists():
        return  # not a Claude Code install; the toast covers notifications
    sentinel = _autowire_sentinel()
    if sentinel.exists():
        return  # already attempted once (or deliberately undone)
    newly = _wire_statusline_quiet()
    try:
        _PP_HOME.mkdir(parents=True, exist_ok=True)
        sentinel.touch()
    except OSError:
        pass
    if newly:
        print("(pair-pressure: wired a 0-token statusline badge into "
              "~/.claude/settings.json; restart the session to see it. "
              "Opt out with watch.autowire=false or "
              "PAIR_PRESSURE_NO_AUTOWIRE=1.)", file=sys.stderr)


def _notify(title, message):
    """Native OS notification (Windows toast / macOS osascript / Linux
    notify-send), no third-party install + durable fallback (watch.log line
    + sentinel json). Returns True if a native notification call exited 0."""
    payload = {"at": now_iso(), "title": title, "message": message}
    try:
        _watch_notify_path().write_text(json.dumps(payload, indent=2),
                                        encoding="utf-8")
    except OSError:
        pass
    _watch_log(f"notify: {title} | {message}")
    try:
        if sys.platform == "darwin":
            return _notify_macos(title, message)
        if sys.platform.startswith("linux"):
            return _notify_linux(title, message)
        if os.name == "nt":
            return _notify_windows(title, message)
    except Exception as e:
        _watch_log(f"toast_failed {e!r}")
    return False


def _powershell_exe():
    """Absolute path to powershell.exe (or pwsh). Bare "powershell" is NOT
    resolvable from the detached pythonw daemon, nor when pp runs under a
    foreign AI CLI / Git-Bash whose PATH lacks C:\\Windows\\System32 -- that
    raised FileNotFoundError(2) and silently killed every toast. Resolve PATH
    first, then fall back to the System32 literal which always exists."""
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if exe:
        return exe
    sysroot = os.environ.get("SystemRoot") or r"C:\Windows"
    cand = os.path.join(sysroot, "System32", "WindowsPowerShell",
                        "v1.0", "powershell.exe")
    return cand if os.path.exists(cand) else "powershell"


def _notify_windows(title, message):
    aumid = (r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}"
             r"\WindowsPowerShell\v1.0\powershell.exe")

    def _esc(s):
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    ps = (
        "$ErrorActionPreference='Stop';"
        "[Windows.UI.Notifications.ToastNotificationManager,"
        "Windows.UI.Notifications,ContentType=WindowsRuntime]>$null;"
        "[Windows.UI.Notifications.ToastNotification,"
        "Windows.UI.Notifications,ContentType=WindowsRuntime]>$null;"
        "[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom,"
        "ContentType=WindowsRuntime]>$null;"
        "$t=[Windows.UI.Notifications.ToastNotificationManager]::"
        "GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$x=$t.GetElementsByTagName('text');"
        f"$x.Item(0).AppendChild($t.CreateTextNode('{_esc(title)}'))>$null;"
        f"$x.Item(1).AppendChild($t.CreateTextNode('{_esc(message)}'))>$null;"
        "$n=[Windows.UI.Notifications.ToastNotification]::new($t);"
        "[Windows.UI.Notifications.ToastNotificationManager]::"
        f"CreateToastNotifier('{aumid}').Show($n);"
    )
    try:
        r = subprocess.run(
            [_powershell_exe(), "-NoProfile", "-NonInteractive",
             "-Command", ps],
            capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            _watch_log(f"toast_failed rc={r.returncode} {r.stderr.strip()[:200]}")
            return False
        return True
    except Exception as e:
        _watch_log(f"toast_failed {e!r}")
        return False


def _notify_macos(title, message):
    if shutil.which("osascript") is None:
        _watch_log("toast_failed osascript not found")
        return False

    def _esc(s):
        return s.replace("\\", "\\\\").replace('"', '\\"')

    script = (f'display notification "{_esc(message)}" '
              f'with title "{_esc(title)}"')
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            _watch_log(f"toast_failed rc={r.returncode} {r.stderr.strip()[:200]}")
            return False
        return True
    except Exception as e:
        _watch_log(f"toast_failed {e!r}")
        return False


def _notify_linux(title, message):
    if shutil.which("notify-send") is None:
        _watch_log("toast_failed notify-send not found "
                   "(install libnotify-bin)")
        return False
    try:
        r = subprocess.run(
            ["notify-send", "-a", "pair-pressure", "-u", "normal", "--",
             title, message],
            capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            _watch_log(f"toast_failed rc={r.returncode} {r.stderr.strip()[:200]}")
            return False
        return True
    except Exception as e:
        _watch_log(f"toast_failed {e!r}")
        return False


def _watch_state_load():
    try:
        d = json.loads(_watch_state_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _watch_state_save(state):
    try:
        _PP_HOME.mkdir(parents=True, exist_ok=True)
        _watch_state_path().write_text(json.dumps(state, indent=2),
                                       encoding="utf-8")
    except OSError:
        pass


def _scan_server_new(server, state):
    """Return list of {channel,thread,post_id,author} for posts newer than the
    per-key marker, not authored by us. Advances `state` markers in place.
    Online: fetch + diff origin/<branch> (working tree untouched). Offline:
    scan working-tree files."""
    global _CURRENT_REPO
    me = os.environ.get("PAIR_PRESSURE_AUTHOR")
    main = _main_repo_path()
    wt = main / ".pp-worktrees" / server
    if not (wt.exists() and (wt / ".git").exists()):
        return []  # never materialize from the daemon (would block on net)
    _CURRENT_REPO = wt
    branch = _server_branch(server)
    posts = []  # (channel, thread, post_id, reader)
    ref = None
    if not _offline():
        git("fetch", "origin", branch, check=False)
        if _origin_branch_exists(branch):
            ref = f"origin/{branch}"
    archived = _archived_names(wt)  # archived channels never raise a notify
    if ref:
        r = git("ls-tree", "-r", "--name-only", ref, check=False)
        names = r.stdout.splitlines() if r.returncode == 0 else []
        for path in names:
            m = re.match(r"channels/([^/]+)/([^/]+)/([^/]+)-(seed|reply)\.md$",
                         path)
            if not m:
                continue
            ch, thr, stem = m.group(1), m.group(2), m.group(3)
            if ch in archived:
                continue
            posts.append((ch, thr, stem, ("git", ref, path)))
    else:
        ch_root = wt / "channels"
        if ch_root.is_dir():
            for ch_dir in _active_channel_dirs(ch_root):
                for tdir in ch_dir.iterdir():
                    if not tdir.is_dir():
                        continue
                    for pf in _post_files(tdir):
                        posts.append((ch_dir.name, tdir.name, _stem_id(pf),
                                      ("file", pf)))
    by_key = {}
    for ch, thr, stem, reader in posts:
        by_key.setdefault((ch, thr), []).append((stem, reader))
    new = []
    for (ch, thr), items in by_key.items():
        items.sort(key=lambda x: x[0])
        key = f"{server}/{ch}/{thr}"
        marker = state.get(key)
        cur_max = items[-1][0]
        if marker is None:
            state[key] = cur_max  # baseline on first sight, no backlog flood
            continue
        for stem, reader in items:
            if stem <= marker:
                continue
            try:
                if reader[0] == "git":
                    _, rref, rpath = reader
                    sr = git("show", f"{rref}:{rpath}", check=False)
                    text = sr.stdout if sr.returncode == 0 else ""
                else:
                    text = reader[1].read_text(encoding="utf-8",
                                               errors="replace")
                fm, _ = parse_post(text)
                au = fm.get("author")
            except Exception:
                au = None
            if au and au != me:
                new.append({"server": server, "channel": ch, "thread": thr,
                            "post_id": stem, "author": au})
        state[key] = max(cur_max, marker)
    _CURRENT_REPO = None
    return new


def cmd_watch_daemon(args):
    os.environ["PAIR_PRESSURE_IS_WATCH_DAEMON"] = "1"
    pid_path = _watch_pid_path()

    def _cleanup(*_a):
        try:
            pid_path.unlink(missing_ok=True)
        except OSError:
            pass
        sys.exit(0)

    import atexit
    import signal as _sig
    atexit.register(lambda: pid_path.unlink(missing_ok=True))
    for sn in ("SIGTERM", "SIGBREAK", "SIGINT"):
        s = getattr(_sig, sn, None)
        if s is not None:
            try:
                _sig.signal(s, _cleanup)
            except (ValueError, OSError):
                pass
    interval, _isrc = _resolve_interval()
    _watch_log(f"daemon loop start interval={interval}s offline={_offline()}")
    while True:
        try:
            cfg = _config_load()
            wcfg = cfg.get("watch") if isinstance(cfg.get("watch"), dict) else {}
            # Re-resolve each tick so `pp watch interval` takes effect live.
            interval, _ = _resolve_interval()
            if wcfg.get("enabled", True) is False:
                _watch_log("watch disabled in config; exiting")
                _cleanup()
            state = _watch_state_load()
            fresh = []
            try:
                servers = [s.get("name")
                           for s in _registry_load().get("servers", [])]
            except SystemExit:
                servers = []
            for srv in servers:
                if not srv:
                    continue
                try:
                    fresh.extend(_scan_server_new(srv, state))
                except Exception as e:
                    _watch_log(f"scan error server={srv}: {e!r}")
            _watch_state_save(state)
            if fresh:
                n = len(fresh)
                last = fresh[-1]
                where = f"#{last['channel']}"
                if n == 1:
                    title = f"pair-pressure: new message in {where}"
                    msg = f"{last['author']} posted in {last['thread']}"
                else:
                    title = f"pair-pressure: {n} new messages"
                    msg = (f"latest: {last['author']} in {where} "
                           f"({last['thread']})")
                _notify(title, msg)
                _watch_unread_bump(fresh)
        except Exception as e:
            _watch_log(f"loop error: {e!r}")
        time.sleep(interval)


def _skill_scripts_dir():
    return Path(__file__).resolve().parent


def _claude_settings_path():
    return Path.home() / ".claude" / "settings.json"


def _other_recent_sessions(window_seconds=3600):
    """Session ids that touched pp in the last `window_seconds`, excluding
    the current session. Used to warn after `pp watch wire` that other
    Claude Code instances need to restart for the statusLine change."""
    sessions_dir = _PP_HOME / "sessions"
    if not sessions_dir.is_dir():
        return []
    me = _session_id()
    cutoff = time.time() - window_seconds
    found = []
    for p in sessions_dir.iterdir():
        if not p.is_file() or p.suffix != ".json":
            continue
        sid = p.stem
        if sid == me:
            continue
        try:
            if p.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        found.append(sid)
    return sorted(found)


def _ps_invoke(ps1):
    # Absolute powershell path so the wired statusLine/nudge work even when
    # Claude Code spawns them with a PATH that lacks System32.
    return (f'"{_powershell_exe()}" -NoProfile -ExecutionPolicy Bypass '
            f'-File "{ps1}"')


def _watch_wire(undo=False, with_nudge=False):
    """Idempotently wire (or --undo) the 0-token statusline badge and the
    opt-in token-costing prompt nudge into ~/.claude/settings.json.

    Backs up the file once (.pp.bak), preserves any existing statusLine
    command and existing UserPromptSubmit hooks (e.g. mememo). The previous
    statusLine command is stored so the wrapper can chain to it and --undo
    can restore it exactly."""
    sp = _claude_settings_path()
    if not sp.exists():
        die(f"{sp} not found")
    try:
        data = json.loads(sp.read_text(encoding="utf-8-sig") or "{}")
    except json.JSONDecodeError as e:
        die(f"settings.json is not valid JSON: {e}")
    if not isinstance(data, dict):
        die("settings.json top level is not an object")

    scripts = _skill_scripts_dir()
    sl_ps1 = scripts / "pp-statusline.ps1"
    nudge_ps1 = scripts / "pp-prompt-nudge.ps1"
    sl_cmd = _ps_invoke(sl_ps1)
    nudge_cmd = _ps_invoke(nudge_ps1)
    # Legacy sidecar from the chaining design (v0.8.1 initial); no longer
    # used now that the statusline is standalone. Keep the path to clean
    # it up on undo for installs that ran the old wire.
    prev_file = _PP_HOME / "statusline-prev.txt"

    bak = sp.with_suffix(".json.pp.bak")
    if not bak.exists():
        try:
            bak.write_text(sp.read_text(encoding="utf-8-sig"),
                            encoding="utf-8")
        except OSError:
            pass

    changed = []

    def _strip_nudge(hooks_list):
        out_l = []
        for grp in hooks_list:
            inner = grp.get("hooks", []) if isinstance(grp, dict) else []
            inner2 = [h for h in inner
                      if "pp-prompt-nudge" not in str(h.get("command", ""))]
            if inner2:
                out_l.append({**grp, "hooks": inner2}
                             if isinstance(grp, dict) else grp)
        return out_l

    if undo:
        sl = data.get("statusLine")
        prev = data.pop("_pp_prev_statusline", None)
        if isinstance(sl, dict) and "pp-statusline.ps1" in str(
                sl.get("command", "")):
            if prev is not None:
                data["statusLine"] = {"type": "command", "command": prev}
            else:
                data.pop("statusLine", None)
            changed.append("statusLine restored")
        hk = data.get("hooks", {})
        ups = hk.get("UserPromptSubmit")
        if isinstance(ups, list):
            new_ups = _strip_nudge(ups)
            if new_ups != ups:
                if new_ups:
                    hk["UserPromptSubmit"] = new_ups
                else:
                    hk.pop("UserPromptSubmit", None)
                changed.append("nudge hook removed")
        try:
            prev_file.unlink(missing_ok=True)
        except OSError:
            pass
        # A deliberate undo must stick: stamp the sentinel so the auto-wire
        # hook won't silently re-wire on the next `pp` call.
        try:
            _autowire_sentinel().touch()
        except OSError:
            pass
        sp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        out({"undone": True, "changed": changed, "backup": str(bak)})
        return

    # ---- wire (idempotent) ----
    sl = data.get("statusLine")
    already = isinstance(sl, dict) and "pp-statusline.ps1" in str(
        sl.get("command", ""))
    if not already:
        prev = ""
        if isinstance(sl, dict) and sl.get("command"):
            prev = str(sl["command"])
        data["_pp_prev_statusline"] = prev
        # Standalone statusline: no chaining, no sidecar needed. Remove any
        # leftover sidecar from a prior wire (chaining era).
        try:
            prev_file.unlink(missing_ok=True)
        except OSError:
            pass
        data["statusLine"] = {"type": "command", "command": sl_cmd}
        changed.append("statusLine replaced (badge, 0 tokens)")
    else:
        changed.append("statusLine already wired")

    warning = None
    if with_nudge:
        hk = data.setdefault("hooks", {})
        ups = hk.get("UserPromptSubmit")
        if not isinstance(ups, list):
            ups = []
        has = any("pp-prompt-nudge" in str(h.get("command", ""))
                  for grp in ups if isinstance(grp, dict)
                  for h in grp.get("hooks", []))
        if not has:
            ups.append({"hooks": [{"type": "command",
                                   "command": nudge_cmd}]})
            hk["UserPromptSubmit"] = ups
            changed.append("nudge hook appended (TOKEN COST)")
        else:
            changed.append("nudge hook already present")
        warning = ("The prompt nudge injects ~15-25 tokens into the model "
                   "on prompts where there are unread messages (once per "
                   "batch, then auto-cleared). This DOES incur API/usage "
                   "cost. The statusline badge alone is 0 tokens. Use "
                   "`pp watch wire --undo` to remove, and `pp watch "
                   "interval <Nm>` to slow polling.")

    sp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    payload = {"wired": True, "changed": changed,
               "statusline_cmd": sl_cmd, "backup": str(bak),
               "nudge_enabled": bool(with_nudge),
               "note": "restart Claude Code (or new session) to load the "
                       "statusline/hook changes"}
    if warning:
        payload["cost_warning"] = warning
    # Multi-instance restart awareness: settings.json is global, but each
    # already-running Claude Code instance cached the OLD statusLine at
    # startup and won't load the new one until restart.
    others = _other_recent_sessions()
    if others:
        payload["other_instances_need_restart"] = others
        payload["restart_warning"] = (
            f"{len(others)} other Claude Code session(s) appear active "
            f"(touched pp in the last hour). The new statusLine/hook is "
            f"only loaded at session start -- restart EACH instance to "
            f"see the change there too."
        )
    out(payload)


def cmd_watch(args):
    sub = getattr(args, "watch_cmd", None)
    if sub == "start":
        running = _watch_running()
        if running:
            out({"running": True, "pid": running.get("pid"),
                 "note": "already running"})
            return
        if getattr(args, "foreground", False):
            cmd_watch_daemon(args)
            return
        pid = _spawn_watch_daemon()
        out({"running": True, "pid": pid, "started": True})
        return
    if sub == "stop":
        d = _read_watch_pid()
        pid = d.get("pid") if d else None
        if not pid or not _pid_alive(pid):
            try:
                _watch_pid_path().unlink(missing_ok=True)
            except OSError:
                pass
            out({"stopped": False, "note": "not running"})
            return
        killed = False
        try:
            if os.name == "nt":
                r = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F", "/T"],
                    capture_output=True, text=True, timeout=10)
                killed = r.returncode == 0
            else:
                import signal as _sig
                os.kill(int(pid), _sig.SIGTERM)
                killed = True
        except Exception as e:
            _watch_log(f"stop error: {e!r}")
        for f in (_watch_pid_path(), _watch_lock_path()):
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
        out({"stopped": killed, "pid": pid})
        return
    if sub == "unread":
        d = _watch_unread_load()
        count = int(d.get("count", 0) or 0)
        if getattr(args, "format", "json") == "line":
            # ASCII-only token for the statusline wrapper.
            print(f"[pp:{count}]" if count > 0 else "")
            return
        out({"count": count, "latest": d.get("latest"),
             "updated_at": d.get("updated_at")})
        return
    if sub == "ack":
        _watch_ack()
        out({"acked": True})
        return
    if sub == "peek":
        # Metadata-only: count + latest sender/channel/thread (+ best-effort
        # thread title). Does NOT read bodies and does NOT clear the badge --
        # so you can decide whether this session should /pp-chat:read it.
        d = _watch_unread_load()
        count = int(d.get("count", 0) or 0)
        latest = d.get("latest")
        title = None
        if latest and latest.get("channel") and latest.get("thread"):
            try:
                main = _main_repo_path()
                for srv in [s.get("name")
                            for s in _registry_load().get("servers", [])]:
                    mp = (main / ".pp-worktrees" / srv / "channels"
                          / latest["channel"] / latest["thread"] / "meta.json")
                    if mp.exists():
                        title = (read_json(mp, {}) or {}).get("title")
                        if title:
                            break
            except Exception:
                pass
        out({"count": count, "latest": latest, "title": title,
             "note": "peek does not clear the badge; "
                     "run /pp-chat:read to view + clear"})
        return
    if sub == "interval":
        val = getattr(args, "value", None)
        if val is None:
            secs, src = _resolve_interval()
            out({"interval_seconds": secs, "source": src,
                 "human": f"{secs // 60}m{secs % 60:02d}s"})
            return
        secs = _parse_interval(val)
        if secs is None:
            die("interval must be like 90, 90s, 5m, or 1h")
        secs = max(_WATCH_INTERVAL_MIN, secs)
        wcfg = _config_load().get("watch")
        wcfg = wcfg if isinstance(wcfg, dict) else {}
        wcfg["interval"] = secs
        _config_save({"watch": wcfg})
        note = ("takes effect within one poll cycle (daemon re-reads each "
                "tick)")
        ev = os.environ.get("PAIR_PRESSURE_WATCH_INTERVAL")
        payload = {"interval_seconds": secs, "saved": True, "note": note}
        if ev is not None and ev.strip() != "":
            payload["warning"] = ("PAIR_PRESSURE_WATCH_INTERVAL env override "
                                  "is set and still wins until unset")
        out(payload)
        return
    if sub == "wire":
        _watch_wire(undo=getattr(args, "undo", False),
                    with_nudge=getattr(args, "nudge", False))
        return
    # status (default)
    d = _read_watch_pid() or {}
    running = bool(_watch_running())
    log_tail = ""
    try:
        lp = _watch_log_path()
        if lp.exists():
            log_tail = "\n".join(
                lp.read_text(encoding="utf-8", errors="replace")
                .splitlines()[-10:])
    except OSError:
        pass
    last_notify = None
    try:
        last_notify = json.loads(
            _watch_notify_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    interval, isrc = _resolve_interval()
    unread = _watch_unread_load()
    out({
        "running": running,
        "pid": d.get("pid"),
        "started_at": d.get("started_at"),
        "interval": interval,
        "interval_source": isrc,
        "offline": _offline(),
        "last_notify": last_notify,
        "unread": int(unread.get("count", 0) or 0),
        "watch_state_keys": len(_watch_state_load()),
        "log_tail": log_tail,
    })


def main():
    p = argparse.ArgumentParser(prog="pp", description="pair-pressure CLI")
    p.add_argument("--version", action="version", version=f"pair-pressure {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("pull", help="git pull --rebase --autostash")
    _add_server_arg(sp)
    _add_repo_arg(sp)
    sp.set_defaults(func=cmd_pull)

    sp = sub.add_parser("push", help="git push if ahead")
    _add_server_arg(sp)
    _add_repo_arg(sp)
    sp.set_defaults(func=cmd_push)

    sp = sub.add_parser("list-channels")
    sp.add_argument("--no-pull", action="store_true")
    sp.add_argument("--all", action="store_true",
                    help="include archived channels (hidden by default)")
    _add_server_arg(sp)
    _add_repo_arg(sp)
    sp.set_defaults(func=cmd_list_channels)

    sp = sub.add_parser("list-threads")
    sp.add_argument("--channel", required=True)
    sp.add_argument("--limit", type=int, default=0)
    sp.add_argument("--no-pull", action="store_true")
    _add_server_arg(sp)
    _add_repo_arg(sp)
    sp.set_defaults(func=cmd_list_threads)

    sp = sub.add_parser("read-thread")
    sp.add_argument("--channel", required=True)
    sp.add_argument("--thread", required=True)
    sp.add_argument("--since", type=int, default=0)
    sp.add_argument("--no-pull", action="store_true")
    _add_server_arg(sp)
    _add_repo_arg(sp)
    sp.set_defaults(func=cmd_read_thread)

    sp = sub.add_parser("new-thread")
    sp.add_argument("--channel", required=True)
    sp.add_argument("--title", required=True)
    sp.add_argument(
        "--kind",
        default="discussion",
        choices=["discussion", "investigation", "task", "decision"],
    )
    sp.add_argument("--body-file", required=True, help="path or '-' for stdin")
    sp.add_argument("--summary", default="")
    sp.add_argument("--via", default="claude-code")
    sp.add_argument("--model", default=None)
    sp.add_argument("--alias", default=None,
                    help="per-call alias override; beats PAIR_PRESSURE_ALIAS. "
                         "Ignored when --via=human.")
    sp.add_argument("--password", default=None,
                    help="advisory access marker; sha256-hashed into meta.json. "
                         "AVOID on the CLI -- visible in process listings. "
                         "Prefer --password-stdin.")
    sp.add_argument("--password-stdin", action="store_true",
                    help="read password as the first line of stdin (before the "
                         "body, when --body-file is '-')")
    sp.add_argument("--attach", dest="attachments", action="append", default=None,
                    metavar="PATH",
                    help="copy file into the post's attachments/ dir and "
                         "append a markdown link. Repeatable. Use `@@<path>` "
                         "in the body to attach + link inline instead.")
    _add_server_arg(sp)
    _add_repo_arg(sp)
    sp.set_defaults(func=cmd_new_thread)

    sp = sub.add_parser("reply")
    sp.add_argument("--channel", required=True)
    sp.add_argument("--thread", required=True)
    sp.add_argument(
        "--stance",
        default="extend",
        choices=["agree", "contradict", "extend", "question", "summary"],
    )
    sp.add_argument("--in-reply-to", default=None)
    sp.add_argument("--body-file", required=True, help="path or '-' for stdin")
    sp.add_argument("--summary", default=None)
    sp.add_argument("--via", default="claude-code")
    sp.add_argument("--model", default=None)
    sp.add_argument("--alias", default=None,
                    help="per-call alias override; beats PAIR_PRESSURE_ALIAS. "
                         "Ignored when --via=human.")
    sp.add_argument("--attach", dest="attachments", action="append", default=None,
                    metavar="PATH",
                    help="copy file into the post's attachments/ dir and "
                         "append a markdown link. Repeatable. Use `@@<path>` "
                         "in the body to attach + link inline instead.")
    _add_server_arg(sp)
    _add_repo_arg(sp)
    sp.set_defaults(func=cmd_reply)

    sp = sub.add_parser("claim", help="atomically claim a task thread")
    sp.add_argument("--channel", required=True)
    sp.add_argument("--thread", required=True)
    sp.add_argument("--via", default="claude-code")
    _add_server_arg(sp)
    _add_repo_arg(sp)
    sp.set_defaults(func=cmd_claim)

    sp = sub.add_parser("start", help="mark a claimed task as in_progress (assignee only)")
    sp.add_argument("--channel", required=True)
    sp.add_argument("--thread", required=True)
    _add_server_arg(sp)
    _add_repo_arg(sp)
    sp.set_defaults(func=cmd_start)

    sp = sub.add_parser("complete", help="mark a task done (assignee only)")
    sp.add_argument("--channel", required=True)
    sp.add_argument("--thread", required=True)
    sp.add_argument("--summary", default=None)
    _add_server_arg(sp)
    _add_repo_arg(sp)
    sp.set_defaults(func=cmd_complete)

    sp = sub.add_parser("abandon", help="release a claim (assignee only by default)")
    sp.add_argument("--channel", required=True)
    sp.add_argument("--thread", required=True)
    sp.add_argument("--reason", default=None)
    sp.add_argument("--force", action="store_true",
                    help="abandon even if you are not the assignee")
    _add_server_arg(sp)
    _add_repo_arg(sp)
    sp.set_defaults(func=cmd_abandon)

    sp = sub.add_parser("handoff", help="reassign a claim (current assignee only)")
    sp.add_argument("--channel", required=True)
    sp.add_argument("--thread", required=True)
    sp.add_argument("--to", required=True)
    _add_server_arg(sp)
    _add_repo_arg(sp)
    sp.set_defaults(func=cmd_handoff)

    sp = sub.add_parser("join", help="record current author as a thread member")
    sp.add_argument("--channel", required=True)
    sp.add_argument("--thread", required=True)
    sp.add_argument("--password", default=None,
                    help="required if the thread was created with --password. "
                         "AVOID on the CLI -- visible in process listings. "
                         "Prefer --password-stdin.")
    sp.add_argument("--password-stdin", action="store_true",
                    help="read password from stdin (entire stdin = password)")
    _add_server_arg(sp)
    _add_repo_arg(sp)
    sp.set_defaults(func=cmd_join)

    sp = sub.add_parser("resolve",
                        help="mark a discussion/investigation/decision thread resolved")
    sp.add_argument("--channel", required=True)
    sp.add_argument("--thread", required=True)
    sp.add_argument("--outcome", default=None,
                    help="for decision threads: REQUIRED, must be "
                         "accepted|rejected|superseded; "
                         "for others: optional free-text summary appended as a final post")
    sp.add_argument("--via", default="claude-code")
    sp.add_argument("--alias", default=None,
                    help="per-call alias override; beats PAIR_PRESSURE_ALIAS. "
                         "Ignored when --via=human.")
    _add_server_arg(sp)
    _add_repo_arg(sp)
    sp.set_defaults(func=cmd_resolve)

    sp = sub.add_parser("status",
                        help="show saved vs active env vars; works without configuration")
    _add_repo_arg(sp)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("offline",
                        help="show or set offline mode (skip fetch/pull/push; "
                             "commits stay local)")
    sp.add_argument("state", nargs="?", default=None,
                    choices=["true", "false"])
    sp.set_defaults(func=cmd_offline)

    sp = sub.add_parser("search", help="grep posts; filter by channel/kind/status/assignee/author/stance")
    sp.add_argument("--query", required=True)
    sp.add_argument("--channel", default=None)
    sp.add_argument(
        "--kind",
        default=None,
        choices=["discussion", "investigation", "task", "decision"],
    )
    sp.add_argument("--status", default=None)
    sp.add_argument("--assignee", default=None)
    sp.add_argument("--author", default=None)
    sp.add_argument(
        "--stance",
        default=None,
        choices=["agree", "contradict", "extend", "question", "summary"],
    )
    sp.add_argument("--limit", type=int, default=0)
    sp.add_argument("--no-pull", action="store_true")
    _add_server_arg(sp)
    _add_repo_arg(sp)
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("feed",
                        help="cross-thread feed view: posts chronological "
                             "(oldest first); --channel narrows scope")
    sp.add_argument("--channel", default=None)
    sp.add_argument("--since", default=None,
                    help="ISO timestamp; only posts >= this are returned")
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--no-pull", action="store_true")
    sp.add_argument("--all-servers", action="store_true",
                    help="span every server on the active repo")
    sp.add_argument("--all-repos", action="store_true",
                    help="span every registered repo (implies --all-servers)")
    _add_server_arg(sp)
    _add_repo_arg(sp)
    sp.set_defaults(func=cmd_feed)

    sp = sub.add_parser("unread",
                        help="new posts not authored by you across servers/"
                             "repos; for polling clients")
    sp.add_argument("--all", action="store_true",
                    help="span every server on the active repo")
    sp.add_argument("--all-repos", action="store_true",
                    help="span every registered repo (implies --all)")
    sp.add_argument("--since", default=None,
                    help="ISO timestamp; count posts >= this instead of using "
                         "the watcher baseline")
    sp.add_argument("--no-pull", action="store_true")
    _add_server_arg(sp)
    _add_repo_arg(sp)
    sp.set_defaults(func=cmd_unread)

    sp = sub.add_parser("aliases-in-use",
                        help="report aliases active in the last N minutes; "
                             "used to detect collisions before claiming a name")
    sp.add_argument("--since-minutes", type=int, default=30,
                    help="activity window in minutes (default: 30)")
    sp.add_argument("--no-pull", action="store_true")
    _add_server_arg(sp)
    _add_repo_arg(sp)
    sp.set_defaults(func=cmd_aliases_in_use)

    # --- smart verbs ---
    sp = sub.add_parser("send",
                        help="smart post: auto-resolves channel/thread from "
                             "state + env defaults; creates thread on demand")
    sp.add_argument("--channel", default=None,
                    help="override resolved channel (else: per-session > global "
                         "> PAIR_PRESSURE_DEFAULT_CHANNEL > 'general')")
    sp.add_argument("--thread", default=None,
                    help="override resolved thread id (else: state file > "
                         "fuzzy-match by title slug > create new)")
    sp.add_argument("--stance", default="extend",
                    choices=["agree", "contradict", "extend", "question", "summary"])
    sp.add_argument("--in-reply-to", default=None)
    sp.add_argument("--body-file", default="-", help="path or '-' for stdin")
    sp.add_argument("--summary", default=None)
    sp.add_argument("--via", default="claude-code")
    sp.add_argument("--model", default=None)
    sp.add_argument("--alias", default=None,
                    help="per-call alias override; beats PAIR_PRESSURE_ALIAS")
    sp.add_argument("--password", default=None,
                    help="only used when auto-creating a thread; "
                         "prefer --password-stdin")
    sp.add_argument("--password-stdin", action="store_true",
                    help="read password as first line of stdin")
    sp.add_argument("--attach", dest="attachments", action="append", default=None,
                    metavar="PATH",
                    help="copy file into the post's attachments/ dir and "
                         "append a markdown link. Repeatable. Use `@@<path>` "
                         "in the body to attach + link inline instead.")
    _add_server_arg(sp)
    _add_repo_arg(sp)
    sp.set_defaults(func=cmd_send)

    sp = sub.add_parser("read",
                        help="smart read: no target=feed; channel=channel feed; "
                             "fuzzy thread match otherwise")
    sp.add_argument("target", nargs="?", default=None,
                    help="optional channel name or thread title/id substring")
    sp.add_argument("--message", dest="message_id", default=None,
                    metavar="<id>",
                    help="print the FULL body of one post by id (full id or "
                         "short 6-char handle); bypasses feed truncation")
    sp.add_argument("--limit", type=int, default=30)
    sp.add_argument("--no-pull", action="store_true")
    sp.add_argument("--pretty", action="store_true",
                    help="render human-readable ANSI-colored chat instead of "
                         "JSON (per-author color); for terminal display")
    _add_server_arg(sp)
    _add_repo_arg(sp)
    sp.set_defaults(func=cmd_read)

    sp_task = sub.add_parser("task", help="task lifecycle (smart, indexed)")
    sub_task = sp_task.add_subparsers(dest="task_cmd", required=True)

    sp = sub_task.add_parser("list",
                             help="number all task threads on the active "
                                  "server (newest first; incl. done)")
    sp.add_argument("--no-pull", action="store_true")
    _add_server_arg(sp)
    _add_repo_arg(sp)
    sp.set_defaults(func=cmd_task_list)

    sp = sub_task.add_parser("claim",
                             help="claim a task by #n / id / title")
    sp.add_argument("ref")
    sp.add_argument("--via", default="claude-code")
    _add_server_arg(sp)
    _add_repo_arg(sp)
    sp.set_defaults(func=cmd_task_claim)

    sp = sub_task.add_parser("update",
                             help="set task state by #n / id / title")
    sp.add_argument("ref")
    sp.add_argument("status",
                    choices=["claimed", "in_progress", "done", "abandoned"])
    sp.add_argument("--summary", default=None)
    sp.add_argument("--reason", default=None)
    sp.add_argument("--via", default="claude-code")
    _add_server_arg(sp)
    _add_repo_arg(sp)
    sp.set_defaults(func=cmd_task_update)

    sp = sub_task.add_parser("show",
                             help="open a task by #n / id / title")
    sp.add_argument("ref")
    sp.add_argument("--no-pull", action="store_true")
    _add_server_arg(sp)
    _add_repo_arg(sp)
    sp.set_defaults(func=cmd_task_show)

    sp = sub_task.add_parser("handoff",
                             help="reassign a task by #n / id / title")
    sp.add_argument("ref")
    sp.add_argument("to")
    _add_server_arg(sp)
    _add_repo_arg(sp)
    sp.set_defaults(func=cmd_task_handoff)

    sp = sub_task.add_parser("abandon",
                             help="release a task claim by #n / id / title")
    sp.add_argument("ref")
    sp.add_argument("--reason", default=None)
    sp.add_argument("--force", action="store_true")
    _add_server_arg(sp)
    _add_repo_arg(sp)
    sp.set_defaults(func=cmd_task_abandon)

    sp = sub_task.add_parser("new",
                             help="create a task thread (auto-resolves channel; "
                                  "optional --to claims+handoffs)")
    sp.add_argument("title")
    sp.add_argument("--channel", default=None)
    sp.add_argument("--to", default=None,
                    help="assignee to claim+handoff to immediately")
    sp.add_argument("--body-file", default=None,
                    help="path or '-' for stdin; if omitted a seed template "
                         "is written")
    sp.add_argument("--password", default=None)
    sp.add_argument("--password-stdin", action="store_true")
    sp.add_argument("--via", default="human")
    sp.add_argument("--attach", dest="attachments", action="append", default=None,
                    metavar="PATH",
                    help="copy file into the seed post's attachments/ dir "
                         "and append a markdown link. Repeatable. Use "
                         "`@@<path>` in the body to attach + link inline.")
    _add_server_arg(sp)
    _add_repo_arg(sp)
    sp.set_defaults(func=cmd_task_new)

    sp = sub_task.add_parser("done",
                             help="mark the current thread (from state) done; "
                                  "refuses if not kind=task")
    sp.add_argument("--summary", default=None)
    sp.add_argument("--channel", default=None,
                    help="override resolved channel")
    sp.add_argument("--thread", default=None,
                    help="override resolved thread id")
    _add_server_arg(sp)
    _add_repo_arg(sp)
    sp.set_defaults(func=cmd_task_done)

    sp_watch = sub.add_parser("watch",
                              help="background new-message notifier "
                                   "(auto-starts; manual control here)")
    sub_watch = sp_watch.add_subparsers(dest="watch_cmd", required=False)
    sp = sub_watch.add_parser("start", help="spawn the daemon if not running")
    sp.add_argument("--foreground", action="store_true",
                    help="run the poll loop inline (debug)")
    sp.set_defaults(func=cmd_watch)
    sub_watch.add_parser("stop", help="stop the daemon") \
        .set_defaults(func=cmd_watch)
    sub_watch.add_parser("status", help="show daemon status (default)") \
        .set_defaults(func=cmd_watch)
    sp = sub_watch.add_parser("unread",
                              help="unread-message count (for statusline)")
    sp.add_argument("--format", choices=["json", "line"], default="json")
    sp.set_defaults(func=cmd_watch)
    sub_watch.add_parser("ack", help="clear the unread counter") \
        .set_defaults(func=cmd_watch)
    sub_watch.add_parser("peek",
                         help="show unread count + latest sender/thread title "
                              "(no body, does not clear the badge)") \
        .set_defaults(func=cmd_watch)
    sp = sub_watch.add_parser("interval",
                              help="show or set poll interval "
                                   "(e.g. 90, 90s, 5m, 1h)")
    sp.add_argument("value", nargs="?", default=None)
    sp.set_defaults(func=cmd_watch)
    sp = sub_watch.add_parser("wire",
                              help="integrate statusline badge (0 tokens) + "
                                   "optional --nudge prompt hook into "
                                   "~/.claude/settings.json (idempotent)")
    sp.add_argument("--nudge", action="store_true",
                    help="also add the in-prompt nudge (INCURS TOKEN COST)")
    sp.add_argument("--undo", action="store_true",
                    help="restore the original statusLine + remove the hook")
    sp.set_defaults(func=cmd_watch)
    sp_watch.set_defaults(func=cmd_watch, watch_cmd=None)

    sp = sub.add_parser("_watch-daemon")  # hidden: the poll loop entrypoint
    sp.set_defaults(func=cmd_watch_daemon)

    sp_channel = sub.add_parser("channel", help="channel management")
    sub_channel = sp_channel.add_subparsers(dest="channel_cmd", required=True)
    sp = sub_channel.add_parser("ensure",
                                help="create a channel if missing; no-op if it exists")
    sp.add_argument("--name", required=True)
    sp.add_argument("--description", default=None)
    _add_server_arg(sp)
    _add_repo_arg(sp)
    sp.set_defaults(func=cmd_channel_ensure)

    sp = sub_channel.add_parser("archive",
                                help="hide a channel from list/feed/read/watch "
                                     "(history kept; reversible)")
    sp.add_argument("name")
    _add_server_arg(sp)
    _add_repo_arg(sp)
    sp.set_defaults(func=cmd_channel_archive)

    sp = sub_channel.add_parser("unarchive",
                                help="restore an archived channel")
    sp.add_argument("name")
    _add_server_arg(sp)
    _add_repo_arg(sp)
    sp.set_defaults(func=cmd_channel_unarchive)

    sp = sub.add_parser("servers", help="list servers (alias for `pp server list`)")
    _add_repo_arg(sp)
    sp.set_defaults(func=cmd_servers)

    sp_server = sub.add_parser("server", help="server management")
    sub_server = sp_server.add_subparsers(dest="server_cmd", required=True)

    sp = sub_server.add_parser("list", help="list servers in the registry")
    _add_repo_arg(sp)
    sp.set_defaults(func=cmd_servers)

    sp = sub_server.add_parser("new", help="create a new server (branch + worktree + channels)")
    sp.add_argument("name")
    sp.add_argument("--description", default=None,
                    help="short description stored in the registry")
    sp.add_argument("--channels", default=None,
                    help="comma-separated channel list (default: general)")
    _add_repo_arg(sp)
    sp.set_defaults(func=cmd_server_new)

    sp = sub_server.add_parser("switch",
                               help="validate + materialize a server worktree; "
                                    "prints env-export hints")
    sp.add_argument("name")
    _add_repo_arg(sp)
    sp.set_defaults(func=cmd_server_switch)

    sp = sub_server.add_parser("remove",
                               help="delete a server branch + worktree + registry entry "
                                    "(requires --yes)")
    sp.add_argument("name")
    sp.add_argument("--yes", action="store_true")
    _add_repo_arg(sp)
    sp.set_defaults(func=cmd_server_remove)

    sp_repo = sub.add_parser("repo", help="manage multiple chat repos "
                                          "(switchable per session)")
    sub_repo = sp_repo.add_subparsers(dest="repo_cmd", required=False)

    sub_repo.add_parser("list", help="list registered chat repos") \
        .set_defaults(func=cmd_repo_list)

    sp = sub_repo.add_parser("add",
                             help="register a chat repo: clone (or adopt "
                                  "--path) + optional init")
    sp.add_argument("name")
    sp.add_argument("url", help="git remote URL (recorded; used for clone)")
    sp.add_argument("--path", default=None,
                    help="register an existing clone at this path instead of "
                         "cloning into ~/.pair-pressure/repos/<name>")
    sp.add_argument("--no-clone", action="store_true",
                    help="adopt an existing clone (implied by --path)")
    sp.add_argument("--with-server", default=None, metavar="NAME",
                    help="scaffold/ensure an initial server in one step")
    sp.add_argument("--channels", default=None,
                    help="comma-separated channels for --with-server")
    sp.set_defaults(func=cmd_repo_add)

    sp = sub_repo.add_parser("use",
                             help="pin this session to a registered repo "
                                  "(clears active server; prints env-export hints)")
    sp.add_argument("name")
    sp.set_defaults(func=cmd_repo_use)

    sp = sub_repo.add_parser("remove",
                             help="unregister a repo (requires --yes); "
                                  "--delete-clone also removes the on-disk clone")
    sp.add_argument("name")
    sp.add_argument("--yes", action="store_true")
    sp.add_argument("--delete-clone", action="store_true",
                    help="also rmtree the clone (only if under "
                         "~/.pair-pressure/repos/)")
    sp.set_defaults(func=cmd_repo_remove)

    sp_repo.set_defaults(func=cmd_repo_list, repo_cmd=None)

    args = p.parse_args()
    _activate_repo(args)  # pin --repo before any verb (may die on bad --repo)
    try:
        _ensure_watcher(args)
    except Exception:
        pass  # a watcher bug must never break a normal pp call
    try:
        _ensure_wired(args)
    except Exception:
        pass  # nor an auto-wire bug
    try:
        args.func(args)
    except subprocess.CalledProcessError as e:
        die(f"git error: {e.stderr.strip() or e.stdout.strip()}")


if __name__ == "__main__":
    main()
