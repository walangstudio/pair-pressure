#!/usr/bin/env python3
"""pair-pressure: shared group chat among AI agents and humans, backed by git.

Single-file, stdlib-only. All output is JSON on stdout; errors go to stderr
and exit nonzero.

Model (schema v3): one git repo = one server (Discord-style). Channels are
flat group chats — posts go straight into the channel, no threads. The server
registry lives at ~/.pair-pressure/servers.json.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = "3"  # chat-repo schema; matches pp-init.py

_VIA_SHORT = {"claude-code": "cc", "human": "h", "mcp": "mcp"}
_VIA_LONG = {v: k for k, v in _VIA_SHORT.items()}

# Resolved once per invocation by _activate(): the active server's name and
# clone path. None until a verb that needs the chat repo resolves it.
_ACTIVE_SERVER: "str | None" = None
_ACTIVE_REPO: "Path | None" = None

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


def _require_schema_v3(p):
    """Die with a clear message when `p` is not a schema-v3 chat repo.

    v2 repos (the pre-1.0 thread/branch model) are not migrated — a fresh
    pp-init is required. A missing marker on an otherwise pair-pressure-shaped
    repo is treated as v2 (the marker was introduced alongside v2)."""
    marker = p / ".pair-pressure" / "schema-version"
    try:
        ver = marker.read_text(encoding="utf-8-sig").strip()
    except OSError:
        ver = None
    if ver == SCHEMA_VERSION:
        return p
    if ver is not None or (p / ".pair-pressure").exists():
        die(f"chat repo {p} uses schema v{ver or '2'}; pair-pressure 1.0 "
            "needs schema v3. v2 content is not migrated — create a fresh "
            "repo with pp-init (or point pp at a v3 repo).")
    die(f"{p} is not a pair-pressure chat repo (no .pair-pressure/). "
        "Run pp-init <dir> to create one.")


# ---- session/global state (where you are: server + channel + alias) ----

STATE_SCHEMA_VERSION = 3  # v3 drops thread_id/repo, adds alias

# Machine-global, NON-git-tracked home for the server registry, state,
# config, and watcher files.
_PP_HOME = Path.home() / ".pair-pressure"


def _session_id():
    sid = os.environ.get("PAIR_PRESSURE_SESSION_ID")
    return sid.strip() if sid and sid.strip() else None


def _state_path_global():
    """Machine-global last-used state — the default for new conversations."""
    return _PP_HOME / "state.json"


def _state_path_session():
    sid = _session_id()
    if not sid:
        return None
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", sid)[:64] or "anon"
    return _PP_HOME / "sessions" / f"{safe}.json"


def _state_load_one(path):
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _state_load():
    """Return (per_session, global). Either may be None."""
    sess = _state_load_one(_state_path_session()) if _session_id() else None
    glob = _state_load_one(_state_path_global())
    return sess, glob


def _state_save(server=None, channel=None, alias=None, source=None,
                session_only=False):
    """Best-effort merge-write to per-session (and, unless session_only,
    global) state files. Never raises — verbs must not die on state-write
    failure.

    Merge semantics: None means "leave the existing value alone", so
    switching channels never blanks the alias and vice versa. The session
    file makes settings resume-safe per conversation; the global file is the
    new-conversation default ("where you last were")."""
    patch = {"server": server, "channel": channel, "alias": alias}
    paths = (
        (_state_path_session(),) if session_only
        else (_state_path_session(), _state_path_global())
    )
    for path in paths:
        if path is None:
            continue
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            merged = _state_load_one(path) or {}
            merged.update({k: v for k, v in patch.items() if v is not None})
            merged["schema_version"] = STATE_SCHEMA_VERSION
            merged["updated_at"] = now_iso()
            merged["source"] = source or "unknown"
            merged.pop("thread_id", None)  # v2 leftovers
            merged.pop("repo", None)
            merged.pop("task_index", None)
            path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
        except OSError:
            continue


# ---- machine-global server registry (a server = a chat repo clone) ----
#
# Lives under _PP_HOME. Tolerant load mirrors _config_load — a missing or
# malformed file yields an empty registry, never a die(), so env-var-only
# installs keep working untouched.

def _servers_registry_path():
    return _PP_HOME / "servers.json"


def _servers_load():
    p = _servers_registry_path()
    if not p.exists():
        return {"schema_version": 2, "servers": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 2, "servers": []}
    if not isinstance(data, dict) or not isinstance(data.get("servers"), list):
        return {"schema_version": 2, "servers": []}
    return data


def _servers_save(data):
    """Best-effort write of servers.json. Never raises (same contract as
    _config_save)."""
    try:
        p = _servers_registry_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def _servers_list():
    return _servers_load().get("servers", [])


def _server_entry(name):
    for s in _servers_list():
        if s.get("name") == name:
            return s
    return None


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


def _compat_env_repo_entry():
    """Back-compat: PAIR_PRESSURE_REPO (a clone path) acts as a server named
    `default` when nothing is registered. Auto-registers on first touch so
    the rest of the code only ever deals with registry entries."""
    ev = os.environ.get("PAIR_PRESSURE_REPO")
    if not ev:
        return None
    p = Path(ev).expanduser()
    entry = {"name": "default", "path": str(p), "url": None,
             "added_at": now_iso()}
    reg = _servers_load()
    if not reg["servers"]:
        reg["servers"].append(entry)
        _servers_save(reg)
    return entry


def resolve_server_name(flag=None):
    """Resolve the active server name. Returns (name, source) — name is None
    when nothing resolves (caller decides whether that's fatal).

    Priority: --server flag > session state > global state >
    PAIR_PRESSURE_SERVER > registry default > sole registered entry >
    PAIR_PRESSURE_REPO compat."""
    if flag:
        return flag, "arg"
    sess, glob = _state_load()
    if sess and sess.get("server"):
        return sess["server"], "session"
    if glob and glob.get("server"):
        return glob["server"], "global"
    ev = os.environ.get("PAIR_PRESSURE_SERVER")
    if ev:
        return ev, "env"
    reg = _servers_load()
    servers = reg.get("servers", [])
    default = reg.get("default")
    if default and any(s.get("name") == default for s in servers):
        return default, "registry-default"
    if len(servers) == 1:
        return servers[0].get("name"), "sole"
    compat = _compat_env_repo_entry()
    if compat:
        return compat["name"], "env-repo"
    return None, None


def _server_path(name):
    """The validated clone path for a registered server, or die."""
    entry = _server_entry(name) or (
        _compat_env_repo_entry() if name == "default" else None)
    if not entry or not entry.get("path"):
        die(f"server '{name}' is not registered (try `pp server list`; "
            "add one with `pp server add <name> <url>`).")
    p = _validated_repo(Path(entry["path"]).expanduser())
    return _require_schema_v3(p)


def _activate(args):
    """Resolve the active server and pin its clone path for this invocation.

    Called lazily via repo_path()/active_server() so registry-only verbs
    (server list/add, status, watch, offline) never demand a configured
    server."""
    global _ACTIVE_SERVER, _ACTIVE_REPO
    name, _src = resolve_server_name(getattr(args, "server", None)
                                     if args is not None else None)
    if not name:
        die("no server configured; run `pp server add <name> <url>` "
            "(or set PAIR_PRESSURE_REPO to a chat repo clone).")
    _ACTIVE_SERVER = name
    _ACTIVE_REPO = _server_path(name)
    return name


def repo_path():
    """The active server's clone path. Resolves lazily on first use."""
    if _ACTIVE_REPO is None:
        _activate(None)
    return _ACTIVE_REPO


def active_server():
    """The active server's name. Resolves lazily on first use."""
    if _ACTIVE_SERVER is None:
        _activate(None)
    return _ACTIVE_SERVER


def resolve_active(args):
    """Fill in args.server/channel from state + defaults.

    Priority per field: explicit arg > per-session state > global state >
    env/default. Mutates `args` in place and returns the resolved values
    plus `sources` diagnostics."""
    sess, glob = _state_load()
    sources = {}

    server, ssrc = resolve_server_name(getattr(args, "server", None))
    if not server:
        die("no server configured; run `pp server add <name> <url>` "
            "(or set PAIR_PRESSURE_REPO to a chat repo clone).")
    sources["server"] = ssrc
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

    return {
        "server": server,
        "channel": channel,
        "sources": sources,
    }


def _where_line(server, channel, with_alias=True):
    """The one-line location banner: `acme #general (alias: Echo)`."""
    line = f"{server} #{channel}"
    if with_alias:
        a = effective_alias()
        if a:
            line += f" (alias: {a})"
    return line


def _add_server_arg(sp):
    """Attach the standard --server flag to a subparser."""
    sp.add_argument(
        "--server", default=None,
        help="server name (see `pp server list`); overrides the active "
             "server for this call.",
    )


def author():
    return env("PAIR_PRESSURE_AUTHOR")


def alias():
    """The configured alias: env beats persisted state.

    Priority: PAIR_PRESSURE_ALIAS env > per-session state > global state.
    `pp alias <name>` persists to state, so a conversation that resumes with
    the same PAIR_PRESSURE_SESSION_ID gets its alias back."""
    a = os.environ.get("PAIR_PRESSURE_ALIAS")
    if a and a.strip():
        return a.strip()
    sess, glob = _state_load()
    for st in (sess, glob):
        v = (st or {}).get("alias")
        if v and str(v).strip():
            return str(v).strip()
    return None


def effective_alias(args=None):
    """Resolve the alias for THIS call: --alias flag beats everything."""
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


# Slim 2-line post header. Not YAML — a fixed by:/rt: pair.
_FM_RE = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)


