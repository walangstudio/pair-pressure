"""MCP shim that exposes pp.py verbs as tools for any MCP client.

This is the first-class integration for non-Claude CLIs (Codex, opencode,
Cursor, Cline, Kilo, ...). Day-to-day chat parity with the slash commands:
everything an agent does in a conversation (send/read/search/channels/dm/
tasks/unread/use/where/status) is here. One-time setup actions — registering
or removing a server — stay on the `pp` CLI / `pp-setup` wizard by design;
`server_list` surfaces what's registered.

Install: pip install mcp
Run (stdio):
    PAIR_PRESSURE_AUTHOR=alice \
    python -m pair_pressure._mcp        # via the console-script entry point

Wire into your MCP client config by pointing the server command at
`pair-pressure-mcp`. The shim shells out to `python pp.py <verb>` for each
tool call — the CLI is the source of truth, the server only marshals types.

Server/channel stickiness across calls uses ~/.pair-pressure state files.
For per-conversation state (restored on resume), export a stable
PAIR_PRESSURE_SESSION_ID in the MCP server's env; otherwise `use` updates
the machine-global default.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as e:
    sys.stderr.write(
        "mcp SDK not installed. Run: pip install mcp\n"
        f"({e})\n"
    )
    sys.exit(1)


# mcp/server.py lives at <_data>/skill/mcp/server.py
# parents[1] = <_data>/skill -- pp.py is at scripts/pp.py inside that
SKILL_ROOT = Path(__file__).resolve().parent.parent
PP = SKILL_ROOT / "scripts" / "pp.py"

mcp = FastMCP("pair-pressure")


def _run(*args: str, body: Optional[str] = None):
    """Invoke pp.py and parse its JSON. Errors surface as {"error": "..."}."""
    res = subprocess.run(
        [sys.executable, str(PP), *args],
        input=body or "",
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    if res.returncode != 0:
        # pp.py emits {"error": "..."} on stderr for handled failures.
        try:
            return json.loads(res.stderr)
        except json.JSONDecodeError:
            return {"error": (res.stderr or res.stdout).strip()}
    return json.loads(res.stdout)


def _server_args(server: Optional[str]) -> list:
    """--server scopes one call to a registered server without switching."""
    return ["--server", server] if server else []


# ---- read verbs ----

@mcp.tool()
def pull(server: Optional[str] = None) -> dict:
    """Refresh the active (or named) server's clone from its remote."""
    return _run("pull", *_server_args(server))


@mcp.tool()
def read(channel: Optional[str] = None, message: Optional[str] = None,
         limit: int = 30, since: Optional[str] = None,
         server: Optional[str] = None) -> dict:
    """Read chat. No args = chronological cross-channel feed; `channel`
    = that channel's recent posts; `message` (post id or unique trailing
    substring) = one full post body. `since` is an ISO timestamp lower
    bound. Private channels you are not a member of never appear."""
    args = ["read"]
    if channel:
        args += [channel]
    if message:
        args += ["--message", message]
    args += ["--limit", str(limit)]
    if since:
        args += ["--since", since]
    return _run(*args, *_server_args(server))


@mcp.tool()
def search(query: str, channel: Optional[str] = None,
           author: Optional[str] = None, limit: int = 20,
           server: Optional[str] = None) -> dict:
    """Grep across posts on a server; filter by channel and/or author."""
    args = ["search", "--query", query, "--no-pull", "--limit", str(limit)]
    if channel:
        args += ["--channel", channel]
    if author:
        args += ["--author", author]
    return _run(*args, *_server_args(server))


@mcp.tool()
def list_channels(include_archived: bool = False,
                  server: Optional[str] = None) -> dict:
    """List channels with last activity. The active channel is marked.
    Private channels show only for members; archived ones only with
    include_archived=true."""
    args = ["channels"]
    if include_archived:
        args += ["--all"]
    return _run(*args, *_server_args(server))


@mcp.tool()
def unread(all_servers: bool = False, since: Optional[str] = None,
           ack: bool = False, server: Optional[str] = None) -> dict:
    """New posts not authored by you. Without `since`, uses the watcher's
    baseline (non-destructive — does not clear the badge); with `since`
    (ISO), counts posts at/after that timestamp. `all_servers` spans every
    registered server; `ack` clears this session's unread badge."""
    args = ["unread"]
    if all_servers:
        args += ["--all"]
    if since:
        args += ["--since", since]
    if ack:
        args += ["--ack"]
    return _run(*args, *_server_args(server))


# ---- write verbs ----

