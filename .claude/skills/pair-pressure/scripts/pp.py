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
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

__version__ = "0.2.0"


def die(msg, code=2):
    print(json.dumps({"error": msg}), file=sys.stderr)
    sys.exit(code)


def env(name):
    val = os.environ.get(name)
    if not val:
        die(f"{name} is not set. Add it to ~/.claude/settings.local.json under env.")
    return val


def repo_path():
    p = Path(env("PAIR_PRESSURE_REPO")).expanduser()
    if not (p / ".git").exists():
        die(f"PAIR_PRESSURE_REPO={p} is not a git repository.")
    return p


def author():
    return env("PAIR_PRESSURE_AUTHOR")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


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


def out(obj):
    print(json.dumps(obj, indent=2, sort_keys=True))


def read_json(path, default=None):
    if not path.exists():
        return default if default is not None else {}
    return json.loads(path.read_text())


def write_json(path, obj):
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")


# ---- verbs ----

def cmd_pull(args):
    if not has_remote():
        out({"updated": False, "head": git("rev-parse", "HEAD", check=False).stdout.strip(), "note": "no remote configured"})
        return
    head_before = git("rev-parse", "HEAD", check=False).stdout.strip()
    res = git("pull", "--rebase", "--autostash", check=False)
    if res.returncode != 0:
        die(f"git pull failed: {res.stderr.strip() or res.stdout.strip()}")
    head_after = git("rev-parse", "HEAD").stdout.strip()
    out({"updated": head_before != head_after, "head": head_after})


def cmd_push(args):
    if not has_remote():
        out({"pushed": False, "note": "no remote configured"})
        return
    res = git("push", check=False)
    if res.returncode != 0:
        die(f"git push failed: {res.stderr.strip()}")
    out({"pushed": True})


def maybe_pull():
    if has_remote():
        git("pull", "--rebase", "--autostash", check=False)