def parse_slim(text):
    """Parse the slim post header. Returns (fm_dict, body) or (None, None).

    Layout:
        ---
        by: alice/Echo via=cc m=opus47
        rt: 20260512T143022123Z r=20260512T142811007Z
        ---
        <body>

    Unknown `k=v` tokens (e.g. the retired `s=` stance) are ignored, so v2
    posts still parse."""
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
    irt = None
    for kv in rt_parts[1:]:
        if "=" not in kv:
            continue
        k, v = kv.split("=", 1)
        if k == "r":
            irt = v

    return {
        "id": pid,
        "reply_to": irt,
        "author": author_,
        "alias": alias_,
        "via": via,
        "model": model,
        "timestamp": _id_to_iso(pid) or pid,
    }, m.group(2)


def parse_post(text):
    """Parse a post file. Returns a dict with keys: id, reply_to, author,
    alias, via, model, timestamp — or ({}, text) when the header is absent."""
    fm, body = parse_slim(text)
    if fm is not None:
        return fm, body
    return {"alias": None}, text


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

    # Always-visible location: where you are / where the next send lands.
    where = payload.get("where")
    if where:
        print(f"{_C_DIM}[{san(str(where))}]{_C_RST}")

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
                  f"{san(str(m.get('channel')))}{_C_RST}")
        return

    if view == "channel":
        print(f"{_C_BOLD}#{san(str(payload.get('channel')))}{_C_RST}")
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
            loc = f" {_C_DIM}#{san(p.get('channel') or '')}{_C_RST}"
        reply = san(p.get("reply_to") or "")
        reply_s = f" {_C_DIM}↩{reply[-6:]}{_C_RST}" if reply else ""
        sid = san(p.get("id") or "")
        # Short id handle (last 6 chars) so a truncated post can be fetched in
        # full via `pp read --message <id>`. Matches read.md's convention.
        ids = f" {_C_DIM}·{sid[-6:]}{_C_RST}" if sid else ""
        print(f"{_C_DIM}{ts}{_C_RST}  {col}{_C_BOLD}{who}{_C_RST}"
              f"{reply_s}{loc}{ids}")
        body = _unwrap_untrusted(p.get("body") or "")
        for line in body.split("\n"):
            print(f"   {col}{san(line)}{_C_RST}")
        print()


def dump_slim(by, via, model, pid, reply_to, body):
    by_line = f"by: {by} via={_short_via(via)}"
    sm = _short_model(model)
    if sm and via != "human":
        by_line += f" m={sm}"
    rt_line = f"rt: {pid}"
    if reply_to:
        rt_line += f" r={reply_to}"
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
    # utf-8-sig tolerates a BOM (PowerShell 5.1 writes one) and decodes UTF-8
    # regardless of the platform default (cp1252 on Windows would mangle a
    # hand-edited channel.json/tasks.json).
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path, obj):
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


# ---- verbs ----

def cmd_pull(args):
    _activate(args)
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
    _activate(args)
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


def cmd_channels(args):
    _activate(args)
    if not args.no_pull:
        maybe_pull()
    root = repo_path() / "channels"
    channels = []
    show_all = getattr(args, "all", False)
    me = author()
    sess, glob = _state_load()
    active = ((sess or {}).get("channel") or (glob or {}).get("channel")
              or _default_channel())
    if root.exists():
        for ch in sorted(p for p in root.iterdir() if p.is_dir()):
            if not _channel_visible(ch, me, include_archived=show_all):
                continue  # shared predicate: hides non-member DMs + archived
            meta = _channel_meta(ch)
            archived = bool(meta.get("archived"))
            private = bool(meta.get("private"))
            members = meta.get("members") or []
            newest = _post_files_desc(ch, limit=1)
            last_at = None
            if newest:
                last_at = _id_to_iso(_stem_id(newest[0]))
            channels.append({
                "name": meta.get("name", ch.name),
                "description": meta.get("description", ""),
                "post_count": len(_post_files(ch)),
                "last_activity": last_at,
                "archived": archived,
                "private": private,
                "members": members if private else None,
                "active": meta.get("name", ch.name) == active,
            })
    out({"where": _where_line(active_server(), active),
         "channels": channels})


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


# ---- post storage: channels/<ch>/posts/YYYY-MM/<id>.md (month shards) ----
#
# A channel is a flat stream of posts. Sharding by month keeps directory
# sizes bounded for git/GitHub; the post id embeds the shard key, so
# chronological order is lexicographic across shards and within them.

_SHARD_RE = re.compile(r"^\d{4}-\d{2}$")
_POST_NAME_RE = re.compile(r"^\d{8}T\d{9}Z\.md$")


def _posts_root(ch_dir):
    return ch_dir / "posts"


def _shard_for(pid):
    return f"{pid[0:4]}-{pid[4:6]}"


def _stem_id(path):
    return path.stem


def _shard_dirs(ch_dir):
    root = _posts_root(ch_dir)
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir()
                  if p.is_dir() and _SHARD_RE.match(p.name))


def _post_files(ch_dir):
    """All post files in a channel, chronological (oldest first)."""
    files = []
    for shard in _shard_dirs(ch_dir):
        files.extend(sorted(
            f for f in shard.iterdir()
            if f.is_file() and _POST_NAME_RE.match(f.name)))
    return files


def _post_files_desc(ch_dir, limit=None):
    """Newest-first post files; walks shards in reverse and stops at
    `limit` so 'last N posts' never scans the whole history."""
    files = []
    for shard in reversed(_shard_dirs(ch_dir)):
        batch = sorted(
            (f for f in shard.iterdir()
             if f.is_file() and _POST_NAME_RE.match(f.name)),
            reverse=True)
        files.extend(batch)
        if limit is not None and len(files) >= limit:
            break
    return files[:limit] if limit is not None else files


def _post_attachments(post_file, pid):
    att_dir = post_file.parent / "attachments" / pid
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
    return attachments