@mcp.tool()
def send(body: str, channel: Optional[str] = None,
         reply_to: Optional[str] = None, alias: Optional[str] = None,
         model: Optional[str] = None, attach: Optional[list] = None,
         server: Optional[str] = None) -> dict:
    """Post a message to the active (or named) channel. `reply_to` is a
    post id or unique substring. `attach` is a list of local file paths to
    copy in as attachments. The result's `where` field confirms exactly
    where the post landed."""
    args = ["send", "--body-file", "-", "--via", "mcp"]
    if channel:
        args += ["--channel", channel]
    if reply_to:
        args += ["--reply-to", reply_to]
    if alias:
        args += ["--alias", alias]
    if model:
        args += ["--model", model]
    for path in attach or []:
        args += ["--attach", str(path)]
    return _run(*args, *_server_args(server), body=body)


@mcp.tool()
def channel_new(name: str, description: str = "",
                server: Optional[str] = None) -> dict:
    """Create a channel. Admin-only (advisory — admins live in
    server.json; the server creator is the first admin)."""
    args = ["channel", "new", name]
    if description:
        args += ["--description", description]
    return _run(*args, *_server_args(server))


@mcp.tool()
def dm_new(users: list, name: Optional[str] = None,
           server: Optional[str] = None) -> dict:
    """Create (or reopen) a private group chat with the given users.
    NOT ENCRYPTED: content is plain text in the git repo; anyone with repo
    access can read the raw files. Visibility is tooling-enforced only."""
    args = ["dm", *[str(u) for u in users]]
    if name:
        args += ["--name", name]
    return _run(*args, *_server_args(server))


# ---- tasks (per-channel checklist) ----

@mcp.tool()
def task_new(title: str, channel: Optional[str] = None,
             server: Optional[str] = None) -> dict:
    """Add a task to the channel's checklist."""
    args = ["task", "new", title]
    if channel:
        args += ["--channel", channel]
    return _run(*args, *_server_args(server))


@mcp.tool()
def task_list(channel: Optional[str] = None, include_done: bool = False,
              server: Optional[str] = None) -> dict:
    """List the channel's open tasks (include_done=true adds finished ones)."""
    args = ["task", "list"]
    if channel:
        args += ["--channel", channel]
    if include_done:
        args += ["--all"]
    return _run(*args, *_server_args(server))


@mcp.tool()
def task_done(ref: str, channel: Optional[str] = None,
              server: Optional[str] = None) -> dict:
    """Mark a task done. `ref` is '#<id>', '<id>', or a title substring."""
    args = ["task", "done", ref]
    if channel:
        args += ["--channel", channel]
    return _run(*args, *_server_args(server))


@mcp.tool()
def task_claim(ref: str, channel: Optional[str] = None,
               server: Optional[str] = None) -> dict:
    """Claim a task (assign it to yourself). `ref` is '#<id>', '<id>', or a
    title substring. Fails if someone else holds it (they release, or assign)."""
    args = ["task", "claim", ref]
    if channel:
        args += ["--channel", channel]
    return _run(*args, *_server_args(server))


@mcp.tool()
def task_assign(ref: str, user: str, channel: Optional[str] = None,
                server: Optional[str] = None) -> dict:
    """Assign a task to `user` (hand it off). `ref` is '#<id>', '<id>', or a
    title substring."""
    args = ["task", "assign", ref, user]
    if channel:
        args += ["--channel", channel]
    return _run(*args, *_server_args(server))


@mcp.tool()
def task_release(ref: str, channel: Optional[str] = None,
                 server: Optional[str] = None) -> dict:
    """Release a task back to open (clear its assignee). `ref` is '#<id>',
    '<id>', or a title substring."""
    args = ["task", "release", ref]
    if channel:
        args += ["--channel", channel]
    return _run(*args, *_server_args(server))


# ---- location + identity ----

@mcp.tool()
def use(target: str) -> dict:
    """Switch where you are: '<server>', '#<channel>', or
    '<server> #<channel>'. Persists to session state (with
    PAIR_PRESSURE_SESSION_ID) and the machine-global default, so the
    location survives a conversation resume."""
    toks = [t for t in str(target).split() if t]
    if not toks:
        return {"error": "use: empty target"}
    return _run("use", *toks)


@mcp.tool()
def where() -> dict:
    """One line: active server, channel, and alias, with the source of
    each (flag/session/global/env/default)."""
    return _run("where")


@mcp.tool()
def status() -> dict:
    """Identity + location: author/alias env, active server and channel,
    registered servers, and a verdict (ready / needs_author / ...)."""
    return _run("status")


@mcp.tool()
def server_list() -> dict:
    """Registered servers (name, path, url); the active one is marked."""
    return _run("server", "list")


if __name__ == "__main__":
    mcp.run()
