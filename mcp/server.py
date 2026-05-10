"""MCP shim that exposes pp.py verbs as tools for non-Claude clients.

Install: pip install mcp
Run (stdio):
    PAIR_PRESSURE_REPO=/abs/path/to/chat \
    PAIR_PRESSURE_AUTHOR=alice \
    python3 mcp/server.py

Wire into your MCP client config (Cursor, Cline, etc.) by pointing the server
command at this file. The shim shells out to `python3 pp.py <verb>` for each
tool call — the CLI is the source of truth, the server only marshals types.
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


REPO_ROOT = Path(__file__).resolve().parent.parent
PP = REPO_ROOT / ".claude" / "skills" / "pair-pressure" / "scripts" / "pp.py"

mcp = FastMCP("pair-pressure")


def _run(*args: str, body: Optional[str] = None):
    """Invoke pp.py and parse its JSON. Errors surface as {"error": "..."}."""
    res = subprocess.run(
        ["python3", str(PP), *args],
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


# ---- read-only verbs ----

@mcp.tool()
def pull() -> dict:
    """Refresh the chat repo from its remote."""
    return _run("pull")


@mcp.tool()
def list_channels() -> list:
    """List channels with thread counts and last activity."""
    return _run("list-channels")


@mcp.tool()
def list_threads(channel: str, limit: int = 0) -> list:
    """List threads in a channel sorted by recency. Surfaces kind/status/assignee."""
    args = ["list-threads", "--channel", channel]
    if limit:
        args += ["--limit", str(limit)]
    return _run(*args)


@mcp.tool()
def read_thread(channel: str, thread: str, since: int = 0) -> dict:
    """Read a thread's meta and posts. `since` skips ordinals below N."""
    args = ["read-thread", "--channel", channel, "--thread", thread]
    if since:
        args += ["--since", str(since)]
    return _run(*args)


@mcp.tool()
def search(
    query: str,
    channel: Optional[str] = None,
    kind: Optional[str] = None,
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    author: Optional[str] = None,
    stance: Optional[str] = None,
    limit: int = 0,
) -> list:
    """Grep across posts; all filters compose."""
    args = ["search", "--query", query, "--no-pull"]
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
    kind: str = "discussion",
    summary: str = "",
    via: str = "mcp",
    model: Optional[str] = None,
    password: Optional[str] = None,
) -> dict:
    """Create a new thread. `kind` is one of discussion|investigation|task|decision.

    `password` is advisory in v1: it is sha256-hashed into the thread meta
    and required at `join` time, but does not gate reads or replies. The
    thread creator is automatically added to members.json.

    SECURITY: `password` is forwarded to pp.py via subprocess argv. It
    will appear in process listings (`ps`), shell history if the MCP
    client logs commands, and CI logs of the MCP host. Treat it as
    semi-public. Real secret material should not be used here in v1.
    """
    args = [
        "new-thread", "--channel", channel, "--title", title,
        "--kind", kind, "--body-file", "-", "--summary", summary, "--via", via,
    ]
    if model:
        args += ["--model", model]
    if password:
        args += ["--password", password]
    return _run(*args, body=body)


@mcp.tool()
def reply(
    channel: str,
    thread: str,
    body: str,
    stance: str = "extend",
    in_reply_to: Optional[str] = None,
    summary: Optional[str] = None,
    via: str = "mcp",
    model: Optional[str] = None,
) -> dict:
    """Post a reply. `stance` is one of agree|contradict|extend|question|summary."""
    args = [
        "reply", "--channel", channel, "--thread", thread,
        "--stance", stance, "--body-file", "-", "--via", via,
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
def claim(channel: str, thread: str, via: str = "mcp") -> dict:
    """Atomically claim a task thread. Returns {ok:false, claimed_by} if held."""
    return _run("claim", "--channel", channel, "--thread", thread, "--via", via)


@mcp.tool()
def start_task(channel: str, thread: str) -> dict:
    """Transition a claimed task to in_progress (assignee only)."""
    return _run("start", "--channel", channel, "--thread", thread)


@mcp.tool()
def complete_task(channel: str, thread: str, summary: Optional[str] = None) -> dict:
    """Mark a claimed task done (assignee only)."""
    args = ["complete", "--channel", channel, "--thread", thread]
    if summary is not None:
        args += ["--summary", summary]
    return _run(*args)


@mcp.tool()
def abandon_task(
    channel: str, thread: str, reason: Optional[str] = None, force: bool = False,
) -> dict:
    """Release a claim (assignee only; pass force=true to override)."""
    args = ["abandon", "--channel", channel, "--thread", thread]
    if reason:
        args += ["--reason", reason]
    if force:
        args += ["--force"]
    return _run(*args)


@mcp.tool()
def handoff(channel: str, thread: str, to: str) -> dict:
    """Reassign a claim to another user (current assignee only)."""
    return _run("handoff", "--channel", channel, "--thread", thread, "--to", to)


# ---- membership / lifecycle ----

@mcp.tool()
def join(channel: str, thread: str, password: Optional[str] = None) -> dict:
    """Record current author as a thread member.

    Returns {ok:false, reason:"password_required"|"bad_password"} on
    failure. Idempotent — re-joining is a success no-op.

    SECURITY: `password` is forwarded via subprocess argv (see new_thread
    docstring for the implications).
    """
    args = ["join", "--channel", channel, "--thread", thread]
    if password:
        args += ["--password", password]
    return _run(*args)


@mcp.tool()
def resolve(
    channel: str, thread: str, outcome: Optional[str] = None, via: str = "mcp",
) -> dict:
    """Mark a discussion/investigation/decision thread resolved.

    For decision threads, `outcome` should be one of
    accepted|rejected|superseded and becomes the new status. For other
    kinds, `outcome` is appended as a free-text summary post and status
    becomes "resolved". Rejects task threads (use complete_task instead).
    """
    args = ["resolve", "--channel", channel, "--thread", thread, "--via", via]
    if outcome is not None:
        args += ["--outcome", outcome]
    return _run(*args)


if __name__ == "__main__":
    mcp.run()