def _post_row(post_file, channel, full=True):
    """A post as a payload row. `full=False` truncates the body to the
    feed snippet length (read --message gives the full body)."""
    fm, body = parse_post(post_file.read_text(encoding="utf-8"))
    pid = fm.get("id") or _stem_id(post_file)
    body = body.strip()
    truncated = False
    if not full:
        width = _snippet_len()
        if len(body) > width:
            body = body[:width].rstrip() + " …"
            truncated = True
    return {
        "id": pid,
        "channel": channel,
        "reply_to": fm.get("reply_to"),
        "author": fm.get("author"),
        "alias": fm.get("alias"),
        "via": fm.get("via"),
        "model": fm.get("model"),
        "timestamp": fm.get("timestamp"),
        "truncated": truncated,
        "body": _wrap_untrusted(body, fm.get("author")),
        "attachments": _post_attachments(post_file, pid),
    }


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
    # Posts are written back as UTF-8; read the source the same way so a body
    # file with non-ASCII (em dash, CJK, emoji) doesn't mojibake or crash on
    # Windows, where the default text encoding is cp1252.
    return Path(args.body_file).read_text(encoding="utf-8")


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
    # Push rejected against an existing remote branch: someone pushed between
    # our fetch and our push. Rebase our local commits onto the new tip --
    # this PRESERVES any prior unpushed history (offline-mode posts, a send
    # whose push failed transiently), and distinct post files never conflict.
    git("rebase", "--abort", check=False)  # clear any half-finished rebase
    rb = git("rebase", f"origin/{branch}", check=False)
    if rb.returncode == 0:
        res2 = git("push", check=False)
        if res2.returncode == 0:
            return info
    else:
        git("rebase", "--abort", check=False)
    # Rebase conflicted (a same-file write, e.g. tasks.json) or the re-push
    # still lost a race. Fall back to a clean replay of THIS write -- but only
    # when our commit is the sole unpushed one, so we never silently discard
    # other local history (the old blind `reset --hard origin` ate offline
    # commits here).
    ahead = git("rev-list", "--count", f"origin/{branch}..HEAD",
                check=False).stdout.strip()
    if ahead not in ("", "0", "1"):
        die("push rejected and the local branch has unpushed commits that "
            "don't rebase cleanly onto origin; resolve manually (`git status` "
            "/ `git log`) so nothing is lost.")
    git("reset", "--hard", f"origin/{branch}")
    info = write_payload()
    _commit_all(build_message(info))
    res2 = git("push", check=False)
    if res2.returncode != 0:
        die(f"push rejected after rebase-retry: {res2.stderr.strip()}")
    return info


# ---- server metadata (.pair-pressure/server.json: name + admins) ----

def _server_meta():
    """Tolerant read of the active server's server.json."""
    try:
        return read_json(repo_path() / ".pair-pressure" / "server.json", {})
    except (OSError, ValueError):
        return {}


def _is_admin(me):
    """Advisory admin check. An empty/missing admins list means the server
    is unmanaged — everyone may administer it."""
    admins = _server_meta().get("admins") or []
    return not admins or me in admins


def _require_admin(action):
    me = author()
    if not _is_admin(me):
        admins = ", ".join(_server_meta().get("admins") or [])
        die(f"{action} is admin-only on this server (admins: {admins}). "
            "Enforcement is advisory — git cannot block a hostile clone — "
            "but pp respects it.")
    return me


# ---- channel helpers + core verbs (send/read/use/where/alias/dm/tasks) ----

def _channel_meta(ch_dir):
    """Tolerant read of channel.json."""
    try:
        return read_json(ch_dir / "channel.json", {"name": ch_dir.name})
    except (OSError, ValueError):
        return {"name": ch_dir.name}


def _channel_archived(ch_dir):
    return bool(_channel_meta(ch_dir).get("archived"))


def _channel_visible(ch_dir, me, include_archived=False):
    """Whether `me` should see this channel in listings/feeds. Private
    channels are member-only (advisory); archived channels are hidden unless
    asked for."""
    meta = _channel_meta(ch_dir)
    if meta.get("private") and me not in (meta.get("members") or []):
        return False
    if meta.get("archived") and not include_archived:
        return False
    return True


def _active_channel_dirs(channels_root, me=None):
    """Visible channel dirs, sorted. Skips archived channels and private
    channels `me` is not a member of."""
    if me is None:
        me = os.environ.get("PAIR_PRESSURE_AUTHOR")
    if not channels_root.exists():
        return []
    return [p for p in sorted(channels_root.iterdir())
            if p.is_dir() and _channel_visible(p, me)]


def _require_channel_member(meta, name, me):
    if meta.get("private") and me not in (meta.get("members") or []):
        die(f"channel '{name}' is a private group you are not a member of.")


def _require_writable_channel(meta, name, me):
    """Gate every write verb (send, task new/done): the channel must exist,
    admit `me` (private membership), and not be archived. One place so a new
    write verb can't forget the archived check the way the task verbs did."""
    _require_channel_member(meta, name, me)
    if meta.get("archived"):
        die(f"channel '{name}' is archived; an admin can restore it with "
            f"`pp channel unarchive {name}`.")


def _snippet_len():
    """Feed body truncation length: env PAIR_PRESSURE_SNIPPET_LEN > config
    snippet_len > 240. `pp read --message <id>` always gives the full body."""
    raw = os.environ.get("PAIR_PRESSURE_SNIPPET_LEN")
    if raw is None or str(raw).strip() == "":
        raw = _config_load().get("snippet_len")
    try:
        n = int(str(raw).strip())
        return n if n > 0 else 240
    except (TypeError, ValueError):
        return 240


def cmd_send(args):
    resolved = resolve_active(args)
    _activate(args)
    maybe_pull()
    ch = channel_dir(args.channel)
    meta = _channel_meta(ch)
    me = author()
    _require_writable_channel(meta, args.channel, me)
    body = read_body(args)
    if not body.strip():
        die("refusing to post an empty message")

    csource = resolved["sources"].get("channel")
    where = _where_line(args.server, args.channel)
    banner = f"→ {where}"
    if csource != "arg":
        banner += f"  [channel from {csource}]"
    print(banner, file=sys.stderr)

    reply_to = getattr(args, "reply_to", None)
    if reply_to:
        hit = _find_post_by_id(reply_to)
        if hit is None:
            die(f"--reply-to: no post matched '{reply_to}'")
        if "ambiguous" in hit:
            die(f"--reply-to '{reply_to}' matches multiple posts; use a "
                "longer id")
        reply_to = hit["id"]
    via = getattr(args, "via", None) or "claude-code"

    def write_payload():
        pid = post_id()
        shard = _posts_root(ch) / _shard_for(pid)
        shard.mkdir(parents=True, exist_ok=True)
        attached = _process_attachments(
            body, shard, pid, getattr(args, "attachments", None) or [])
        pf = shard / f"{pid}.md"
        pf.write_text(dump_slim(
            by=by_for_via(via, args), via=via,
            model=getattr(args, "model", None),
            pid=pid, reply_to=reply_to, body=attached,
        ), encoding="utf-8")
        return {"post_id": pid,
                "path": str(pf.relative_to(repo_path())).replace("\\", "/")}

    def msg(info):
        return (f"#{args.channel}: post by {by_for_via(via, args)} "
                f"[via {_short_via(via)}]")

    info = push_with_retry(write_payload, msg)
    _state_save(server=args.server, channel=args.channel, source="send")
    out({"ok": True, "where": where, "server": args.server,
         "channel": args.channel, "channel_source": csource, **info})


def _emit_read(args, payload):
    if getattr(args, "pretty", False):
        _render_chat(payload)
    else:
        out(payload)


def _find_post_by_id(query):
    """Find a post by full id or substring handle across visible channels.
    Returns a full post row, {"ambiguous": [...]}, or None."""
    q = str(query).strip().lstrip("·")
    if not q:
        return None
    me = os.environ.get("PAIR_PRESSURE_AUTHOR")
    root = repo_path() / "channels"
    exact, partial = [], []
    for ch_dir in _active_channel_dirs(root, me):
        for pf in _post_files(ch_dir):
            sid = _stem_id(pf)
            if sid == q:
                exact.append((pf, ch_dir.name))
            elif q in sid:
                partial.append((pf, ch_dir.name))
    hits = exact or partial
    if len(hits) == 1:
        pf, chname = hits[0]
        return _post_row(pf, chname, full=True)
    if len(hits) > 1:
        return {"ambiguous": [
            {"id": _stem_id(pf), "channel": c} for pf, c in hits[:20]]}
    return None


