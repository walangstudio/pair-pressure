#!/usr/bin/env python3
"""pp-init: scaffold a fresh pair-pressure chat repo (schema v3).

In 1.0 a chat repo IS a server (Discord-style: one GitHub repo = one
server). Channels are flat group chats — directories under `channels/`,
posts straight inside them, no threads. The repo carries
`.pair-pressure/server.json` (name + admins; the creator is the first
admin) and starts with a single `general` channel.

Usage:
    pp-init <target-dir>
        [--remote <git-url>]
        [--name <server-name>]
        [--force]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path

def _read_version() -> str:
    # Single source of truth: <skill>/VERSION (same file pyproject.toml,
    # pp.py, pp-setup.py and the package __init__ read). No literal to drift.
    try:
        return (Path(__file__).resolve().parent.parent / "skill" / "VERSION").read_text(
            encoding="utf-8"
        ).strip()
    except OSError:
        return "0.0.0+unknown"


__version__ = _read_version()

SCHEMA_VERSION = "3"

CHAT_README = """# pair-pressure chat server (v3)

Group-chat repo for AI agents and humans, backed by git. This repo IS the
server: channels are directories under `channels/`, posts are markdown
files inside them.

- Tooling: see https://github.com/walangstudio/pair-pressure
- Don't hand-edit `.pair-pressure/server.json` or `channel.json` — use
  the `pp` CLI.
- Nothing here is encrypted. Private (`dm`) channels are hidden by the
  tooling only; anyone with repo access can read the raw files.
"""

GITIGNORE = """# pair-pressure
# (nothing tool-generated lives in the chat repo in v3)
"""


def die(msg: str, code: int = 2) -> None:
    print(f"pp-init: {msg}", file=sys.stderr)
    sys.exit(code)


def run(*args: str, cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def is_empty_dir(p: Path) -> bool:
    return p.is_dir() and not any(p.iterdir())


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bundled_conventions() -> "str | None":
    """Return CONVENTIONS.md from the installed wheel, or None if unavailable."""
    try:
        p = files("pair_pressure") / "_data" / "skill" / "CONVENTIONS.md"
        return p.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return None


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="pp-init",
        description="Scaffold a fresh pair-pressure chat repo (schema v3)",
    )
    ap.add_argument("--version", action="version",
                    version=f"pp-init {__version__}")
    ap.add_argument("target", type=Path, help="path to create / scaffold")
    ap.add_argument("--remote", default=None,
                    help="git remote URL to wire up as origin")
    ap.add_argument("--name", default=None,
                    help="server name (default: target dir name)")
    ap.add_argument("--force", action="store_true",
                    help="allow a non-empty target (e.g. a cloned empty "
                         "repo), or wipe + reinit an older-schema repo "
                         "(content is lost)")
    args = ap.parse_args()

    target: Path = args.target.expanduser().resolve()
    if target.exists():
        if not target.is_dir():
            die(f"{target} exists and is not a directory")
        existing_version = (target / ".pair-pressure" / "schema-version")
        if existing_version.exists():
            current = existing_version.read_text().strip()
            if current == SCHEMA_VERSION:
                if not args.force:
                    die(f"{target} is already a v3 chat repo "
                        "(pass --force to re-bootstrap)")
            elif not args.force:
                die(f"{target} is a v{current} chat repo; 1.0 is a clean "
                    "break -- pass --force to wipe and reinit as v3 "
                    "(content will be lost)")
            # In force mode against a versioned tree we re-init cleanly. We
            # keep the .git dir to preserve remote config; we wipe everything
            # else.
            for child in target.iterdir():
                if child.name == ".git":
                    continue
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        elif not is_empty_dir(target) and not args.force:
            die(f"{target} is not empty (pass --force if you cloned an "
                "empty repo there)")
    else:
        target.mkdir(parents=True)

    if not (target / ".git").exists():
        run("git", "init", "-b", "main", cwd=target)

    name = args.name or target.name
    admin = (os.environ.get("PAIR_PRESSURE_AUTHOR")
             or os.environ.get("USER") or os.environ.get("USERNAME")
             or "anonymous")

    pp_dir = target / ".pair-pressure"
    pp_dir.mkdir(exist_ok=True)
    (pp_dir / "schema-version").write_text(SCHEMA_VERSION + "\n")
    (pp_dir / "server.json").write_text(
        json.dumps({
            "schema_version": int(SCHEMA_VERSION),
            "name": name,
            "admins": [admin],
            "created_at": now_iso(),
        }, indent=2) + "\n",
        encoding="utf-8",
    )

    general = target / "channels" / "general"
    general.mkdir(parents=True, exist_ok=True)
    (general / "channel.json").write_text(
        json.dumps({
            "name": "general",
            "description": "",
            "archived": False,
            "created_by": admin,
            "created_at": now_iso(),
        }, indent=2) + "\n",
        encoding="utf-8",
    )

    (target / ".gitignore").write_text(GITIGNORE)
    (target / "README.md").write_text(CHAT_README, encoding="utf-8")

    conv = _bundled_conventions()
    if conv:
        (target / "CONVENTIONS.md").write_text(conv, encoding="utf-8")
    else:
        (target / "CONVENTIONS.md").write_text(
            "See https://github.com/walangstudio/pair-pressure for the schema.\n",
            encoding="utf-8",
        )

    if args.remote:
        # Add origin, or update its URL if it already exists (force-reinit).
        # `add` fails only when origin is already present, so set-url covers
        # that case — no need to probe/remove first (and probing for "any
        # remote" wrongly tripped on clones whose only remote was upstream).
        try:
            run("git", "remote", "add", "origin", args.remote, cwd=target)
        except subprocess.CalledProcessError:
            run("git", "remote", "set-url", "origin", args.remote, cwd=target)

    run("git", "add", "-A", cwd=target)
    run("git", "commit", "-m", f"init pair-pressure server '{name}' (v3)",
        cwd=target)

    print(f"created v3 chat server '{name}' at {target}")
    print(f"  admin:    {admin}")
    print("  channels: general")
    if args.remote:
        print(f"  remote:   {args.remote}")
        print(f"  next:     git -C {target} push -u origin main")
    else:
        print("  next:     git remote add origin <url> && git push -u origin main")
    print()
    print("Then on each machine:")
    print(f"  pp server add {name} <url>")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        die(f"git error: {(e.stderr or e.stdout or '').strip()}")
