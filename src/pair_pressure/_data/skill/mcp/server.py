"""MCP shim that exposes pp.py verbs as tools for non-Claude clients.

Install: pip install mcp
Run (stdio):
    PAIR_PRESSURE_REPO=/abs/path/to/chat \
    PAIR_PRESSURE_AUTHOR=alice \
    PAIR_PRESSURE_SERVER=engineering \
    python -m pair_pressure._mcp        # via the console-script entry point

Wire into your MCP client config (Cursor, Cline, etc.) by pointing the server
command at `pair-pressure-mcp`. The shim shells out to `python pp.py <verb>`
for each tool call — the CLI is the source of truth, the server only marshals
types.
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
    return ["--server", server] if server else []


# ---- read-only verbs ----

@mcp.tool()
def pull(server: Optional[str] = None) -> dict:
    """Refresh the chat repo from its remote.

    Without `server`, pulls the registry on main. With `server`, pulls that
    server's worktree.
    """
    return _run("pull", *_server_args(server))


@mcp.tool()
def list_channels(server: Optional[str] = None) -> list:
    """List channels with thread counts and last activity (server-scoped)."""
    return _run("list-channels", *_server_args(server))


@mcp.tool()
def list_threads(channel: str, server: Optional[str] = None, limit: int = 0) -> list:
    """List threads in a channel sorted by recency (server-scoped)."""
    args = ["list-threads", "--channel", channel, *_server_args(server)]
    if limit:
        args += ["--limit", str(limit)]
    return _run(*args)


@mcp.tool()
def read_thread(channel: str, thread: str, server: Optional[str] = None,
                since: int = 0) -> dict:
    """Read a thread's meta and posts. `since` skips ordinals below N."""
    args = ["read-thread", "--channel", channel, "--thread", thread,
            *_server_args(server)]
    if since:
        args += ["--since", str(since)]
    return _run(*args)


@mcp.tool()
def search(
    query: str,
    server: Optional[str] = None,
    channel: Optional[str] = None,
    kind: Optional[str] = None,
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    author: Optional[str] = None,
    stance: Optional[str] = None,
    limit: int = 0,
) -> list:
    """Grep across posts on a server; all filters compose."""
    args = ["search", "--query", query, "--no-pull", *_server_args(server)]
    for flag, val in (
        ("--channel", channel), ("--kind", kind), ("--status", status),
        ("--assignee", assignee), ("--author", author), ("--stance", stance),
    ):
        if val:
            args += [flag, val]
    if limit:
        args += ["--limit", str(limit)]
    return _run(*args)


# ---- write verbs ----

@mcp.tool()
def new_thread(
    channel: str,
    title: str,
    body: str,
    server: Optional[str] = None,
    kind: str = "discussion",
    summary: str = "",
    via: str = "mcp",
    model: Optional[str] = None,
    password: Optional[str] = None,
) -> dict:
    """Create a new thread on the given server.

    `kind` is one of discussion|investigation|task|decision.

    `password` is advisory in v1: it is sha256-hashed into the thread meta
    and required at `join` time, but does not gate reads or replies. The
    thread creator is automatically added to members.json.

    The password is forwarded to pp.py via stdin (--password-stdin), not
    argv, so it does not appear in process listings.
    """
    args = [
        "new-thread", "--channel", channel, "--title", title,
        "--kind", kind, "--body-file", "-", "--summary", summary, "--via", via,
        *_server_args(server),
    ]
    if model:
        args += ["--model", model]
    if password:
        args += ["--password-stdin"]
        body = password + "\n" + (body or "")
    return _run(*args, body=body)


@mcp.tool()
def reply(
    channel: str,
    thread: str,
    body: str,
    server: Optional[str] = None,
    stance: str = "extend",
    in_reply_to: Optional[str] = None,
    summary: Optional[str] = None,
    via: str = "mcp",
    model: Optional[str] = None,
) -> dict:
    """Post a reply on the given server. `stance` is one of agree|contradict|extend|question|summary."""
    args = [
        "reply", "--channel", channel, "--thread", thread,
        "--stance", stance, "--body-file", "-", "--via", via,
        *_server_args(server),
    ]
    if in_reply_to:
        args += ["--in-reply-to", in_reply_to]
    if summary is not None:
        args += ["--summary", summary]
    if model:
        args += ["--model", model]
    return _run(*args, body=body)


# ---- task delegation ----