def cmd_read(args):
    resolved = resolve_active(args)
    _activate(args)
    _watch_ack()  # reading clears the unread badge (best-effort)
    if not getattr(args, "no_pull", False):
        maybe_pull()
    me = author()
    where = _where_line(args.server, resolved["channel"])
    limit = getattr(args, "limit", None) or 30
    since = getattr(args, "since", None)

    mid = getattr(args, "message_id", None)
    if mid:
        hit = _find_post_by_id(mid)
        if hit is None:
            _emit_read(args, {"view": "message", "matched": False,
                              "query": mid, "where": where})
        elif "ambiguous" in hit:
            _emit_read(args, {"view": "ambiguous_message", "query": mid,
                              "matches": hit["ambiguous"], "where": where})
        else:
            _emit_read(args, {"view": "message", "post": hit, "where": where})
        return

    root = repo_path() / "channels"
    target = getattr(args, "target", None)
    if target:
        ch_dir = _safe_subpath(root, target)
        if not ch_dir.is_dir():
            die(f"channel '{target}' does not exist (try `pp channels`)")
        _require_channel_member(_channel_meta(ch_dir), target, me)
        files = list(reversed(_post_files_desc(ch_dir, limit=limit)))
        posts = [_post_row(pf, target, full=False) for pf in files]
        if since:
            posts = [p for p in posts if (p.get("timestamp") or "") >= since]
        _emit_read(args, {"view": "channel", "channel": target,
                          "where": where, "posts": posts})
        return

    rows = []
    for ch_dir in _active_channel_dirs(root, me):
        for pf in _post_files_desc(ch_dir, limit=limit):
            rows.append((pf, ch_dir.name))
    rows.sort(key=lambda t: _stem_id(t[0]))
    rows = rows[-limit:]
    posts = [_post_row(pf, c, full=False) for pf, c in rows]
    if since:
        posts = [p for p in posts if (p.get("timestamp") or "") >= since]
    _emit_read(args, {"view": "feed", "where": where, "posts": posts})


def _switch_to(server=None, channel=None):
    """Validate + persist a server/channel switch; returns the result row.
    Shared by `pp use` and `pp server use`."""
    if server:
        if not _server_entry(server):
            die(f"server '{server}' is not registered "
                "(try `pp server list`).")
        _server_path(server)  # validates clone + schema
        if not channel:
            channel = _default_channel()
    if channel:
        name = server
        if not name:
            name, _src = resolve_server_name(None)
            if not name:
                die("no server configured; run `pp server add <name> <url>`.")
        path = _server_path(name)
        ch = _safe_subpath(path / "channels", channel)
        if not ch.is_dir():
            die(f"channel '{channel}' does not exist on {name} "
                "(try `pp channels`)")
        meta = _channel_meta(ch)
        _require_channel_member(meta, channel, author())
        if meta.get("archived"):
            die(f"channel '{channel}' is archived; an admin can restore it "
                f"with `pp channel unarchive {channel}`.")
    _state_save(server=server, channel=channel, source="use")
    sname, _src = resolve_server_name(None)
    sess, glob = _state_load()
    cname = ((sess or {}).get("channel") or (glob or {}).get("channel")
             or _default_channel())
    line = _where_line(sname, cname)
    print(f"now in: {line}", file=sys.stderr)
    return {"ok": True, "where": line, "server": sname, "channel": cname,
            "alias": effective_alias()}


def cmd_use(args):
    server = None
    channel = None
    for tok in args.target:
        if tok.startswith("#"):
            channel = tok[1:]
        elif server is None:
            server = tok
        else:
            die("usage: pp use <server> | #<channel> | <server> #<channel>")
    if not server and not channel:
        die("usage: pp use <server> | #<channel> | <server> #<channel>")
    out(_switch_to(server=server, channel=channel))


def cmd_where(args):
    sess, glob = _state_load()
    sname, ssrc = resolve_server_name(None)
    if sess and sess.get("channel"):
        channel, csrc = sess["channel"], "session"
    elif glob and glob.get("channel"):
        channel, csrc = glob["channel"], "global"
    else:
        channel, csrc = _default_channel(), "default"
    line = _where_line(sname or "(no server)", channel)
    if getattr(args, "pretty", False):
        print(line)
        return
    out({"where": line, "server": sname, "server_source": ssrc,
         "channel": channel, "channel_source": csrc,
         "alias": effective_alias(), "session_id": _session_id()})


def _alias_in_use_elsewhere(name):
    """Another recent session (last hour) persisted this alias. Advisory
    collision check — there is no central session registry."""
    sessions_dir = _PP_HOME / "sessions"
    if not sessions_dir.is_dir():
        return False
    mine = _state_path_session()
    cutoff = time.time() - 3600
    for p in sessions_dir.iterdir():
        if not p.is_file() or p.suffix != ".json":
            continue
        if mine is not None and p == mine:
            continue
        try:
            if p.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        data = _state_load_one(p) or {}
        if (data.get("alias") or "").strip() == name:
            return True
    return False


_ALIAS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")


def cmd_alias(args):
    name = (getattr(args, "name", None) or "").strip()
    if not name:
        out({"alias": effective_alias(),
             "env": os.environ.get("PAIR_PRESSURE_ALIAS") or None,
             "session_id": _session_id()})
        return
    if not _ALIAS_RE.match(name):
        # The alias is written into the space-delimited single-line slim
        # header (`by: author/alias ...`); whitespace or `/` would not
        # round-trip through parse_slim and would corrupt the attribution.
        die("alias must start with a letter and contain only letters, "
            "digits, '_' or '-' (max 32 chars)")
    payload = {"ok": True, "alias": name,
               "persisted": "session" if _session_id() else "global"}
    if _alias_in_use_elsewhere(name):
        payload["warning"] = (f"alias '{name}' was set by another recent "
                              "session — pick a different one to avoid "
                              "confusion")
    if os.environ.get("PAIR_PRESSURE_ALIAS"):
        payload["warning_env"] = ("PAIR_PRESSURE_ALIAS env is set and beats "
                                  "the persisted alias until unset")
    _state_save(alias=name, source="alias")
    out(payload)


def cmd_dm(args):
    _activate(args)
    maybe_pull()
    me = author()
    users = sorted({me, *[u.strip() for u in args.users if u.strip()]})
    if len(users) < 2:
        die("dm needs at least one other user: pp dm <user> [<user> ...]")
    name = (getattr(args, "name", None)
            or "dm-" + "-".join(slugify(u)[:16] for u in users))
    if not _CHANNEL_NAME_RE.match(name):
        die(f"channel name must match {_CHANNEL_NAME_RE.pattern}")
    root = repo_path() / "channels"
    ch = _safe_subpath(root, name)
    warning = ("DM content is NOT encrypted — it is plain text in the git "
               "repo; anyone with repo access can read the raw files.")
    if ch.is_dir():
        meta = _channel_meta(ch)
        if not meta.get("private"):
            die(f"'{name}' already exists and is a public channel")
        if me not in (meta.get("members") or []):
            die(f"'{name}' exists but you are not a member")
        _state_save(channel=name, source="dm")
        out({"ok": True, "created": False, "channel": name,
             "members": meta.get("members"), "warning": warning})
        return
    print(f"creating private group #{name} — {warning}", file=sys.stderr)

    def write_payload():
        ch.mkdir(parents=True, exist_ok=True)
        write_json(ch / "channel.json", {
            "name": name,
            "description": "private group chat",
            "private": True,
            "members": users,
            "archived": False,
            "created_by": me,
            "created_at": now_iso(),
        })
        return {"channel": name, "members": users}

    info = push_with_retry(write_payload, lambda i: f"dm: create #{name}")
    _state_save(channel=name, source="dm")
    out({"ok": True, "created": True, "warning": warning, **info})


