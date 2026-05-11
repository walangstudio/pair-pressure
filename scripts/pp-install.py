#!/usr/bin/env python3
"""pp-install: interactive onboarding wizard for pair-pressure.

After the bootstrap installer (install.ps1 / install.sh) has placed `pp` on
PATH, this wizard:

  - prompts for author identity (default from `git config user.name`)
  - resolves the chat repo (existing path, clone remote, or pp-init a new one)
  - merges PAIR_PRESSURE_REPO/AUTHOR into ~/.claude/settings.local.json
  - installs the skill at ~/.claude/skills/pair-pressure (junction on Win,
    symlink on POSIX)
  - copies /pp-chat:* slash commands into ~/.claude/commands/pp-chat/
  - runs `pp list-channels` to verify

Re-running on a v0.1 / v0.2 install routes through an upgrade flow that
preserves existing env vars and only overwrites slash commands whose
checksum has changed since the previous canonical version.

Usage:
    pp-install                          fully interactive
    pp-install --yes                    use defaults; fail if no default
    pp-install --author X --repo /path  partial non-interactive
    pp-install --reinstall              skip upgrade detection
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

__version__ = "0.3.0"

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / ".claude" / "skills" / "pair-pressure"
COMMAND_SOURCES = SKILL_DIR / "templates" / "commands"
CLAUDE_HOME = Path.home() / ".claude"
SETTINGS_PATH = CLAUDE_HOME / "settings.local.json"
SETTINGS_GLOBAL_PATH = CLAUDE_HOME / "settings.json"
USER_SKILL_PATH = CLAUDE_HOME / "skills" / "pair-pressure"
USER_COMMANDS_PATH = CLAUDE_HOME / "commands" / "pp-chat"

# Markers for the shell-profile env-var block. Used to find + replace
# idempotently rather than appending duplicates on re-runs.
PROFILE_BEGIN = "# >>> pair-pressure env vars (pp-install) >>>"
PROFILE_END   = "# <<< pair-pressure env vars <<<"
PP_ENV_KEYS = ("PAIR_PRESSURE_REPO", "PAIR_PRESSURE_AUTHOR")


# ---- helpers ----

def die(msg, code=2):
    print(f"pp-install: {msg}", file=sys.stderr)
    sys.exit(code)


def run(*args, cwd=None, check=True, capture=True):
    return subprocess.run(
        args, cwd=cwd, check=check,
        capture_output=capture, text=True,
    )


def git_default(key):
    """Read a value from `git config --global <key>`, or None."""
    try:
        r = run("git", "config", "--global", key, check=False)
        return r.stdout.strip() or None
    except FileNotFoundError:
        return None


# ---- prompt ----

class PromptCtx:
    """Holds the non-interactive flag so prompt() can short-circuit."""
    non_interactive = False

def _render_choice_hint(choices, default):
    """Format a choice prompt hint following the standard "capital letter
    is the default" UX convention.

    Examples:
      ["y","n"], "y"     -> "Y/n"
      ["y","n"], "n"     -> "y/N"
      ["a","b","c"], "b" -> "a/B/c"
      ["1","2","3"], "2" -> "1/2/3, default: 2"     # numbers can't carry case
      ["y","n"], None    -> "y/n"
    """
    all_single_letters = all(len(c) == 1 and c.isalpha() for c in choices)
    if all_single_letters and default:
        return "/".join(
            c.upper() if c.lower() == default.lower() else c.lower()
            for c in choices
        )
    rendered = "/".join(choices)
    if default is not None and default != "":
        rendered += f", default: {default}"
    return rendered


def _matches_choice(raw, choices):
    """Case-insensitive match for single-letter choices, exact match otherwise."""
    if all(len(c) == 1 for c in choices):
        return any(c.lower() == raw.lower() for c in choices)
    return raw in choices


def prompt(label, default=None, choices=None, validate=None):
    """Read a line from stdin. Reprompts on validation failure.

    Non-interactive mode (PromptCtx.non_interactive == True) returns the
    default immediately, or dies if no default was supplied.
    """
    if PromptCtx.non_interactive:
        if default is None:
            die(f"non-interactive mode but no default for: {label}")
        return default
    if choices:
        hint = f" [{_render_choice_hint(choices, default)}]"
    elif default is not None and default != "":
        hint = f" [{default}]"
    else:
        hint = ""
    while True:
        raw = input(f"{label}{hint}: ").strip()
        if not raw and default is not None:
            raw = default
        if choices and not _matches_choice(raw, choices):
            print(f"  must be one of: {', '.join(choices)}")
            continue
        if validate:
            err = validate(raw)
            if err:
                print(f"  {err}")
                continue
        return raw


def yes_no(label, default_yes=True):
    """Yes/no prompt with [Y/n] or [y/N] convention. Returns bool."""
    d = "y" if default_yes else "n"
    return prompt(label, default=d, choices=["y", "n"]).lower() == "y"


# ---- existing install detection ----

def detect_existing_install():
    """Returns (version_str, install_method) if pair-pressure is already on
    PATH; None otherwise. install_method is one of 'uv'/'pipx'/'pip'/'unknown'.
    """
    try:
        v = run("pp", "--version", check=False)
        if v.returncode != 0:
            return None
        m = re.search(r"pair-pressure\s+(\S+)", v.stdout)
        if not m:
            return None
        version = m.group(1).strip()
    except FileNotFoundError:
        return None
    # Probe install method
    probes = (
        ("uv",   ["uv", "tool", "list"]),
        ("pipx", ["pipx", "list"]),
    )
    for name, cmd in probes:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if "pair-pressure" in (r.stdout or ""):
                return (version, name)
        except FileNotFoundError:
            pass
    return (version, "unknown")


def detect_pp_on_path():
    """Returns (path_to_pp, is_pair_pressure_bool) or (None, None)."""
    pp = shutil.which("pp")
    if not pp:
        return (None, None)
    try:
        v = run("pp", "--version", check=False)
        is_ours = "pair-pressure" in (v.stdout or v.stderr or "")
        return (pp, is_ours)
    except FileNotFoundError:
        return (pp, False)


# ---- settings.local.json merge ----

def _merge_into_settings_file(path, env_updates, backup=True):
    """Merge env_updates into the `env` block of a Claude Code settings file.

    Tolerates:
      - file absent           -> create with {"env": {...}}
      - file empty / blanks   -> treat as {}
      - file has UTF-8 BOM    -> stripped (PowerShell 5.1 Set-Content -Encoding utf8
                                  writes BOM by default; Python's json.loads rejects
                                  the leading \\ufeff with "Expecting value")
      - file is invalid JSON  -> dies with a clear pointer to the .bak

    Writes without BOM so subsequent reads by any tool stay consistent.
    """
    data = {}
    if path.exists():
        text = path.read_text(encoding="utf-8-sig").strip()
        if text:
            try:
                data = json.loads(text)
            except json.JSONDecodeError as e:
                bak = path.with_suffix(".json.bak")
                die(
                    f"existing {path} is not valid JSON: {e}\n"
                    f"  Fix the JSON manually, or delete the file and re-run.\n"
                    + (f"  (previous backup at {bak})" if bak.exists() else "")
                )
        if backup:
            shutil.copy(path, path.with_suffix(".json.bak"))
    env = data.setdefault("env", {})
    env.update(env_updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return data


def merge_settings(env_updates, backup=True):
    """Write env vars to BOTH Claude Code settings files.

    Some Claude Code builds honor `~/.claude/settings.local.json`, others
    only `~/.claude/settings.json`. We write to both so the env vars are
    picked up regardless of which file the build trusts. The shell-profile
    write below is the belt-and-braces third layer.
    """
    data = _merge_into_settings_file(SETTINGS_PATH, env_updates, backup=backup)
    _merge_into_settings_file(SETTINGS_GLOBAL_PATH, env_updates, backup=backup)
    return data


def _shell_profile_block(env_updates):
    """Render the marker-wrapped env-var block for a POSIX or PowerShell profile."""
    if os.name == "nt":
        lines = [PROFILE_BEGIN]
        for k, v in env_updates.items():
            # PowerShell needs backslashes in double-quoted strings to be
            # left alone (no special meaning), so a single-quoted form is
            # safer when values contain backslashes.
            lines.append(f"$env:{k} = '{v}'")
        lines.append(PROFILE_END)
        return "\r\n".join(lines)
    lines = [PROFILE_BEGIN]
    for k, v in env_updates.items():
        # POSIX: single-quote to avoid expansion.
        v_esc = v.replace("'", "'\"'\"'")
        lines.append(f"export {k}='{v_esc}'")
    lines.append(PROFILE_END)
    return "\n".join(lines)


def write_shell_profile(env_updates):
    """Idempotently insert the env-var block into the user's shell profile(s).

    On Windows: writes to $PROFILE.CurrentUserAllHosts (Documents/WindowsPowerShell/profile.ps1).
    On POSIX:   writes to ~/.bashrc AND ~/.zshrc if they exist; creates ~/.profile if neither does.

    Idempotency: an existing block (between PROFILE_BEGIN and PROFILE_END
    markers) is REPLACED, not appended. So re-running the wizard with new
    values updates in place.
    """
    block = _shell_profile_block(env_updates)
    targets = []
    if os.name == "nt":
        candidates = [
            Path.home() / "Documents" / "WindowsPowerShell" / "profile.ps1",
            Path.home() / "OneDrive" / "Documents" / "WindowsPowerShell" / "profile.ps1",
        ]
        # Pick the one that exists, or the first if neither does.
        targets.append(next((c for c in candidates if c.parent.exists()), candidates[0]))
    else:
        for rc in (".bashrc", ".zshrc"):
            p = Path.home() / rc
            if p.exists():
                targets.append(p)
        if not targets:
            targets.append(Path.home() / ".profile")

    pattern = re.compile(
        re.escape(PROFILE_BEGIN) + r"[\s\S]*?" + re.escape(PROFILE_END),
        re.MULTILINE,
    )
    written = []
    for path in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(encoding="utf-8-sig") if path.exists() else ""
        if pattern.search(existing):
            new_text = pattern.sub(block, existing)
            action = "updated"
        else:
            sep = "" if not existing or existing.endswith("\n") else "\n"
            new_text = f"{existing}{sep}\n{block}\n" if existing else f"{block}\n"
            action = "appended to"
        path.write_text(new_text, encoding="utf-8")
        written.append((path, action))
    return written


# ---- skill install ----

def install_skill():
    """Junction (Windows) or symlink (POSIX) the skill into ~/.claude/skills.

    Idempotent: if the destination already exists and points at the right
    place, no-op. If it points somewhere else, leaves it alone and warns.
    """
    src = SKILL_DIR
    dst = USER_SKILL_PATH
    if dst.exists() or dst.is_symlink():
        # Resolve and compare. On Windows, junctions resolve via .resolve().
        try:
            existing_target = dst.resolve(strict=False)
            if existing_target == src.resolve():
                return "already-installed"
        except OSError:
            pass
        return "exists-different-target"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        # mklink /j needs string paths and works without admin / dev mode
        r = subprocess.run(
            ["cmd", "/c", "mklink", "/j", str(dst), str(src)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            die(f"failed to create junction: {r.stderr.strip() or r.stdout.strip()}")
    else:
        dst.symlink_to(src)
    return "installed"


# ---- slash command install ----

def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def install_slash_commands(bin_name="pp", force_overwrite=False):
    """Copy slash command sources into ~/.claude/commands/pp-chat/.

    On collision (file already exists), compare checksums:
      - identical: no-op
      - different: prompt user (or honor force_overwrite); never blind clobber
    If bin_name != 'pp', also rewrite the file content so `pp ` becomes
    `<bin_name> ` before writing.
    """
    if not COMMAND_SOURCES.is_dir():
        die(f"missing canonical slash command sources at {COMMAND_SOURCES}")
    USER_COMMANDS_PATH.mkdir(parents=True, exist_ok=True)
    actions = {"new": 0, "updated": 0, "kept": 0, "unchanged": 0}
    for src in sorted(COMMAND_SOURCES.glob("*.md")):
        body = src.read_text()
        if bin_name != "pp":
            body = re.sub(r"\bpp\b", bin_name, body)
        dst = USER_COMMANDS_PATH / src.name
        if not dst.exists():
            dst.write_text(body)
            actions["new"] += 1
            continue
        if dst.read_text() == body:
            actions["unchanged"] += 1
            continue
        # Different. Ask, unless force.
        if force_overwrite or (
            not PromptCtx.non_interactive
            and yes_no(f"  overwrite customized {src.name}?", default_yes=False)
        ):
            dst.write_text(body)
            actions["updated"] += 1
        else:
            actions["kept"] += 1
    return actions


# ---- chat repo resolution ----

def resolve_target_path(user_input, default):
    """Turn a user-supplied path string into a sensibly-located absolute Path.

    Rules:
      empty input          -> the default (as-is)
      absolute path        -> use as-is
      relative w/ separator -> resolve against $HOME (not cwd, since the
                               user usually runs the wizard from the source
                               repo and cwd-relative would dump chat data
                               inside the tooling clone)
      bare name (no sep)   -> adopt the default's parent directory; so
                               `pp-chat-test` with default `~/code/foo`
                               becomes `~/code/pp-chat-test`
    """
    if not user_input:
        return Path(default).expanduser().resolve()
    p = Path(user_input).expanduser()
    if p.is_absolute():
        return p.resolve()
    if "/" in user_input or "\\" in user_input:
        return (Path.home() / p).resolve()
    return (Path(default).expanduser().parent / user_input).resolve()


def repo_name_from_url(url):
    """Extract the repo name from a git URL.

    Handles common forms:
      https://github.com/org/repo
      https://github.com/org/repo.git
      git@github.com:org/repo.git
      ssh://git@host/path/repo.git

    Returns None if the URL is empty or the name can't be determined cleanly.
    """
    if not url:
        return None
    name = re.split(r"[/:]", url.rstrip("/"))[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name or None


def _refuse_inside_tooling(target):
    """Hard-fail if target lands inside the tooling repo (REPO_ROOT).

    Cloning chat data into the tooling clone is almost never what the user
    intended -- they get a nested repo and confusing git output. Hard-error
    with a clear message rather than silently doing the wrong thing.
    """
    try:
        target.relative_to(REPO_ROOT)
    except ValueError:
        return  # target is NOT inside the tooling repo -- good
    die(
        f"Refusing to place chat repo at {target}\n"
        f"  -- that's inside the tooling repo ({REPO_ROOT}).\n"
        f"  Chat data should live elsewhere. Re-run the wizard and supply\n"
        f"  an absolute path (e.g. {Path.home() / 'code' / target.name}),\n"
        f"  or just press Enter at the prompt to accept the default."
    )


def resolve_chat_repo(args):
    """Returns (path_to_chat_repo, created_new_bool)."""
    if args.repo:
        # Non-interactive path: take what was given. If --create-if-missing
        # and the path doesn't exist, pp-init it.
        target = resolve_target_path(args.repo, str(Path.home() / "code" / "pair-pressure-chat"))
        _refuse_inside_tooling(target)
        if not target.exists() and args.create_if_missing:
            _pp_init(target, args.channels, args.remote)
            return (target, True)
        if not (target / ".git").exists():
            die(f"{target} is not a git repository")
        # Non-interactive: scaffold automatically if the git dir has no
        # pair-pressure layout and --create-if-missing was set. Otherwise
        # error out -- silent scaffolding without consent would be wrong.
        if not _is_scaffolded(target):
            if args.create_if_missing:
                _pp_init(target, args.channels, remote=None, force=True)
            else:
                die(f"{target} is a git repo but not scaffolded as a pair-pressure "
                    f"chat (no .pair-pressure/schema-version). Pass --create-if-missing "
                    f"to auto-scaffold, or run pp-init manually.")
        return (target, False)

    print("Where is your chat repo?")
    print("  1) I have an existing local clone")
    print("  2) Clone it from a remote URL")
    print("  3) Initialize a brand-new local one (calls pp-init)")
    choice = prompt("choice", default="1", choices=["1", "2", "3"])

    default_dir = str(Path.home() / "code" / "pair-pressure-chat")
    if choice == "1":
        target = resolve_target_path(prompt("Path to existing chat repo", default=default_dir), default_dir)
        _refuse_inside_tooling(target)
        if not (target / ".git").exists():
            die(f"{target} is not a git repository")
        return (target, False)

    if choice == "2":
        url = prompt("Remote URL (git@... or https://...)")
        # Default the clone target to a sibling of ~/code/ named after the
        # remote repo (matches `git clone <url>` behavior). Falls back to
        # the generic default if the URL doesn't parse.
        derived = repo_name_from_url(url)
        clone_default = str(Path.home() / "code" / derived) if derived else default_dir
        target = resolve_target_path(
            prompt("Clone to (absolute path, or bare name to use the default's parent dir)",
                   default=clone_default),
            clone_default,
        )
        _refuse_inside_tooling(target)
        if target.exists() and any(target.iterdir()):
            die(f"{target} exists and is not empty")
        target.parent.mkdir(parents=True, exist_ok=True)
        print(f"Cloning {url} -> {target} ...")
        run("git", "clone", url, str(target), capture=False)
        # The remote might be empty (newly created on GitHub, never pushed)
        # or it might be a real-but-non-pair-pressure repo. Either way, if
        # there's no .pair-pressure/schema-version we'd hit a confusing
        # "channel does not exist" error later. Offer to scaffold now.
        _scaffold_if_needed(target, url=url)
        return (target, True)

    # choice == "3"
    target = resolve_target_path(
        prompt("Init at (absolute path, or bare name to use the default's parent dir)",
               default=default_dir),
        default_dir,
    )
    _refuse_inside_tooling(target)
    channels = prompt("Channels (comma-separated)", default="general,planning,brainstorm")
    remote = prompt("Remote URL to set as origin (blank to skip)", default="")
    _pp_init(target, channels, remote or None)
    return (target, True)


def _pp_init(target, channels, remote, force=False):
    """Invoke pp-init on a target dir.

    `force=True` lets pp-init proceed when the target already exists with
    content (the typical case: we just `git clone`d an empty remote, so
    the target has a `.git/` directory and nothing else). pp-init's own
    `git init -b main` is idempotent, so it won't damage existing git
    state -- it will leave `origin` configured to whatever the clone set.
    """
    pp_init = REPO_ROOT / "scripts" / "pp-init.py"
    args = [sys.executable, str(pp_init), str(target), "--channels", channels]
    if remote:
        args += ["--remote", remote]
    if force:
        args += ["--force"]
    print(f"Running pp-init {target} ...")
    subprocess.run(args, check=True)


def _is_scaffolded(target):
    """Return True iff `target` looks like a pair-pressure chat repo."""
    return (target / ".pair-pressure" / "schema-version").is_file()


def _has_remote(target):
    """Return True iff the git repo at `target` has an `origin` remote configured."""
    r = subprocess.run(
        ["git", "-C", str(target), "remote"],
        capture_output=True, text=True, check=False,
    )
    return "origin" in (r.stdout or "")


def _push_initial_commit(target):
    """`git push -u origin main` from `target`. Returns (success, message)."""
    r = subprocess.run(
        ["git", "-C", str(target), "push", "-u", "origin", "main"],
        capture_output=True, text=True, check=False,
    )
    if r.returncode == 0:
        return (True, "pushed scaffold to origin/main")
    return (False, (r.stderr or r.stdout).strip())


def _scaffold_if_needed(target, url=None):
    """If `target` is a git dir without pair-pressure scaffolding, offer to
    create it. Used by the wizard right after a `git clone`. The clone may
    have brought down an empty remote, or a populated-but-different repo.

    After scaffolding, if `target` has an `origin` remote, offer to push
    the initial commit. Otherwise `pp new-thread` and friends would hit
    "fatal: ambiguous argument 'origin/main'" on the first write
    (push_with_retry handles this in v0.3+, but pushing now also avoids
    the trap for any older `pp` in the wild).
    """
    if _is_scaffolded(target):
        return
    print()
    print(f"  {target} is a git repo but has no pair-pressure scaffolding yet.")
    print(f"  (probably: the remote is empty, or it's not a pair-pressure chat repo).")
    if not yes_no("  Scaffold it now? (channels + schema-version + initial commit)",
                  default_yes=True):
        die("Aborting -- chat repo is not scaffolded. "
            "Run pp-init on it manually, or rerun pp-install and pick option 3.")
    channels = prompt("Channels (comma-separated)",
                      default="general,planning,brainstorm")
    # Don't pass --remote: the clone already set origin. pp-init's --remote
    # would try to `git remote add origin` and fail with "already exists".
    _pp_init(target, channels, remote=None, force=True)

    if _has_remote(target):
        if yes_no(f"  Push the scaffold to origin now? "
                  f"(otherwise future `pp` ops will hit empty-remote errors)",
                  default_yes=True):
            ok, msg = _push_initial_commit(target)
            if ok:
                print(f"  {msg}")
            else:
                print(f"  push failed: {msg}")
                print(f"  You can push manually later:")
                print(f"    cd {target}")
                print(f"    git push -u origin main")
        else:
            print(f"  Skipped. Push manually before using pp:")
            print(f"    cd {target}")
            print(f"    git push -u origin main")


# ---- verification ----

def verify(chat_repo, author):
    """Sanity-check that `pp list-channels` works with the new env."""
    env = os.environ.copy()
    env["PAIR_PRESSURE_REPO"] = str(chat_repo)
    env["PAIR_PRESSURE_AUTHOR"] = author
    pp = shutil.which("pp")
    if not pp:
        return ("skip", "pp not on PATH yet — restart your shell and re-run pp-install to verify")
    r = subprocess.run([pp, "list-channels"], env=env, capture_output=True, text=True)
    if r.returncode != 0:
        return ("fail", (r.stderr or r.stdout).strip())
    try:
        channels = json.loads(r.stdout)
        return ("ok", f"{len(channels)} channel(s) visible: {', '.join(c['name'] for c in channels)}")
    except json.JSONDecodeError:
        return ("fail", "pp list-channels did not return JSON")


# ---- upgrade flow ----

def upgrade_flow(existing_version, install_method, args):
    """Re-install package via same method, refresh slash commands, preserve env."""
    print(f"\nFound existing pair-pressure {existing_version} (installed via: {install_method})")
    if not yes_no(f"Upgrade to {__version__}?", default_yes=True):
        print("Cancelled.")
        sys.exit(0)
    print()
    print(f"[1/4] Reinstalling package via {install_method}...")
    if install_method == "uv":
        run("uv", "tool", "install", "--editable", str(REPO_ROOT), "--reinstall", capture=False, check=False)
    elif install_method == "pipx":
        run("pipx", "install", "--editable", str(REPO_ROOT), "--force", capture=False, check=False)
    elif install_method == "pip":
        run(sys.executable, "-m", "pip", "install", "--user", "--editable",
            str(REPO_ROOT), "--upgrade", capture=False, check=False)
    else:
        print(f"  unknown install method; skipping package step. Run `{install_method} upgrade pair-pressure` manually.")

    print("[2/4] Refreshing slash commands (preserving any you customized)...")
    actions = install_slash_commands(bin_name=args.bin_name, force_overwrite=args.overwrite_commands)
    print(f"   new={actions['new']} updated={actions['updated']} kept-customized={actions['kept']} unchanged={actions['unchanged']}")

    print("[3/4] Preserving existing settings.local.json env vars...")
    existing = {}
    if SETTINGS_PATH.exists():
        try:
            existing = json.loads(SETTINGS_PATH.read_text() or "{}").get("env", {})
        except json.JSONDecodeError:
            pass
    for k in ("PAIR_PRESSURE_REPO", "PAIR_PRESSURE_AUTHOR"):
        v = existing.get(k)
        mark = "OK" if v else "MISSING"
        print(f"   {k}: {mark}{' = ' + v if v else ''}")

    print("[4/4] Running verification...")
    repo = existing.get("PAIR_PRESSURE_REPO")
    author = existing.get("PAIR_PRESSURE_AUTHOR")
    if repo and author:
        status, msg = verify(Path(repo), author)
        print(f"   {status}: {msg}")
    else:
        print("   skipped (env vars incomplete) — run `pp-install` without --yes to fix")

    print(f"\nUpgraded to {__version__}. Restart Claude Code to pick up any new env vars.")


# ---- fresh install flow ----

def fresh_install_flow(args):
    print("pair-pressure setup wizard")
    print()

    # Identity
    default_author = git_default("user.name") or os.environ.get("USER") or os.environ.get("USERNAME")
    default_email  = git_default("user.email")
    author = args.author or prompt(
        "Author identity (used in post frontmatter)",
        default=default_author,
        validate=lambda s: None if s else "must not be blank",
    )
    email = args.email or prompt(
        "Author email (used to configure git on the chat repo)",
        default=default_email or "",
    )

    # Chat repo
    chat_repo, _ = resolve_chat_repo(args)

    # If we just cloned/init'd, set git config on the chat repo so commits
    # carry the user's identity.
    if email:
        run("git", "-C", str(chat_repo), "config", "user.email", email, check=False)
    if author:
        run("git", "-C", str(chat_repo), "config", "user.name", author, check=False)

    # Skill
    do_skill = args.no_skill is False and (
        args.skill or yes_no(
            f"\nInstall the Claude Code skill at {USER_SKILL_PATH}?",
            default_yes=True,
        )
    )
    if do_skill:
        result = install_skill()
        print(f"  skill: {result}")

    # Slash commands
    do_commands = args.no_commands is False and (
        args.commands or yes_no(
            f"Install /pp-chat:* slash commands at {USER_COMMANDS_PATH}?",
            default_yes=True,
        )
    )
    if do_commands:
        actions = install_slash_commands(bin_name=args.bin_name, force_overwrite=args.overwrite_commands)
        print(f"  slash commands: new={actions['new']} updated={actions['updated']} kept-customized={actions['kept']} unchanged={actions['unchanged']}")

    # Settings merge -- write to BOTH settings.local.json and settings.json,
    # plus the user's shell profile, so env vars are picked up regardless
    # of which mechanism the Claude Code build honors.
    env_updates = {
        "PAIR_PRESSURE_REPO": str(chat_repo),
        "PAIR_PRESSURE_AUTHOR": author,
    }
    print(f"\nMerging env vars into:")
    print(f"  {SETTINGS_PATH}")
    print(f"  {SETTINGS_GLOBAL_PATH}")
    print(f"  + PAIR_PRESSURE_REPO   = {chat_repo}")
    print(f"  + PAIR_PRESSURE_AUTHOR = {author}")
    merge_settings(env_updates)
    for path, action in write_shell_profile(env_updates):
        print(f"  shell profile: {action} {path}")

    # Verify
    print("\nRunning verification (pp list-channels)...")
    status, msg = verify(chat_repo, author)
    print(f"  {status}: {msg}")

    print()
    print("Done. If you have Claude Code open, restart it so env vars take effect.")


# ---- main ----

def main():
    ap = argparse.ArgumentParser(
        prog="pp-install",
        description="pair-pressure setup wizard",
    )
    ap.add_argument("--version", action="version", version=f"pp-install {__version__}")
    ap.add_argument("--yes", action="store_true",
                    help="non-interactive; use defaults; fail if no default")
    ap.add_argument("--reinstall", action="store_true",
                    help="skip upgrade detection, force full fresh install")
    ap.add_argument("--bin-name", default="pp",
                    help="installed binary name (default: pp; useful if another `pp` is on PATH)")
    ap.add_argument("--overwrite-commands", action="store_true",
                    help="overwrite customized slash command files without prompting")

    # Non-interactive overrides
    ap.add_argument("--author", default=None, help="PAIR_PRESSURE_AUTHOR value")
    ap.add_argument("--email",  default=None, help="git user.email on the chat repo clone")
    ap.add_argument("--repo",   default=None, help="path to chat repo (skips repo prompt)")
    ap.add_argument("--remote", default=None, help="remote URL when initialising a fresh chat repo")
    ap.add_argument("--channels", default="general,planning,brainstorm",
                    help="channels when --create-if-missing creates a fresh chat repo")
    ap.add_argument("--create-if-missing", action="store_true",
                    help="if --repo path doesn't exist, run pp-init on it")

    # Yes/no toggles for skill+commands
    ap.add_argument("--skill",    action="store_true", help="install the skill (default: prompt yes)")
    ap.add_argument("--no-skill", action="store_true", help="skip skill install")
    ap.add_argument("--commands",    action="store_true", help="install slash commands (default: prompt yes)")
    ap.add_argument("--no-commands", action="store_true", help="skip slash commands install")

    args = ap.parse_args()

    if args.yes:
        PromptCtx.non_interactive = True

    # Upgrade vs fresh
    if not args.reinstall:
        existing = detect_existing_install()
        if existing and existing[0] != __version__:
            upgrade_flow(existing[0], existing[1], args)
            return
        if existing and existing[0] == __version__:
            print(f"pair-pressure {__version__} already installed.")
            if PromptCtx.non_interactive or not yes_no(
                "Run the wizard anyway to update config?", default_yes=False
            ):
                return

    # Warn if a non-pair-pressure `pp` is on PATH (collision)
    pp_path, is_ours = detect_pp_on_path()
    if pp_path and is_ours is False:
        print(f"\nWARNING: a different `pp` is on PATH at {pp_path}")
        print("  This installer will not change PATH order. To avoid the shadow,")
        print("  re-run with --bin-name pair-pp and update your shell aliases.")
        if not PromptCtx.non_interactive:
            if not yes_no("Continue anyway?", default_yes=False):
                print("Cancelled.")
                sys.exit(1)

    fresh_install_flow(args)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        die(f"command failed: {' '.join(e.cmd) if hasattr(e, 'cmd') else e}\n{(e.stderr or e.stdout or '').strip()}")
    except KeyboardInterrupt:
        print()
        die("aborted by user", code=130)