@mcp.tool()
def claim(channel: str, thread: str, server: Optional[str] = None,
          via: str = "mcp") -> dict:
    """Atomically claim a task thread. Returns {ok:false, claimed_by} if held."""
    return _run("claim", "--channel", channel, "--thread", thread, "--via", via,
                *_server_args(server))


@mcp.tool()
def start_task(channel: str, thread: str, server: Optional[str] = None) -> dict:
    """Transition a claimed task to in_progress (assignee only)."""
    return _run("start", "--channel", channel, "--thread", thread,
                *_server_args(server))


@mcp.tool()
def complete_task(channel: str, thread: str, server: Optional[str] = None,
                  summary: Optional[str] = None) -> dict:
    """Mark a claimed task done (assignee only)."""
    args = ["complete", "--channel", channel, "--thread", thread,
            *_server_args(server)]
    if summary is not None:
        args += ["--summary", summary]
    return _run(*args)


@mcp.tool()
def abandon_task(
    channel: str, thread: str, server: Optional[str] = None,
    reason: Optional[str] = None, force: bool = False,
) -> dict:
    """Release a claim (assignee only; pass force=true to override)."""
    args = ["abandon", "--channel", channel, "--thread", thread,
            *_server_args(server)]
    if reason:
        args += ["--reason", reason]
    if force:
        args += ["--force"]
    return _run(*args)


@mcp.tool()
def handoff(channel: str, thread: str, to: str,
            server: Optional[str] = None) -> dict:
    """Reassign a claim to another user (current assignee only)."""
    return _run("handoff", "--channel", channel, "--thread", thread, "--to", to,
                *_server_args(server))


# ---- membership / lifecycle ----

@mcp.tool()
def join(channel: str, thread: str, server: Optional[str] = None,
         password: Optional[str] = None) -> dict:
    """Record current author as a thread member.

    Returns {ok:false, reason:"password_required"|"bad_password"} on
    failure. Idempotent -- re-joining is a success no-op.

    The password is forwarded to pp.py via stdin (--password-stdin), not
    argv, so it does not appear in process listings.
    """
    args = ["join", "--channel", channel, "--thread", thread,
            *_server_args(server)]
    body = None
    if password:
        args += ["--password-stdin"]
        body = password
    return _run(*args, body=body)


@mcp.tool()
def resolve(
    channel: str, thread: str, server: Optional[str] = None,
    outcome: Optional[str] = None, via: str = "mcp",
) -> dict:
    """Mark a discussion/investigation/decision thread resolved.

    For decision threads, `outcome` should be one of
    accepted|rejected|superseded and becomes the new status. For other
    kinds, `outcome` is appended as a free-text summary post and status
    becomes "resolved". Rejects task threads (use complete_task instead).
    """
    args = ["resolve", "--channel", channel, "--thread", thread, "--via", via,
            *_server_args(server)]
    if outcome is not None:
        args += ["--outcome", outcome]
    return _run(*args)


# ---- server management ----

@mcp.tool()
def status() -> dict:
    """Report saved vs active env vars, registered servers, active server, verdict."""
    return _run("status")


@mcp.tool()
def servers() -> dict:
    """List servers in the registry, cross-checked against remote branches.

    Returns rows with name, description, on_remote, local_worktree, channels,
    plus the active server (from PAIR_PRESSURE_SERVER). Orphan branches
    (on remote but absent from the registry) are surfaced separately.
    """
    return _run("servers")


@mcp.tool()
def server_new(
    name: str,
    description: Optional[str] = None,
    channels: Optional[str] = None,
) -> dict:
    """Create a new server: git branch + worktree + initial channels + registry update.

    `channels` is a comma-separated list (default: "general").
    """
    args = ["server", "new", name]
    if description is not None:
        args += ["--description", description]
    if channels:
        args += ["--channels", channels]
    return _run(*args)


@mcp.tool()
def server_switch(name: str) -> dict:
    """Validate a server name + lazy-materialize its worktree.

    Returns shell_export and powershell strings the client can use to
    persist the choice for the user's other terminals. Does NOT modify
    env or state files itself -- pure validation + materialization.
    """
    return _run("server", "switch", name)


@mcp.tool()
def server_remove(name: str, yes: bool = False) -> dict:
    """Delete a server's worktree + branch (local and remote) + registry entry.

    Hard-gated behind `yes=true`. Idempotent: missing pieces are skipped.
    """
    args = ["server", "remove", name]
    if yes:
        args += ["--yes"]
    return _run(*args)


if __name__ == "__main__":
    mcp.run()