# ---- tasks: a minimal per-channel checklist (tasks.json) ----

def _tasks_path(ch_dir):
    return ch_dir / "tasks.json"


def _tasks_load(ch_dir):
    try:
        data = read_json(_tasks_path(ch_dir), {"next_id": 1, "tasks": []})
    except (OSError, ValueError):
        data = {"next_id": 1, "tasks": []}
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        data = {"next_id": 1, "tasks": []}
    data.setdefault("next_id", 1)
    return data


def _match_task(tasks, ref):
    """Resolve a task ref: '#3'/'3' = task id; otherwise title substring.
    Returns a task dict, a list (ambiguous), or None."""
    s = str(ref).strip()
    if s.startswith("#"):
        s = s[1:]
    if s.isdigit():
        tid = int(s)
        for t in tasks:
            if t.get("id") == tid:
                return t
        return None
    sl = s.lower()
    hits = [t for t in tasks if sl in str(t.get("title", "")).lower()]
    if len(hits) == 1:
        return hits[0]
    return hits or None


def cmd_task_new(args):
    resolve_active(args)
    _activate(args)
    maybe_pull()
    ch = channel_dir(args.channel)
    _require_writable_channel(_channel_meta(ch), args.channel, author())
    title = args.title.strip()
    if not title:
        die("task title must not be empty")

    def write_payload():
        # Re-read inside the closure: a rebase-retry replays this against
        # the fresh tree, so a concurrent task new can't be lost.
        data = _tasks_load(ch)
        tid = int(data["next_id"])
        task = {"id": tid, "title": title, "status": "open",
                "by": by_token(), "at": now_iso(),
                "done_by": None, "done_at": None}
        data["tasks"].append(task)
        data["next_id"] = tid + 1
        write_json(_tasks_path(ch), data)
        return {"task": task}

    info = push_with_retry(
        write_payload,
        lambda i: f"#{args.channel}: task #{i['task']['id']}: {title}")
    out({"ok": True, "channel": args.channel,
         "where": _where_line(args.server, args.channel), **info})


def cmd_task_list(args):
    resolve_active(args)
    _activate(args)
    if not getattr(args, "no_pull", False):
        maybe_pull()
    ch = channel_dir(args.channel)
    _require_channel_member(_channel_meta(ch), args.channel, author())
    tasks = _tasks_load(ch)["tasks"]
    if not getattr(args, "all", False):
        tasks = [t for t in tasks if t.get("status") != "done"]
    out({"where": _where_line(args.server, args.channel),
         "channel": args.channel, "tasks": tasks})


def cmd_task_done(args):
    resolve_active(args)
    _activate(args)
    maybe_pull()
    ch = channel_dir(args.channel)
    _require_writable_channel(_channel_meta(ch), args.channel, author())

    def write_payload():
        data = _tasks_load(ch)
        t = _match_task(data["tasks"], args.ref)
        if t is None:
            die(f"no task matched '{args.ref}' in #{args.channel} "
                "(try `pp task list`)")
        if isinstance(t, list):
            die(f"'{args.ref}' matches {len(t)} tasks — use the #id")
        if t.get("status") == "done":
            return {"task": t, "already_done": True}
        t["status"] = "done"
        t["done_by"] = by_token()
        t["done_at"] = now_iso()
        write_json(_tasks_path(ch), data)
        return {"task": t}

    info = push_with_retry(
        write_payload,
        lambda i: f"#{args.channel}: done task #{i['task']['id']}")
    out({"ok": True, "channel": args.channel, **info})


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
    """Identity + location status. Works pre-configuration (never dies)."""
    saved = _read_saved_env()
    active = {
        "PAIR_PRESSURE_AUTHOR": os.environ.get("PAIR_PRESSURE_AUTHOR") or None,
        "PAIR_PRESSURE_REPO":   os.environ.get("PAIR_PRESSURE_REPO")   or None,
        "PAIR_PRESSURE_ALIAS":  os.environ.get("PAIR_PRESSURE_ALIAS")  or None,
    }
    author_ok = bool(active.get("PAIR_PRESSURE_AUTHOR"))
    servers = [s.get("name") for s in _servers_list()]
    sname, ssrc = resolve_server_name(None)
    if not author_ok and not servers and not active.get("PAIR_PRESSURE_REPO"):
        verdict = "not_configured"
        message = "Not configured. Run `pp-setup` to set up."
    elif saved.get("PAIR_PRESSURE_AUTHOR") and not author_ok:
        verdict = "needs_restart"
        message = ("Saved but not yet loaded — restart your CLI session to "
                   "pick up the env vars.")
    elif not author_ok:
        verdict = "needs_author"
        message = "PAIR_PRESSURE_AUTHOR is not set. Run `pp-setup`."
    elif not sname:
        verdict = "needs_server"
        message = "No server registered. Run `pp server add <name> <url>`."
    else:
        verdict = "ready"
        message = "Ready."
    sess, glob = _state_load()
    channel = ((sess or {}).get("channel") or (glob or {}).get("channel")
               or _default_channel())
    out({
        "saved": saved,
        "active": active,
        "verdict": verdict,
        "message": message,
        "alias": effective_alias(),
        "servers": servers,
        "where": (f"{sname} #{channel}" if sname else None),
        "server": sname,
        "server_source": ssrc,
        "channel": channel,
        "session_id": _session_id(),
        "offline": {
            "active": _offline(),
            "config": _config_load().get("offline", False),
            "env": os.environ.get("PAIR_PRESSURE_OFFLINE"),
        },
    })


def _snippet(text, query, width=160):
    """One-line snippet from `text` containing `query` (case-insensitive),
    or the first content line if no match."""
    lower = text.lower()
    q = query.lower()
    idx = lower.find(q)
    if idx < 0:
        _, body = parse_post(text)
        for line in (body or "").splitlines():
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
    _activate(args)
    if not args.no_pull:
        maybe_pull()
    repo = repo_path()
    me = os.environ.get("PAIR_PRESSURE_AUTHOR")
    visible = {d.name for d in _active_channel_dirs(repo / "channels", me)}

    ql = args.query.lower()
    paths = set()
    res = git("grep", "-l", "-i", "-F", "-e", args.query, "--",
              "channels/", check=False)
    if res.returncode == 0:
        paths.update(p for p in res.stdout.splitlines() if p.endswith(".md"))
    if not paths:
        # Manual walk fallback (e.g. unborn branch with no commits).
        root = repo / "channels"
        for ch_dir in (root.iterdir() if root.exists() else []):
            if not ch_dir.is_dir():
                continue
            for p in _post_files(ch_dir):
                try:
                    if ql in p.read_text(encoding="utf-8",
                                         errors="replace").lower():
                        paths.add(str(p.relative_to(repo)).replace("\\", "/"))
                except OSError:
                    continue

    results = []
    for rel in sorted(paths):
        p = repo / rel
        parts = Path(rel).parts
        # channels/<ch>/posts/<shard>/<id>.md
        if len(parts) != 5 or parts[0] != "channels" or parts[2] != "posts":
            continue
        channel = parts[1]
        if channel not in visible:
            continue
        if args.channel and channel != args.channel:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if ql not in text.lower():
            continue
        fm, _body = parse_post(text)
        if args.author and fm.get("author") != args.author:
            continue
        results.append({
            "channel": channel,
            "post_id": fm.get("id", _stem_id(p)),
            "author": fm.get("author"),
            "alias": fm.get("alias"),
            "timestamp": fm.get("timestamp"),
            "snippet": _snippet(text, args.query),
        })

    results.sort(key=lambda r: r.get("timestamp") or "", reverse=True)
    if args.limit:
        results = results[: args.limit]
    out(results)


