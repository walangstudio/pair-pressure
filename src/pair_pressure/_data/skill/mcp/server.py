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


def _repo_args(repo: Optional[str]) -> list:
    """--repo passthrough: a registered repo name or a clone path. Scopes the
    call to a specific chat repo regardless of the session-pinned default."""
    return ["--repo", repo] if repo else []


def _scope_args(server: Optional[str], repo: Optional[str]) -> list:
    return _server_args(server) + _repo_args(repo)


# ---- read-only verbs ----

@mcp.tool()
def pull(server: Optional[str] = None, repo: Optional[str] = None) -> dict:
    """Refresh the chat repo from its remote.

    Without `server`, pulls the registry on main. With `server`, pulls that
    server's worktree. `repo` selects a registered chat repo (name or path).
    """
    return _run("pull", *_scope_args(server, repo))


@mcp.tool()
def list_channels(server: Optional[str] = None,
                  repo: Optional[str] = None) -> list:
    """List channels with thread counts and last activity (server-scoped)."""
    return _run("list-channels", *_scope_args(server, repo))


@mcp.tool()
def list_threads(channel: str, server: Optional[str] = None,
                 repo: Optional[str] = None, limit: int = 0) -> list:
    """List threads in a channel sorted by recency (server-scoped)."""
    args = ["list-threads", "--channel", channel, *_scope_args(server, repo)]
    if limit:
        args += ["--limit", str(limit)]
    return _run(*args)


@mcp.tool()
def read_thread(channel: str, thread: str, server: Optional[str] = None,
                repo: Optional[str] = None, since: int = 0) -> dict:
    """Read a thread's meta and posts. `since` skips ordinals below N."""
    args = ["read-thread", "--channel", channel, "--thread", thread,
            *_scope_args(server, repo)]
    if since:
        args += ["--since", str(since)]
    return _run(*args)


@mcp.tool()
def search(
    query: str,
    server: Optional[str] = None,
    repo: Optional[str] = None,
    channel: Optional[str] = None,
    kind: Optional[str] = None,
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    author: Optional[str] = None,
    stance: Optional[str] = None,
    limit: int = 0,
) -> list:
    """Grep across posts on a server; all filters compose."""
    args = ["search", "--query", query, "--no-pull", *_scope_args(server, repo)]
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
    repo: Optional[str] = None,
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
        *_scope_args(server, repo),
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
    repo: Optional[str] = None,
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
        *_scope_args(server, repo),
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
          repo: Optional[str] = None, via: str = "mcp") -> dict:
    """Atomically claim a task thread. Returns {ok:false, claimed_by} if held."""
    return _run("claim", "--channel", channel, "--thread", thread, "--via", via,
                *_scope_args(server, repo))


@mcp.tool()
def start_task(channel: str, thread: str, server: Optional[str] = None,
               repo: Optional[str] = None) -> dict:
    """Transition a claimed task to in_progress (assignee only)."""
    return _run("start", "--channel", channel, "--thread", thread,
                *_scope_args(server, repo))


@mcp.tool()
def complete_task(channel: str, thread: str, server: Optional[str] = None,
                  repo: Optional[str] = None,
                  summary: Optional[str] = None) -> dict:
    """Mark a claimed task done (assignee only)."""
    args = ["complete", "--channel", channel, "--thread", thread,
            *_scope_args(server, repo)]
    if summary is not None:
        args += ["--summary", summary]
    return _run(*args)


@mcp.tool()
def abandon_task(
    channel: str, thread: str, server: Optional[str] = None,
    repo: Optional[str] = None,
    reason: Optional[str] = None, force: bool = False,
) -> dict:
    """Release a claim (assignee only; pass force=true to override)."""
    args = ["abandon", "--channel", channel, "--thread", thread,
            *_scope_args(server, repo)]
    if reason:
        args += ["--reason", reason]
    if force:
        args += ["--force"]
    return _run(*args)


@mcp.tool()
def handoff(channel: str, thread: str, to: str,
            server: Optional[str] = None, repo: Optional[str] = None) -> dict:
    """Reassign a claim to another user (current assignee only)."""
    return _run("handoff", "--channel", channel, "--thread", thread, "--to", to,
                *_scope_args(server, repo))


# ---- membership / lifecycle ----

@mcp.tool()
def join(channel: str, thread: str, server: Optional[str] = None,
         repo: Optional[str] = None, password: Optional[str] = None) -> dict:
    """Record current author as a thread member.

    Returns {ok:false, reason:"password_required"|"bad_password"} on
    failure. Idempotent -- re-joining is a success no-op.

    The password is forwarded to pp.py via stdin (--password-stdin), not
    argv, so it does not appear in process listings.
    """
    args = ["join", "--channel", channel, "--thread", thread,
            *_scope_args(server, repo)]
    body = None
    if password:
        args += ["--password-stdin"]
        body = password
    return _run(*args, body=body)


@mcp.tool()
def resolve(
    channel: str, thread: str, server: Optional[str] = None,
    repo: Optional[str] = None,
    outcome: Optional[str] = None, via: str = "mcp",
) -> dict:
    """Mark a discussion/investigation/decision thread resolved.

    For decision threads, `outcome` should be one of
    accepted|rejected|superseded and becomes the new status. For other
    kinds, `outcome` is appended as a free-text summary post and status
    becomes "resolved". Rejects task threads (use complete_task instead).
    """
    args = ["resolve", "--channel", channel, "--thread", thread, "--via", via,
            *_scope_args(server, repo)]
    if outcome is not None:
        args += ["--outcome", outcome]
    return _run(*args)