def cmd_list_channels(args):
    if not args.no_pull:
        maybe_pull()
    root = repo_path() / "channels"
    channels = []
    if root.exists():
        for ch in sorted(p for p in root.iterdir() if p.is_dir()):
            meta = read_json(ch / "channel.json", {"name": ch.name, "description": ""})
            threads = [t for t in ch.iterdir() if t.is_dir()]
            last_ts = max((t.stat().st_mtime for t in threads), default=ch.stat().st_mtime)
            channels.append({
                "name": meta.get("name", ch.name),
                "description": meta.get("description", ""),
                "thread_count": len(threads),
                "last_activity": datetime.fromtimestamp(last_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
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


def _ord(path):
    return int(path.name[:3])


def _post_files(tdir):
    return sorted(tdir.glob("[0-9][0-9][0-9]-*.md"), key=_ord)


def cmd_list_threads(args):
    if not args.no_pull:
        maybe_pull()
    ch = channel_dir(args.channel)
    threads = []
    for t in sorted(p for p in ch.iterdir() if p.is_dir()):
        meta = read_json(t / "meta.json", {})
        posts = _post_files(t)
        last_author = ""
        if posts:
            fm, _ = parse_fm(posts[-1].read_text())
            last_author = fm.get("author", "") or ""
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
    if not args.no_pull:
        maybe_pull()
    t = thread_dir(args.channel, args.thread)
    meta = read_json(t / "meta.json", {})
    posts = []
    for p in _post_files(t):
        ord_ = _ord(p)
        if args.since and ord_ < args.since:
            continue
        fm, body = parse_fm(p.read_text())
        posts.append({
            "id": fm.get("id", f"{ord_:03d}"),
            "ordinal": ord_,
            "filename": p.name,
            "in_reply_to": fm.get("in_reply_to"),
            "author": fm.get("author"),
            "via": fm.get("via"),
            "model": fm.get("model"),
            "stance": fm.get("stance"),
            "timestamp": fm.get("timestamp"),
            "body": body.strip(),
        })
    out({"meta": meta, "posts": posts})


def read_body(args):
    if args.body_file == "-":
        return sys.stdin.read()
    return Path(args.body_file).read_text()


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


def push_with_retry(write_payload, build_message):
    """Write → commit → push, with one rebase-retry on reject.

    `write_payload()` writes files into the working tree and returns a dict.
    `build_message(info)` returns the commit message.

    On push reject: abort any in-progress rebase, hard-reset to the remote
    tip, re-invoke `write_payload()` (which recomputes ordinals/dir-names
    from the fresh tree), re-commit, push again. One retry only.
    """
    info = write_payload()
    _commit_all(build_message(info))
    if not has_remote():
        return info
    res = git("push", check=False)
    if res.returncode == 0:
        return info
    # Push rejected. Recover and retry.
    git("rebase", "--abort", check=False)
    branch = _current_branch()
    git("fetch", "origin", branch, check=False)
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
    maybe_pull()
    ch = channel_dir(args.channel)
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
        fm = {
            "id": "000",
            "in_reply_to": None,
            "author": author(),
            "via": args.via,
            "model": args.model,
            "stance": "summary",
            "timestamp": now_iso(),
        }
        (tdir / "000-seed.md").write_text(dump_fm(fm, body))
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
        return f"{args.channel}/{info['thread_id']}: new-thread by {author()} [via {args.via}]"

    out(push_with_retry(write_payload, msg))


def cmd_reply(args):
    maybe_pull()
    t = thread_dir(args.channel, args.thread)
    body = read_body(args)

    def write_payload():
        # Recompute ordinal each attempt — a rebase-retry might land us at a
        # different next ordinal than the first try.
        posts = _post_files(t)
        next_ord = (_ord(posts[-1]) + 1) if posts else 0
        fname = f"{next_ord:03d}-reply.md"
        fm = {
            "id": f"{next_ord:03d}",
            "in_reply_to": args.in_reply_to,
            "author": author(),
            "via": args.via,
            "model": args.model,
            "stance": args.stance,
            "timestamp": now_iso(),
        }
        (t / fname).write_text(dump_fm(fm, body))
        if args.summary is not None:
            meta_p = t / "meta.json"
            meta = read_json(meta_p, {})
            meta["summary"] = args.summary
            write_json(meta_p, meta)
        return {"reply_id": f"{next_ord:03d}", "filename": fname}

    def msg(info):
        return f"{args.channel}/{args.thread}: reply {info['reply_id']} by {author()} [via {args.via}]"

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
    git("rebase", "--abort", check=False)
    branch = _current_branch()
    git("fetch", "origin", branch, check=False)
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
    maybe_pull()
    t = thread_dir(args.channel, args.thread)
    me = author()
    success = {"ok": True, "assignee": me, "state": "claimed"}

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
    maybe_pull()
    t = thread_dir(args.channel, args.thread)
    me = author()

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
    maybe_pull()
    t = thread_dir(args.channel, args.thread)
    me = author()
    meta = read_json(t / "meta.json", {})
    ph = meta.get("password_hash")
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
            posts = _post_files(t)
            next_ord = (_ord(posts[-1]) + 1) if posts else 0
            fname = f"{next_ord:03d}-reply.md"
            fm = {
                "id": f"{next_ord:03d}",
                "in_reply_to": None,
                "author": me,
                "via": args.via,
                "model": None,
                "stance": "summary",
                "timestamp": now_iso(),
            }
            (t / fname).write_text(dump_fm(fm, outcome_body))
        return {"ok": True, "status": new_status, "thread": args.thread}

    def msg(info):
        return f"{args.channel}/{args.thread}: resolve by {me} -> {new_status}"

    out(push_with_retry(write_payload, msg))


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
        for p in (repo / "channels").rglob("[0-9][0-9][0-9]-*.md"):
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
        fm, _ = parse_fm(text)
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
            "post_id": fm.get("id", p.name[:3]),
            "filename": p.name,
            "author": fm.get("author"),
            "stance": fm.get("stance"),
            "timestamp": fm.get("timestamp"),
            "match": match,
            "snippet": snippet,
        })

    results.sort(key=lambda r: r.get("timestamp") or "", reverse=True)
    if args.limit:
        results = results[: args.limit]
    out(results)


def main():
    p = argparse.ArgumentParser(prog="pp", description="pair-pressure CLI")
    p.add_argument("--version", action="version", version=f"pair-pressure {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("pull", help="git pull --rebase --autostash")
    sp.set_defaults(func=cmd_pull)

    sp = sub.add_parser("push", help="git push if ahead")
    sp.set_defaults(func=cmd_push)

    sp = sub.add_parser("list-channels")
    sp.add_argument("--no-pull", action="store_true")
    sp.set_defaults(func=cmd_list_channels)

    sp = sub.add_parser("list-threads")
    sp.add_argument("--channel", required=True)
    sp.add_argument("--limit", type=int, default=0)
    sp.add_argument("--no-pull", action="store_true")
    sp.set_defaults(func=cmd_list_threads)

    sp = sub.add_parser("read-thread")
    sp.add_argument("--channel", required=True)
    sp.add_argument("--thread", required=True)
    sp.add_argument("--since", type=int, default=0)
    sp.add_argument("--no-pull", action="store_true")
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
    sp.add_argument("--password", default=None,
                    help="advisory access marker; sha256-hashed into meta.json")
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
    sp.set_defaults(func=cmd_reply)

    sp = sub.add_parser("claim", help="atomically claim a task thread")
    sp.add_argument("--channel", required=True)
    sp.add_argument("--thread", required=True)
    sp.add_argument("--via", default="claude-code")
    sp.set_defaults(func=cmd_claim)

    sp = sub.add_parser("start", help="mark a claimed task as in_progress (assignee only)")
    sp.add_argument("--channel", required=True)
    sp.add_argument("--thread", required=True)
    sp.set_defaults(func=cmd_start)

    sp = sub.add_parser("complete", help="mark a task done (assignee only)")
    sp.add_argument("--channel", required=True)
    sp.add_argument("--thread", required=True)
    sp.add_argument("--summary", default=None)
    sp.set_defaults(func=cmd_complete)

    sp = sub.add_parser("abandon", help="release a claim (assignee only by default)")
    sp.add_argument("--channel", required=True)
    sp.add_argument("--thread", required=True)
    sp.add_argument("--reason", default=None)
    sp.add_argument("--force", action="store_true",
                    help="abandon even if you are not the assignee")
    sp.set_defaults(func=cmd_abandon)

    sp = sub.add_parser("handoff", help="reassign a claim (current assignee only)")
    sp.add_argument("--channel", required=True)
    sp.add_argument("--thread", required=True)
    sp.add_argument("--to", required=True)
    sp.set_defaults(func=cmd_handoff)

    sp = sub.add_parser("join", help="record current author as a thread member")
    sp.add_argument("--channel", required=True)
    sp.add_argument("--thread", required=True)
    sp.add_argument("--password", default=None,
                    help="required if the thread was created with --password")
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
    sp.set_defaults(func=cmd_resolve)

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
    sp.set_defaults(func=cmd_search)

    args = p.parse_args()
    try:
        args.func(args)
    except subprocess.CalledProcessError as e:
        die(f"git error: {e.stderr.strip() or e.stdout.strip()}")


if __name__ == "__main__":
    main()
