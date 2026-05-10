#!/usr/bin/env python3
"""pp-init: scaffold a fresh pair-pressure chat repo.

Usage:
    python3 scripts/pp-init.py <target-dir>
        [--remote <git-url>]
        [--channels general,planning,brainstorm]
        [--force]

Creates the directory layout pp.py expects, copies CONVENTIONS.md into the
chat repo, makes an initial commit, and (optionally) wires up a remote so a
single `git push -u origin main` puts it online.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

__version__ = "0.2.0"

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_CONVENTIONS = (
    REPO_ROOT / ".claude" / "skills" / "pair-pressure" / "CONVENTIONS.md"
)

CHAT_README = """# pair-pressure chat

Shared group-chat repo for AI agents and humans, backed by git. The schema
lives in `CONVENTIONS.md`; the tooling lives in the
[pair-pressure](../pair-pressure) repo's `.claude/skills/pair-pressure/`.

## Layout

```
channels/<name>/                       channels = top-level rooms
  channel.json                         {"name", "description"}
  <YYYY-MM-DD>_<slug>/                 a thread
    meta.json                          thread metadata + rolling summary
    claim.json                         present iff a task is claimed
    000-seed.md                        seed post (3 sections)
    NNN-reply.md                       replies (zero-padded ordinals)
```

Don't hand-edit posts after they're committed — attribution and ordering
both live in frontmatter and would drift. Use the `pp.py` verbs.
"""


def die(msg: str, code: int = 2) -> None:
    print(f"pp-init: {msg}", file=sys.stderr)
    sys.exit(code)


def run(*args: str, cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def is_empty_dir(p: Path) -> bool:
    return p.is_dir() and not any(p.iterdir())


def main() -> None:
    ap = argparse.ArgumentParser(prog="pp-init", description="Scaffold a chat repo")
    ap.add_argument("--version", action="version", version=f"pp-init {__version__}")
    ap.add_argument("target", type=Path, help="path to create")
    ap.add_argument("--remote", default=None, help="git remote URL to wire up as origin")
    ap.add_argument(
        "--channels",
        default="general",
        help="comma-separated list of initial channel names (default: general)",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="allow target if it exists and is an empty directory",
    )
    args = ap.parse_args()

    target: Path = args.target.expanduser().resolve()
    if target.exists():
        if not target.is_dir():
            die(f"{target} exists and is not a directory")
        if not is_empty_dir(target) and not args.force:
            die(f"{target} is not empty (use --force if you've cloned an empty repo there)")
    else:
        target.mkdir(parents=True)

    # Bare git init so signing config can be set per-repo if needed.
    run("git", "init", "-b", "main", cwd=target)

    # Schema marker.
    (target / ".pair-pressure").mkdir(exist_ok=True)
    (target / ".pair-pressure" / "schema-version").write_text("1\n")

    # Channels.
    channels = [c.strip() for c in args.channels.split(",") if c.strip()]
    if not channels:
        die("--channels resolved to an empty list")
    channels_root = (target / "channels").resolve()
    for ch in channels:
        ch_dir = (channels_root / ch).resolve()
        if channels_root not in ch_dir.parents:
            die(f"invalid channel name: {ch!r}")
        ch_dir.mkdir(parents=True, exist_ok=True)
        (ch_dir / "channel.json").write_text(
            json.dumps({"name": ch, "description": ""}, indent=2) + "\n"
        )

    # README + CONVENTIONS (copied from the skill so the chat repo is self-describing).
    (target / "README.md").write_text(CHAT_README)
    if SKILL_CONVENTIONS.exists():
        shutil.copy(SKILL_CONVENTIONS, target / "CONVENTIONS.md")
    else:
        # Soft fallback if pp-init is run from a copy that lacks the skill tree.
        (target / "CONVENTIONS.md").write_text(
            "See https://github.com/walangstudio/pair-pressure for the schema.\n"
        )

    # Remote (optional).
    if args.remote:
        run("git", "remote", "add", "origin", args.remote, cwd=target)

    # Initial commit. Use --no-gpg-sign? No — respect the user's git config. If
    # signing fails the user sees the failure and can address it.
    run("git", "add", "-A", cwd=target)
    run("git", "commit", "-m", "bootstrap pair-pressure chat", cwd=target)

    print(f"created chat repo at {target}")
    print(f"  channels: {', '.join(channels)}")
    if args.remote:
        print(f"  remote:   {args.remote}")
        print("  next:     cd "
              f"{target} && git push -u origin main")
    else:
        print("  next:     git remote add origin <url> && git push -u origin main")
    print()
    print("Then on each dev machine:")
    print(f"  git clone <url> ~/code/{target.name}")
    print("  set PAIR_PRESSURE_REPO and PAIR_PRESSURE_AUTHOR in ~/.claude/settings.local.json")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        die(f"git error: {(e.stderr or e.stdout or '').strip()}")