def cmd_unread(args):
    """New posts not authored by you. Default mode reuses the watcher's
    baseline (watch-state.json) WITHOUT persisting marker advances, so it
    never clears the badge. `--since <ISO>` counts every post at/after a
    timestamp instead. `--all` spans every registered server. `--ack` clears
    this session's unread bucket."""
    me = os.environ.get("PAIR_PRESSURE_AUTHOR")
    if getattr(args, "all", False):
        servers = [s.get("name") for s in _servers_list() if s.get("name")]
    else:
        name, _src = resolve_server_name(getattr(args, "server", None))
        servers = [name] if name else []
    items = []
    since = getattr(args, "since", None)
    state = None if since else _watch_state_load()
    for srv in servers:
        try:
            if since:
                entry = _server_entry(srv)
                path = Path(entry["path"]).expanduser() if entry else None
                if not path or not path.is_dir():
                    continue
                for ch_dir in _active_channel_dirs(path / "channels", me):
                    for pf in _post_files(ch_dir):
                        ts = _id_to_iso(_stem_id(pf))
                        if not ts or ts < since:
                            continue
                        fm, _b = parse_post(pf.read_text(
                            encoding="utf-8", errors="replace"))
                        au = fm.get("author")
                        if au and au != me:
                            items.append({"server": srv,
                                          "channel": ch_dir.name,
                                          "post_id": _stem_id(pf),
                                          "author": au})
            else:
                items.extend(_scan_server_new(srv, state))
        except SystemExit:
            continue
        except Exception:
            continue
    res = {"count": len(items), "items": items}
    try:
        buckets = read_json(_watch_unread_path(), {})
        if buckets:
            res["buckets"] = buckets
    except Exception:
        pass
    if getattr(args, "ack", False):
        _watch_ack()
        res["acked"] = True
    out(res)


_CHANNEL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def cmd_channel_new(args):
    """Create a channel (admin-only, advisory). Re-creating an existing
    channel is a no-op; an archived one is revived."""
    _activate(args)
    maybe_pull()
    _require_admin("channel new")
    name = args.name
    if not _CHANNEL_NAME_RE.match(name):
        die(f"channel name must match {_CHANNEL_NAME_RE.pattern}")

    ch = repo_path() / "channels" / name
    if ch.is_dir():
        existing = _channel_meta(ch)
        if existing.get("archived"):
            def unarchive_payload():
                meta = _channel_meta(ch)
                meta.pop("archived", None)
                write_json(ch / "channel.json", meta)
                return {"ok": True, "created": False, "unarchived": True,
                        "channel": meta.get("name", name)}

            out(push_with_retry(
                unarchive_payload,
                lambda i: f"#{name}: unarchive-channel by {by_token()}"))
            return
        out({"ok": True, "created": False,
             "channel": existing.get("name", name)})
        return

    def write_payload():
        # Re-check inside the retry: a parallel admin might have created the
        # channel during our rebase. If so, treat as success.
        if ch.is_dir():
            return {"ok": True, "created": False, "channel": name}
        ch.mkdir(parents=True)
        write_json(ch / "channel.json", {
            "name": name,
            "description": args.description or "",
            "archived": False,
            "created_by": author(),
            "created_at": now_iso(),
        })
        return {"ok": True, "created": True, "channel": name}

    out(push_with_retry(
        write_payload, lambda i: f"#{name}: new-channel by {by_token()}"))


def _set_channel_archived(args, archived):
    """Flip channel.json `archived` (admin-only, advisory). Archiving hides
    a channel from list/feed/read/watch while keeping every post; unarchiving
    restores it. Idempotent (a no-op write yields no commit)."""
    _activate(args)
    maybe_pull()
    _require_admin("channel archive/unarchive")
    name = args.name
    ch = repo_path() / "channels" / name
    if not ch.is_dir():
        die(f"channel '{name}' does not exist")

    def write_payload():
        meta = _channel_meta(ch)
        if archived:
            meta["archived"] = True
        else:
            meta.pop("archived", None)
        write_json(ch / "channel.json", meta)
        return {"ok": True, "channel": meta.get("name", name),
                "archived": bool(archived)}

    verb = "archive" if archived else "unarchive"
    out(push_with_retry(
        write_payload, lambda i: f"#{name}: {verb}-channel by {by_token()}"))


def cmd_channel_archive(args):
    _set_channel_archived(args, True)


def cmd_channel_unarchive(args):
    _set_channel_archived(args, False)


def _valid_server_name(name):
    return bool(_CHANNEL_NAME_RE.match(name or ""))


# ---- server management (a server = a registered chat repo clone) ----

def _default_server_clone_dir(name):
    return _PP_HOME / "servers" / name


def _is_chat_repo(path):
    """A path is an initialized chat repo if it has the v3 marker files."""
    pp = Path(path) / ".pair-pressure"
    return (pp / "schema-version").exists() or (pp / "server.json").exists()


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
    git("config", "user.email", f"{au}@pair-pressure.local", cwd=cwd,
        check=False)


def _pp_init_argv():
    """argv prefix that runs pp-init. Prefer the console script on PATH (the
    robust path for wheel installs). Fall back to the bundled script at
    _data/scripts/pp-init.py -- but that only exists in the package tree, not
    in the ~/.claude/skills copy (which ships _data/skill only), so when it's
    missing, run the installed package module instead."""
    if shutil.which("pp-init"):
        return ["pp-init"]
    bundled = (Path(__file__).resolve().parent.parent.parent
               / "scripts" / "pp-init.py")
    if bundled.is_file():
        return [sys.executable, str(bundled)]
    return [sys.executable, "-m", "pair_pressure._init"]


def cmd_server_add(args):
    """Register a server: clone (or adopt --path) + optional init + record.

    `pp server add <name> <url> [--path DIR] [--no-clone]`

    A server IS a chat repo clone (Discord-style: one GitHub repo = one
    server). The clone lands in ~/.pair-pressure/servers/<name> by default.
    If the remote is an uninitialized repo, it is bootstrapped with `pp-init`
    and the default branch is pushed. The first server added becomes the
    registry default."""
    name = args.name
    if not _valid_server_name(name):
        die("server name must match ^[a-z0-9][a-z0-9._-]{0,63}$")
    if _server_entry(name):
        die(f"server '{name}' is already registered (try `pp server list`)")

    dest = (Path(args.path).expanduser() if args.path
            else _default_server_clone_dir(name))
    adopt = bool(args.path) or args.no_clone

    if adopt:
        if not (dest / ".git").exists():
            die(f"--path {dest} is not a git repository")
    else:
        if _offline():
            die("cannot clone in offline mode; run `pp offline false` first, "
                "or register an existing clone with `pp server add <name> "
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

    if not _is_chat_repo(dest):
        # Bootstrap the chat structure (server.json + channels/general).
        _ensure_git_identity(dest)
        argv = _pp_init_argv() + [str(dest), "--force", "--name", name]
        sub_env = os.environ.copy()
        sub_env.setdefault("PAIR_PRESSURE_AUTHOR", author())
        ir = subprocess.run(argv, env=sub_env, capture_output=True, text=True)
        if ir.returncode != 0:
            die(f"pp-init failed: {ir.stderr.strip() or ir.stdout.strip()}")
        if not _offline():
            branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=dest,
                         check=False).stdout.strip() or "main"
            git("push", "-u", "origin", branch, cwd=dest, check=False)
    else:
        _require_schema_v3(dest)

    reg = _servers_load()
    reg.setdefault("servers", []).append({
        "name": name,
        "path": str(dest.resolve()),
        "url": args.url,
        "added_at": now_iso(),
        "added_by": author(),
    })
    reg.setdefault("schema_version", 2)
    if not reg.get("default"):
        reg["default"] = name
    _servers_save(reg)
    out({"ok": True, "name": name, "path": str(dest.resolve()),
         "url": args.url, "default": reg.get("default") == name})


