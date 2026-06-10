#!/usr/bin/env python3
"""pp-init: scaffold a fresh pair-pressure shared chat repo (schema v2).

In v0.4 the chat repo is a multi-tenant host: `main` holds the server
registry (no channels), each server lives on its own `server/<name>`
branch (created via `pp server new`), and worktrees materialize per
server on demand.

Usage:
    pp-init <target-dir>
        [--remote <git-url>]
        [--with-server <name>] [--channels general]
        [--force]

Without --with-server the repo starts empty (no servers). Run
`pp server new <name>` afterwards to create the first one. With
--with-server, the first server is scaffolded and pushed in the same
step.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from importlib.resources import files
from pathlib import Path

__version__ = "0.4.2"

SCHEMA_VERSION = "2"

CHAT_README = """# pair-pressure shared chat (v2)

Multi-tenant group-chat repo for AI agents and humans, backed by git.

- `main` holds a thin registry at `.pair-pressure/servers.json`
- Each server lives on a `server/<name>` branch
- Tooling: see https://github.com/walangstudio/pair-pressure

Don't hand-edit `.pair-pressure/servers.json` -- use `pp server new` /
`pp server remove`. Don't add channels on `main` -- they belong on
server branches.
"""

GITIGNORE = """# pair-pressure
.pp-worktrees/
"""


def die(msg: str, code: int = 2) -> None:
    print(f"pp-init: {msg}", file=sys.stderr)
    sys.exit(code)


def run(*args: str, cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def is_empty_dir(p: Path) -> bool:
    return p.is_dir() and not any(p.iterdir())


def _bundled_conventions() -> str | None:
    """Return CONVENTIONS.md from the installed wheel, or None if unavailable."""
    try:
        p = files("pair_pressure") / "_data" / "skill" / "CONVENTIONS.md"
        return p.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return None


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="pp-init",
        description="Scaffold a fresh pair-pressure shared chat repo (v2)",
    )
    ap.add_argument("--version", action="version", version=f"pp-init {__version__}")
    ap.add_argument("target", type=Path, help="path to create / scaffold")
    ap.add_argument("--remote", default=None,
                    help="git remote URL to wire up as origin")
    ap.add_argument("--with-server", default=None, metavar="NAME",
                    help="scaffold an initial server in one step")
    ap.add_argument("--channels", default="general",
                    help="comma-separated channels for --with-server "
                         "(default: general)")
    ap.add_argument("--force", action="store_true",
                    help="allow target if it exists and is empty (or for "
                         "an existing schema-v1 repo, wipe + reinit as v2)")
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
                    die(f"{target} is already a v2 chat repo "
                        "(pass --force to re-bootstrap)")
            elif not args.force:
                die(f"{target} is a v{current} chat repo; v0.4 is a clean "
                    "break -- pass --force to wipe and reinit as v2 "
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

    pp_dir = target / ".pair-pressure"
    pp_dir.mkdir(exist_ok=True)
    (pp_dir / "schema-version").write_text(SCHEMA_VERSION + "\n")
    (pp_dir / "servers.json").write_text(
        json.dumps({"schema_version": int(SCHEMA_VERSION), "servers": []},
                   indent=2) + "\n",
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
        # Replace any existing origin (force-reinit case).
        run("git", "remote", "remove", "origin", cwd=target) if (
            subprocess.run(["git", "remote"], cwd=target,
                           capture_output=True, text=True).stdout.strip()
        ) else None
        try:
            run("git", "remote", "add", "origin", args.remote, cwd=target)
        except subprocess.CalledProcessError:
            # `remote add` fails if the remote already exists; re-try set-url.
            run("git", "remote", "set-url", "origin", args.remote, cwd=target)

    run("git", "add", "-A", cwd=target)
    run("git", "commit", "-m", "init pair-pressure registry v2", cwd=target)

    server_info = None
    if args.with_server:
        # Delegate to `pp server new` so we don't duplicate the worktree +
        # registry-append logic. The pp on PATH MUST be 0.4.x for this to work.
        env = os.environ.copy()
        env["PAIR_PRESSURE_REPO"] = str(target)
        env.setdefault("PAIR_PRESSURE_AUTHOR",
                       env.get("USER") or env.get("USERNAME") or "anonymous")
        cmd = ["pp", "server", "new", args.with_server]
        if args.channels:
            cmd += ["--channels", args.channels]
        res = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if res.returncode != 0:
            die(
                f"--with-server failed: {res.stderr.strip() or res.stdout.strip()}\n"
                f"(repo is scaffolded; create the server manually with "
                f"`pp server new {args.with_server}`)"
            )
        try:
            server_info = json.loads(res.stdout)
        except json.JSONDecodeError:
            server_info = {"raw": res.stdout.strip()}

    print(f"created v2 chat repo at {target}")
    if args.remote:
        print(f"  remote:   {args.remote}")
        print(f"  next:     cd {target} && git push -u origin main")
    else:
        print("  next:     git remote add origin <url> && git push -u origin main")
    if server_info:
        print(f"  initial server: {args.with_server}")
        print(f"    branch:   {server_info.get('branch', f'server/{args.with_server}')}")
        print(f"    channels: {', '.join(server_info.get('channels', []))}")
    else:
        print("  servers:  none yet -- run `pp server new <name>` to create one")
    print()
    print("Then on each dev machine:")
    print(f"  git clone <url> ~/code/{target.name}")
    print("  pp-setup    # sets PAIR_PRESSURE_REPO/AUTHOR/SERVER")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        die(f"git error: {(e.stderr or e.stdout or '').strip()}")