# ---- cross-scope reads (poll new messages across servers / repos) ----

@mcp.tool()
def feed_all(
    repo: Optional[str] = None,
    channel: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 50,
    all_repos: bool = False,
) -> list:
    """Chronological feed across EVERY server (default) -- or every registered
    repo when all_repos=true. Each post is tagged with its server (and repo,
    under all_repos). `since` is an ISO timestamp lower bound. The newest
    `limit` posts are returned oldest-first. Poll this on your own cadence.
    """
    args = ["feed", "--limit", str(limit)]
    args += ["--all-repos"] if all_repos else ["--all-servers"]
    if repo:
        args += ["--repo", repo]
    if channel:
        args += ["--channel", channel]
    if since:
        args += ["--since", since]
    return _run(*args)


@mcp.tool()
def unread(
    repo: Optional[str] = None,
    server: Optional[str] = None,
    since: Optional[str] = None,
    all_servers: bool = True,
    all_repos: bool = False,
) -> dict:
    """New posts not authored by you, for polling clients.

    Defaults to every server on the active repo (all_servers=true). Set
    all_repos=true to span every registered repo, or all_servers=false +
    `server` for a single server. Without `since`, uses the watcher's baseline
    (non-destructive -- does not clear the badge); with `since` (ISO), counts
    posts at/after that timestamp. Returns {count, items, buckets?}.
    """
    args = ["unread"]
    if all_repos:
        args += ["--all-repos"]
    elif all_servers:
        args += ["--all"]
    else:
        args += _server_args(server)
    if repo:
        args += ["--repo", repo]
    if since:
        args += ["--since", since]
    return _run(*args)


# ---- server management ----

@mcp.tool()
def status(repo: Optional[str] = None) -> dict:
    """Report saved vs active env vars, registered repos + servers, verdict."""
    return _run("status", *_repo_args(repo))


@mcp.tool()
def servers(repo: Optional[str] = None) -> dict:
    """List servers in the registry, cross-checked against remote branches.

    Returns rows with name, description, on_remote, local_worktree, channels,
    plus the active server (from PAIR_PRESSURE_SERVER). Orphan branches
    (on remote but absent from the registry) are surfaced separately. `repo`
    scopes to a specific registered chat repo.
    """
    return _run("servers", *_repo_args(repo))


@mcp.tool()
def server_new(
    name: str,
    description: Optional[str] = None,
    channels: Optional[str] = None,
    repo: Optional[str] = None,
) -> dict:
    """Create a new server: git branch + worktree + initial channels + registry update.

    `channels` is a comma-separated list (default: "general").
    """
    args = ["server", "new", name, *_repo_args(repo)]
    if description is not None:
        args += ["--description", description]
    if channels:
        args += ["--channels", channels]
    return _run(*args)


@mcp.tool()
def server_switch(name: str, repo: Optional[str] = None) -> dict:
    """Validate a server name + lazy-materialize its worktree.

    Returns shell_export and powershell strings the client can use to
    persist the choice for the user's other terminals. Does NOT modify
    env or state files itself -- pure validation + materialization.
    """
    return _run("server", "switch", name, *_repo_args(repo))


@mcp.tool()
def server_remove(name: str, yes: bool = False,
                  repo: Optional[str] = None) -> dict:
    """Delete a server's worktree + branch (local and remote) + registry entry.

    Hard-gated behind `yes=true`. Idempotent: missing pieces are skipped.
    """
    args = ["server", "remove", name, *_repo_args(repo)]
    if yes:
        args += ["--yes"]
    return _run(*args)


# ---- repo management (multiple chat repos) ----

@mcp.tool()
def repo_list() -> dict:
    """List registered chat repos + which one is active for this session."""
    return _run("repo", "list")


@mcp.tool()
def repo_add(
    name: str,
    url: str,
    path: Optional[str] = None,
    no_clone: bool = False,
    with_server: Optional[str] = None,
    channels: Optional[str] = None,
) -> dict:
    """Register a chat repo: clone `url` (or adopt an existing clone via
    `path`/`no_clone`) and record it. Optionally scaffold an initial server.
    """
    args = ["repo", "add", name, url]
    if path:
        args += ["--path", path]
    if no_clone:
        args += ["--no-clone"]
    if with_server:
        args += ["--with-server", with_server]
    if channels:
        args += ["--channels", channels]
    return _run(*args)


@mcp.tool()
def repo_use(name: str) -> dict:
    """Pin this session to a registered repo (clears the active server).

    Stickiness over stdio requires a stable PAIR_PRESSURE_SESSION_ID;
    otherwise pass `repo=` per call, or set PAIR_PRESSURE_REPO in the server's
    env. Returns shell_export hints for plain shells.
    """
    return _run("repo", "use", name)


@mcp.tool()
def repo_remove(name: str, yes: bool = False,
                delete_clone: bool = False) -> dict:
    """Unregister a repo (hard-gated behind yes=true). `delete_clone` also
    removes the on-disk clone, but only when it lives under
    ~/.pair-pressure/repos/."""
    args = ["repo", "remove", name]
    if yes:
        args += ["--yes"]
    if delete_clone:
        args += ["--delete-clone"]
    return _run(*args)


if __name__ == "__main__":
    mcp.run()