def cmd_server_list(args):
    """List registered servers; `active` marks the one in use."""
    active, _src = resolve_server_name(None)
    rows = []
    for s in _servers_list():
        path = s.get("path")
        rows.append({
            "name": s.get("name"),
            "path": path,
            "url": s.get("url") or s.get("remote"),
            "exists": bool(path and
                           (Path(path).expanduser() / ".git").exists()),
            "active": s.get("name") == active,
        })
    out({"servers": rows, "active": active,
         "default": _servers_load().get("default")})


def cmd_server_use(args):
    """Switch the active server (same as `pp use <server>`)."""
    out(_switch_to(server=args.name))


def cmd_server_remove(args):
    """Unregister a server. --yes required. --delete-clone also removes the
    on-disk clone, but ONLY when it lives under ~/.pair-pressure/servers/."""
    name = args.name
    entry = _server_entry(name)
    if not entry:
        die(f"server '{name}' is not registered")
    if not args.yes:
        die("refusing to remove without --yes")
    if args.delete_clone:
        raw = entry.get("path") or ""
        if not raw:
            die("refusing --delete-clone: registry entry has no path")
        path = Path(raw).resolve()
        root = (_PP_HOME / "servers").resolve()
        if root in path.parents:
            shutil.rmtree(path, ignore_errors=True)
        else:
            die(f"refusing --delete-clone: {path} is outside "
                f"{root} (remove it manually)")
    reg = _servers_load()
    reg["servers"] = [s for s in reg.get("servers", [])
                      if s.get("name") != name]
    if reg.get("default") == name:
        reg["default"] = (reg["servers"][0]["name"] if reg["servers"]
                          else None)
    _servers_save(reg)
    out({"ok": True, "removed": name, "default": reg.get("default")})


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
                  "at": now_iso()}
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
    dying. Covers env-var installs and the server registry."""
    if os.environ.get("PAIR_PRESSURE_REPO"):
        return True
    return bool(_servers_list())


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
        # The value lands inside a PowerShell single-quoted string literal
        # ('...'), so a bare ' would break out and let attacker-controlled
        # post-author text run as PowerShell in the watcher daemon. Doubling
        # ' is the PS single-quote escape. We do NOT XML-escape &<> here:
        # the string is handed to CreateTextNode, which stores literal text
        # and lets the toast serializer escape it -- pre-escaping would
        # double it (R&D -> "R&amp;D" shown verbatim).
        return s.replace("'", "''")

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


def _hidden_channel_names(repo, me, ref=None):
    """Channel names the watcher must not notify about: archived channels
    and private channels `me` is not a member of. With `ref`, channel.json
    is read from origin/<branch> via `git show` so a stale working tree can
    never leak a DM toast."""
    metas = {}
    if ref:
        r = git("ls-tree", "-r", "--name-only", ref, cwd=repo, check=False)
        names = r.stdout.splitlines() if r.returncode == 0 else []
        for path in names:
            m = re.match(r"channels/([^/]+)/channel\.json$", path)
            if not m:
                continue
            sr = git("show", f"{ref}:{path}", cwd=repo, check=False)
            try:
                metas[m.group(1)] = (json.loads(sr.stdout)
                                     if sr.returncode == 0 else {})
            except json.JSONDecodeError:
                metas[m.group(1)] = {}
    else:
        root = repo / "channels"
        if root.is_dir():
            for ch in root.iterdir():
                if not ch.is_dir():
                    continue
                try:
                    metas[ch.name] = read_json(ch / "channel.json", {})
                except (OSError, ValueError):
                    metas[ch.name] = {}
    hidden = set()
    for name, meta in metas.items():
        if not isinstance(meta, dict):
            continue
        if meta.get("archived"):
            hidden.add(name)
        elif meta.get("private") and me not in (meta.get("members") or []):
            hidden.add(name)
    return hidden


def _scan_server_new(server, state):
    """Return list of {server,channel,post_id,author} for posts newer than
    the per-channel marker, not authored by us. Advances `state` markers in
    place. Online: fetch + diff origin/<branch> (working tree untouched).
    Offline: scan working-tree files. Never clones — a registered server
    whose clone is missing is skipped."""
    me = os.environ.get("PAIR_PRESSURE_AUTHOR")
    entry = _server_entry(server)
    if not entry or not entry.get("path"):
        return []
    repo = Path(entry["path"]).expanduser()
    if not (repo / ".git").exists():
        return []
    posts = []  # (channel, post_id, reader)
    ref = None
    if not _offline():
        branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo,
                     check=False).stdout.strip()
        if branch and branch != "HEAD":
            git("fetch", "origin", branch, cwd=repo, check=False)
            if git("rev-parse", "--verify", f"origin/{branch}", cwd=repo,
                   check=False).returncode == 0:
                ref = f"origin/{branch}"
    hidden = _hidden_channel_names(repo, me, ref=ref)
    if ref:
        r = git("ls-tree", "-r", "--name-only", ref, cwd=repo, check=False)
        names = r.stdout.splitlines() if r.returncode == 0 else []
        for path in names:
            m = re.match(
                r"channels/([^/]+)/posts/(\d{4}-\d{2})/(\d{8}T\d{9}Z)\.md$",
                path)
            if not m:
                continue
            ch, pid = m.group(1), m.group(3)
            if ch in hidden:
                continue
            posts.append((ch, pid, ("git", ref, path)))
    else:
        ch_root = repo / "channels"
        if ch_root.is_dir():
            for ch_dir in sorted(p for p in ch_root.iterdir() if p.is_dir()):
                if ch_dir.name in hidden:
                    continue
                for pf in _post_files(ch_dir):
                    posts.append((ch_dir.name, _stem_id(pf), ("file", pf)))
    by_key = {}
    for ch, pid, reader in posts:
        by_key.setdefault(ch, []).append((pid, reader))
    new = []
    for ch, items in by_key.items():
        items.sort(key=lambda x: x[0])
        key = f"{server}/{ch}"
        marker = state.get(key)
        cur_max = items[-1][0]
        if marker is None:
            state[key] = cur_max  # baseline on first sight, no backlog flood
            continue
        for pid, reader in items:
            if pid <= marker:
                continue
            try:
                if reader[0] == "git":
                    _, rref, rpath = reader
                    sr = git("show", f"{rref}:{rpath}", cwd=repo, check=False)
                    text = sr.stdout if sr.returncode == 0 else ""
                else:
                    text = reader[1].read_text(encoding="utf-8",
                                               errors="replace")
                fm, _ = parse_post(text)
                au = fm.get("author")
            except Exception:
                au = None
            if au and au != me:
                new.append({"server": server, "channel": ch,
                            "post_id": pid, "author": au})
        state[key] = max(cur_max, marker)
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
            servers = [s.get("name") for s in _servers_list()]
            for srv in servers:
                if not srv:
                    continue
                try:
                    fresh.extend(_scan_server_new(srv, state))
                except Exception as e:
                    _watch_log(f"scan error server={srv}: {e!r}")
            # Drop pre-1.0 markers (server/channel/thread keys) so the file
            # doesn't accumulate dead entries; v3 keys are server/channel.
            state = {k: v for k, v in state.items()
                     if isinstance(k, str) and k.count("/") == 1}
            _watch_state_save(state)
            if fresh:
                n = len(fresh)
                last = fresh[-1]
                where = f"{last.get('server', '')} #{last['channel']}".strip()
                if n == 1:
                    title = f"pair-pressure: new message in {where}"
                    msg = f"{last['author']} posted in {where}"
                else:
                    title = f"pair-pressure: {n} new messages"
                    msg = f"latest: {last['author']} in {where}"
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


def _via_arg(value):
    """Validate --via: claude-code | human | mcp | mcp:<client>. The
    `mcp:<client>` form tags which MCP client composed the post (Codex,
    Cursor, ...) and is preserved verbatim through the slim header, so the
    client tag must be a bare token (no whitespace, which would break the
    single-line `by:` header)."""
    if value in ("claude-code", "human", "mcp"):
        return value
    if value.startswith("mcp:"):
        client = value[4:]
        if client and not any(c.isspace() for c in client) and "/" not in client:
            return value
    raise argparse.ArgumentTypeError(
        "via must be claude-code, human, mcp, or mcp:<client> "
        "(client = a bare token, no spaces or '/')")


def main():
    p = argparse.ArgumentParser(prog="pp", description="pair-pressure CLI")
    p.add_argument("--version", action="version",
                   version=f"pair-pressure {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("send", help="post a message to the active channel")
    sp.add_argument("--channel", default=None,
                    help="target channel (default: active channel)")
    sp.add_argument("--reply-to", dest="reply_to", default=None,
                    help="post id (or unique substring) this replies to")
    sp.add_argument("--body-file", default="-",
                    help="path to message body, or '-' for stdin (default)")
    sp.add_argument("--via", default="claude-code", type=_via_arg,
                    help="claude-code | human | mcp | mcp:<client>")
    sp.add_argument("--model", default=None)
    sp.add_argument("--alias", default=None,
                    help="per-call alias override for AI posts")
    sp.add_argument("--attach", action="append", dest="attachments",
                    default=None, help="file to attach (repeatable)")
    _add_server_arg(sp)
    sp.set_defaults(func=cmd_send)

    sp = sub.add_parser(
        "read", help="feed (no arg), a channel, or one post via --message")
    sp.add_argument("target", nargs="?", default=None, help="channel name")
    sp.add_argument("--message", dest="message_id", default=None,
                    help="post id or trailing-substring handle — full body")
    sp.add_argument("--limit", type=int, default=30)
    sp.add_argument("--since", default=None, help="ISO timestamp filter")
    sp.add_argument("--no-pull", action="store_true")
    sp.add_argument("--pretty", action="store_true",
                    help="ANSI chat rendering instead of JSON")
    _add_server_arg(sp)
    sp.set_defaults(func=cmd_read)

    sp = sub.add_parser("channels", help="list channels")
    sp.add_argument("--all", action="store_true", help="include archived")
    sp.add_argument("--no-pull", action="store_true")
    _add_server_arg(sp)
    sp.set_defaults(func=cmd_channels)

    sp = sub.add_parser("channel",
                        help="channel admin: new/archive/unarchive")
    chsub = sp.add_subparsers(dest="channel_cmd", required=True)
    c = chsub.add_parser("new", help="create a channel (admin)")
    c.add_argument("name")
    c.add_argument("--description", default="")
    _add_server_arg(c)
    c.set_defaults(func=cmd_channel_new)
    c = chsub.add_parser("archive",
                         help="hide a channel, keep history (admin)")
    c.add_argument("name")
    _add_server_arg(c)
    c.set_defaults(func=cmd_channel_archive)
    c = chsub.add_parser("unarchive",
                         help="restore an archived channel (admin)")
    c.add_argument("name")
    _add_server_arg(c)
    c.set_defaults(func=cmd_channel_unarchive)

    sp = sub.add_parser("dm", help="create/open a private group chat")
    sp.add_argument("users", nargs="+", help="other member author names")
    sp.add_argument("--name", default=None,
                    help="channel name (default dm-<members>)")
    _add_server_arg(sp)
    sp.set_defaults(func=cmd_dm)

    sp = sub.add_parser("task", help="per-channel task checklist")
    tsub = sp.add_subparsers(dest="task_cmd", required=True)
    t = tsub.add_parser("new", help="add a task")
    t.add_argument("title")
    t.add_argument("--channel", default=None)
    _add_server_arg(t)
    t.set_defaults(func=cmd_task_new)
    t = tsub.add_parser("list", help="open tasks (--all includes done)")
    t.add_argument("--channel", default=None)
    t.add_argument("--all", action="store_true")
    t.add_argument("--no-pull", action="store_true")
    _add_server_arg(t)
    t.set_defaults(func=cmd_task_list)
    t = tsub.add_parser("done", help="mark a task done by #id or title")
    t.add_argument("ref")
    t.add_argument("--channel", default=None)
    _add_server_arg(t)
    t.set_defaults(func=cmd_task_done)

    sp = sub.add_parser("server",
                        help="server registry: list/add/use/remove")
    ssub = sp.add_subparsers(dest="server_cmd")
    s = ssub.add_parser("list", help="registered servers")
    s.set_defaults(func=cmd_server_list)
    s = ssub.add_parser("add", help="register a server (clone or adopt)")
    s.add_argument("name")
    s.add_argument("url")
    s.add_argument("--path", default=None,
                   help="adopt an existing clone at this path")
    s.add_argument("--no-clone", action="store_true")
    s.set_defaults(func=cmd_server_add)
    s = ssub.add_parser("use", help="switch the active server")
    s.add_argument("name")
    s.set_defaults(func=cmd_server_use)
    s = ssub.add_parser("remove", help="unregister a server")
    s.add_argument("name")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--delete-clone", dest="delete_clone",
                   action="store_true")
    s.set_defaults(func=cmd_server_remove)
    sp.set_defaults(func=cmd_server_list, server_cmd=None)

    sp = sub.add_parser("use", help="switch server and/or channel")
    sp.add_argument("target", nargs="+",
                    help="<server>, #<channel>, or <server> #<channel>")
    sp.set_defaults(func=cmd_use)

    sp = sub.add_parser("where", help="one line: where you are")
    sp.add_argument("--pretty", action="store_true",
                    help="print the bare location line")
    sp.set_defaults(func=cmd_where)

    sp = sub.add_parser("status", help="identity + location status")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("alias", help="show or set this session's AI alias")
    sp.add_argument("name", nargs="?", default=None)
    sp.set_defaults(func=cmd_alias)

    sp = sub.add_parser("search",
                        help="grep posts; filter by channel/author")
    sp.add_argument("--query", required=True)
    sp.add_argument("--channel", default=None)
    sp.add_argument("--author", default=None)
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--no-pull", action="store_true")
    _add_server_arg(sp)
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser(
        "unread", help="new posts not by you; --ack clears the badge")
    sp.add_argument("--all", action="store_true",
                    help="span all registered servers")
    sp.add_argument("--since", default=None,
                    help="ISO timestamp instead of the watcher baseline")
    sp.add_argument("--ack", action="store_true",
                    help="clear this session's unread bucket")
    _add_server_arg(sp)
    sp.set_defaults(func=cmd_unread)

    sp = sub.add_parser("pull", help="git pull --rebase --autostash")
    _add_server_arg(sp)
    sp.set_defaults(func=cmd_pull)

    sp = sub.add_parser("push", help="git push if ahead")
    _add_server_arg(sp)
    sp.set_defaults(func=cmd_push)

    sp = sub.add_parser("offline", help="show or set offline mode")
    sp.add_argument("state", nargs="?", choices=["true", "false"],
                    default=None)
    sp.set_defaults(func=cmd_offline)

    sp = sub.add_parser(
        "watch", help="background watcher: start/stop/status/interval/wire")
    wsub = sp.add_subparsers(dest="watch_cmd")
    w = wsub.add_parser("start")
    w.add_argument("--foreground", action="store_true",
                   help="run the poll loop inline (debug)")
    w = wsub.add_parser("stop")
    w = wsub.add_parser("status")
    w = wsub.add_parser("interval")
    w.add_argument("value", nargs="?", default=None,
                   help="e.g. 90, 90s, 5m, 1h; empty shows current")
    w = wsub.add_parser("wire")
    w.add_argument("--nudge", action="store_true",
                   help="also wire the in-prompt nudge (costs tokens)")
    w.add_argument("--undo", action="store_true",
                   help="restore the pre-pp statusline")
    sp.set_defaults(func=cmd_watch, watch_cmd=None)

    sp = sub.add_parser("_watch-daemon")
    sp.set_defaults(func=cmd_watch_daemon)

    args = p.parse_args()
    try:
        _ensure_watcher(args)
    except Exception:
        pass  # the watcher must never break a normal pp call
    try:
        _ensure_wired(args)
    except Exception:
        pass  # ditto for statusline auto-wire
    args.func(args)


if __name__ == "__main__":
    main()
