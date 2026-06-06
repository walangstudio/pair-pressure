#!/usr/bin/env python3
"""pp-setup: interactive onboarding wizard for pair-pressure.

(Also reachable as the legacy name `pp-install`; both console scripts
dispatch here.)

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
    pp-setup                            fully interactive
    pp-setup --yes                      use defaults; fail if no default
    pp-setup --author X --repo /path    partial non-interactive
    pp-setup --reinstall                skip upgrade detection
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
from importlib.resources import files
from pathlib import Path

def _read_version() -> str:
    # Single source of truth: _data/skill/VERSION. pp-setup.py is at
    # _data/scripts/pp-setup.py, so the file is at ../skill/VERSION relative
    # to this script — works in both editable and wheel installs.
    try:
        return (Path(__file__).resolve().parent.parent / "skill" / "VERSION").read_text(
            encoding="utf-8"
        ).strip()
    except OSError:
        return "0.0.0+unknown"


__version__ = _read_version()

# `__file__` resolves to one of:
#   - editable install: <repo>/src/pair_pressure/_data/scripts/pp-setup.py
#   - wheel install:    <venv>/Lib/site-packages/pair_pressure/_data/scripts/pp-setup.py
# parents[1] = .../_data ; parents[2] = .../pair_pressure ; parents[3] = .../src or site-packages
DATA_ROOT = Path(__file__).resolve().parent.parent  # _data/
PP_INIT_SCRIPT = DATA_ROOT / "scripts" / "pp-init.py"


def _bundled_skill_root() -> Path:
    """Locate the skill source tree via importlib.resources.

    Works for both editable (source dir) and wheel (site-packages) installs.
    Falls back to DATA_ROOT/skill which equals the resource path when the
    package is importable.
    """
    try:
        return Path(str(files("pair_pressure") / "_data" / "skill"))
    except (ModuleNotFoundError, OSError):
        return DATA_ROOT / "skill"


SKILL_DIR = _bundled_skill_root()
COMMAND_SOURCES = SKILL_DIR / "templates" / "commands"
CLAUDE_HOME = Path.home() / ".claude"
SETTINGS_PATH = CLAUDE_HOME / "settings.local.json"
SETTINGS_GLOBAL_PATH = CLAUDE_HOME / "settings.json"
USER_SKILL_PATH = CLAUDE_HOME / "skills" / "pair-pressure"
USER_COMMANDS_PATH = CLAUDE_HOME / "commands" / "pp-chat"

# Markers for the shell-profile env-var block. Used to find + replace
# idempotently rather than appending duplicates on re-runs. The marker text
# stays `(pp-install)` even after the pp-install → pp-setup rename so existing
# profile blocks written by older versions are still matched and replaced
# rather than duplicated.
PROFILE_BEGIN = "# >>> pair-pressure env vars (pp-install) >>>"
PROFILE_END   = "# <<< pair-pressure env vars <<<"
PP_ENV_KEYS = ("PAIR_PRESSURE_REPO", "PAIR_PRESSURE_AUTHOR",
               "PAIR_PRESSURE_SERVER", "PAIR_PRESSURE_ALIAS")

# Short, distinctive names used as the random default for the AI alias.
# Single-word, capitalised, ASCII only. Add freely; the only constraint is
# that nothing collides with `PAIR_PRESSURE_AUTHOR` defaults like git names.
_ALIAS_POOL = (
    "Echo", "Nova", "Iris", "Atlas", "Sage", "Vega", "Lyra", "Orion",
    "Nyx", "Onyx", "Juno", "Halo", "Ember", "Cipher", "Pixel", "Quill",
    "Rune", "Talon", "Vox", "Wren", "Zephyr", "Aria", "Cosmo", "Flare",
    "Glyph", "Kairos", "Mira", "Pulse", "Solace", "Tempo", "Indigo",
    "Kestrel", "Lumen", "Phoenix",
)
_ALIAS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")


def random_alias():
    """Pick a fresh random alias from the pool. Used as the install default."""
    return random.choice(_ALIAS_POOL)


# ---- helpers ----

def die(msg, code=2):
    print(f"pp-setup: {msg}", file=sys.stderr)
    sys.exit(code)


def require_git():
    """Hard-fail with platform-specific install pointers if git isn't on PATH.

    pair-pressure is a thin layer over `git`; every read and every write
    shells out. Failing fast here is much cleaner than the user hitting a
    confusing `FileNotFoundError: 'git'` mid-wizard.
    """
    if shutil.which("git"):
        return
    if os.name == "nt":
        hint = (
            "  Windows: https://git-scm.com/download/win\n"
            "           or:  winget install --id Git.Git -e\n"
            "           or:  choco install git"
        )
    elif sys.platform == "darwin":
        hint = (
            "  macOS:   brew install git\n"
            "           or install the Xcode Command Line Tools:  xcode-select --install"
        )
    else:
        hint = (
            "  Debian/Ubuntu:  sudo apt install git\n"
            "  Fedora/RHEL:    sudo dnf install git\n"
            "  Arch:           sudo pacman -S git\n"
            "  Source/docs:    https://git-scm.com/download/linux"
        )
    die(
        "git is required (pair-pressure is a thin layer over `git`) but was "
        "not found on PATH.\n" + hint + "\nReopen your shell after install, "
        "then re-run pp-setup.",
        code=3,
    )


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

def _merge_permissions_into_settings_file(path, allow_entries):
    """Merge allow_entries into permissions.allow in a Claude Code settings file.

    Creates the file and the permissions.allow list if absent; deduplicates
    entries so re-running the wizard is idempotent.
    """
    data = {}
    if path.exists():
        text = path.read_text(encoding="utf-8-sig").strip()
        if text:
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                return  # don't clobber a broken file; env merge will catch it
    perms = data.setdefault("permissions", {})
    existing = perms.setdefault("allow", [])
    for entry in allow_entries:
        if entry not in existing:
            existing.append(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def merge_permissions(bin_name="pp"):
    """Add pp-related bash commands to permissions.allow in both settings files."""
    entries = [
        f"Bash({bin_name})",
        f"Bash({bin_name} *)",
        "Bash(pp-init *)",
        "Bash(pp-setup *)",
        # Legacy alias name; still on PATH, still callable.
        "Bash(pp-install *)",
        "Bash(pair-pressure-mcp *)",
    ]
    _merge_permissions_into_settings_file(SETTINGS_PATH, entries)
    _merge_permissions_into_settings_file(SETTINGS_GLOBAL_PATH, entries)


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
            # Use a lambda so re.sub treats `block` as a literal string. Direct
            # substitution interprets backslash sequences in the replacement
            # (Windows paths like `C:\Users\...` -> `bad escape \U`).
            new_text = pattern.sub(lambda _m: block, existing)
            action = "updated"
        else:
            sep = "" if not existing or existing.endswith("\n") else "\n"
            new_text = f"{existing}{sep}\n{block}\n" if existing else f"{block}\n"
            action = "appended to"
        path.write_text(new_text, encoding="utf-8")
        written.append((path, action))
    return written


# ---- MCP client config (non-Claude clients) ----

# Each entry: (snippet filename, shape, human-readable canonical destination).
# We write a ready-to-use snippet rather than mutating the client's real
# config in place -- the on-disk location of several of these (Cline/Kilo
# live in editor extension storage) varies by OS/editor/version, and a
# wrong-path or malformed write is worse than a copy-paste. CLIENTS.md lists
# the canonical paths.
MCP_CLIENTS = {
    "cursor":   ("cursor.mcp.json",   "mcpservers",
                 "~/.cursor/mcp.json (global) or <project>/.cursor/mcp.json"),
    "cline":    ("cline.mcp.json",    "mcpservers",
                 "Cline panel > MCP Servers > Configure (cline_mcp_settings.json)"),
    "kilo":     ("kilo.mcp.json",     "mcpservers",
                 "Kilo Code > MCP settings (mcp_settings.json)"),
    "opencode": ("opencode.json",     "opencode",
                 "~/.config/opencode/opencode.json"),
    "codex":    ("codex.config.toml", "toml",
                 "~/.codex/config.toml"),
}


def _mcp_env(chat_repo, author, alias=None):
    env = {
        "PAIR_PRESSURE_REPO": str(chat_repo),
        "PAIR_PRESSURE_AUTHOR": author,
    }
    if alias:
        env["PAIR_PRESSURE_ALIAS"] = alias
    return env


def _mcp_snippet(shape, env):
    """Render an MCP server config snippet for the given client shape."""
    if shape == "mcpservers":  # Cursor / Cline / Kilo
        return json.dumps({
            "mcpServers": {
                "pair-pressure": {"command": "pair-pressure-mcp", "env": env}
            }
        }, indent=2) + "\n"
    if shape == "opencode":
        return json.dumps({
            "$schema": "https://opencode.ai/config.json",
            "mcp": {
                "pair-pressure": {
                    "type": "local",
                    "command": ["pair-pressure-mcp"],
                    "enabled": True,
                    "environment": env,
                }
            }
        }, indent=2) + "\n"
    if shape == "toml":  # Codex CLI
        def tstr(s):  # TOML basic-string escape (Windows paths have backslashes)
            return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
        lines = ["[mcp_servers.pair-pressure]", 'command = "pair-pressure-mcp"']
        env_pairs = ", ".join(f"{k} = {tstr(v)}" for k, v in env.items())
        lines.append(f"env = {{ {env_pairs} }}")
        return "\n".join(lines) + "\n"
    raise ValueError(f"unknown MCP client shape: {shape}")


def write_mcp_client_config(client, chat_repo, author, alias=None):
    """Write a ready-to-use MCP config snippet for `client` under
    ~/.pair-pressure/mcp/. Returns (snippet_path, canonical_destination).
    Idempotent: overwrites the snippet each run."""
    filename, shape, dest = MCP_CLIENTS[client]
    env = _mcp_env(chat_repo, author, alias)
    content = _mcp_snippet(shape, env)
    out_dir = Path.home() / ".pair-pressure" / "mcp"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / filename
    path.write_text(content, encoding="utf-8")
    return path, dest


# ---- skill install ----

def _is_junction_or_symlink(p: Path) -> bool:
    if p.is_symlink():
        return True
    if os.name == "nt":
        # Windows junctions are not symlinks; detect via FILE_ATTRIBUTE_REPARSE_POINT.
        try:
            import stat
            return bool(p.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)  # type: ignore[attr-defined]
        except (OSError, AttributeError):
            return False
    return False


def install_skill():
    """Copy the bundled skill tree into ~/.claude/skills/pair-pressure.

    v0.4 ships skill data inside the wheel (`pair_pressure._data.skill`),
    so the source IS the install. We copy rather than junction so the
    user's source clone can be deleted/moved without breaking the skill.

    Idempotent:
      - missing destination -> copy
      - existing copy       -> overwrite (skill is the authoritative source)
      - junction/symlink (old v0.3 install) -> remove + replace with a copy

    Writes ~/.claude/skills/pair-pressure/.pp-version so future re-runs
    can detect stale skill files vs the installed wheel.
    """
    src = SKILL_DIR
    dst = USER_SKILL_PATH
    if not src.is_dir():
        die(f"bundled skill not found at {src} (wheel built without _data?)")

    dst.parent.mkdir(parents=True, exist_ok=True)
    action = "installed"
    if dst.exists() or _is_junction_or_symlink(dst):
        if _is_junction_or_symlink(dst):
            # v0.3 left a junction here. Remove it cleanly so we can copy.
            try:
                if os.name == "nt":
                    # rmdir works for junctions; unlink fails. shutil handles both.
                    os.rmdir(dst)
                else:
                    dst.unlink()
            except OSError:
                die(f"cannot remove existing junction/symlink at {dst} -- "
                    "delete it manually and re-run pp-setup")
            action = "replaced-junction"
        else:
            shutil.rmtree(dst)
            action = "refreshed"

    shutil.copytree(src, dst)
    (dst / ".pp-version").write_text(__version__ + "\n", encoding="utf-8")
    return action


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
    """Hard-fail if target lands inside the install-time tooling clone.

    v0.4 installs are wheel-based by default, so the "tooling repo" is the
    site-packages dir -- never a place a user would deliberately put their
    chat repo. We still refuse if the target's resolved path contains a
    `pyproject.toml`, which is a strong "this is a Python project" signal.
    """
    for parent in [target, *target.parents]:
        if (parent / "pyproject.toml").is_file():
            die(
                f"Refusing to place chat repo at {target}\n"
                f"  -- {parent} is a Python project root (pyproject.toml exists).\n"
                f"  Chat data should live in its own dir. Re-run the wizard and\n"
                f"  supply an absolute path (e.g. {Path.home() / 'code' / target.name})."
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
    remote = prompt("Remote URL to set as origin (blank to skip)", default="")
    # In v0.4 pp-init scaffolds the registry only -- channels live on server
    # branches. We'll prompt to create the first server after chat-repo
    # resolution, so the "init" branch here is just the empty registry.
    _pp_init(target, channels="", remote=remote or None)
    return (target, True)


def _pp_init(target, channels, remote, force=False, with_server=None):
    """Invoke pp-init on a target dir.

    v0.4 scaffolds an empty v2 registry. If `with_server` is provided,
    pp-init also runs `pp server new <name> --channels <channels>` to
    create an initial server in one step.

    `force=True` lets pp-init proceed when the target already exists with
    content (typical case: we just `git clone`d an empty remote).
    """
    args = [sys.executable, str(PP_INIT_SCRIPT), str(target)]
    if remote:
        args += ["--remote", remote]
    if force:
        args += ["--force"]
    if with_server:
        args += ["--with-server", with_server, "--channels", channels]
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
    """If `target` is a git dir without v2 scaffolding, offer to bootstrap it.

    Used by the wizard right after a `git clone`. The clone may have
    brought down an empty remote, or a v1-or-other repo. v0.4 is a clean
    break -- no migration -- so v1 schemas trigger a force re-init.

    After scaffolding, if `target` has an `origin` remote, offer to push
    the initial commit so future `pp` ops don't trip over an empty remote.
    """
    if _is_scaffolded(target):
        return
    print()
    print(f"  {target} is a git repo but has no v2 pair-pressure scaffolding yet.")
    print(f"  (probably: the remote is empty, or it's an older / unrelated repo).")
    if not yes_no("  Scaffold it now? (registry + .gitignore + initial commit)",
                  default_yes=True):
        die("Aborting -- chat repo is not scaffolded. "
            "Run pp-init on it manually, or rerun pp-setup and pick option 3.")
    # Don't pass --remote: the clone already set origin. pp-init's --remote
    # would try to `git remote add origin` and fail with "already exists".
    _pp_init(target, channels="", remote=None, force=True)

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

def verify(chat_repo, author, server=None):
    """Sanity-check via pp.

    If `server` is given, run `pp list-channels --server <name>`. Otherwise
    run `pp servers` -- a registry-level verb that does not require a
    server context, so the verification still works when the registry is
    empty or has many servers and no default was configured.
    """
    env = os.environ.copy()
    env["PAIR_PRESSURE_REPO"] = str(chat_repo)
    env["PAIR_PRESSURE_AUTHOR"] = author
    pp = shutil.which("pp")
    if not pp:
        return ("skip", "pp not on PATH yet — restart your shell and re-run pp-setup to verify")
    if server:
        env["PAIR_PRESSURE_SERVER"] = server
        cmd = [pp, "list-channels", "--server", server]
        label = f"pp list-channels --server {server}"
    else:
        cmd = [pp, "servers"]
        label = "pp servers"
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        return ("fail", (r.stderr or r.stdout).strip())
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return ("fail", f"{label} did not return JSON")
    if server:
        # pp list-channels returns a list of {name, ...} dicts.
        channels = data if isinstance(data, list) else []
        names = ", ".join(c["name"] for c in channels) or "(no channels yet)"
        return ("ok", f"{len(channels)} channel(s) on '{server}': {names}")
    # pp servers returns {"servers": [...], "active": ...}.
    servers = data.get("servers", []) if isinstance(data, dict) else []
    if not servers:
        return ("ok", "registry reachable; no servers yet -- create one with "
                      "`pp server new <name>`")
    names = ", ".join(s["name"] for s in servers)
    return ("ok", f"{len(servers)} server(s) registered: {names}")


# ---- first-server bootstrap ----

_SERVER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _registry_has_servers(chat_repo: Path) -> bool:
    p = chat_repo / ".pair-pressure" / "servers.json"
    if not p.is_file():
        return False
    try:
        return bool(json.loads(p.read_text(encoding="utf-8-sig")).get("servers"))
    except (OSError, json.JSONDecodeError):
        return False


def maybe_create_first_server(chat_repo: Path, args, author: str):
    """If the chat repo's registry is empty, offer to create a first server.

    Honors --server-name / --channels for non-interactive runs. Returns the
    name of the created server, or None if none was created.
    """
    if not (chat_repo / ".pair-pressure" / "servers.json").is_file():
        return None
    if _registry_has_servers(chat_repo):
        return None

    if args.server_name:
        name = args.server_name
    elif PromptCtx.non_interactive:
        return None
    else:
        print()
        if not yes_no(
            "The chat repo has no servers yet. Create your first server now?",
            default_yes=True,
        ):
            return None
        name = prompt(
            "Server name", default="general",
            validate=lambda s: None if _SERVER_NAME_RE.match(s)
                                    else "must match ^[a-z0-9][a-z0-9._-]{0,63}$",
        )

    channels = args.channels or "general"
    if not PromptCtx.non_interactive and not args.channels:
        channels = prompt(
            "Channels for this server (comma-separated)",
            default="general",
        )

    env = os.environ.copy()
    env["PAIR_PRESSURE_REPO"] = str(chat_repo)
    env["PAIR_PRESSURE_AUTHOR"] = author
    print(f"Creating server '{name}' (channels: {channels})...")
    pp = shutil.which("pp")
    cmd = [pp or "pp", "server", "new", name, "--channels", channels]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  failed: {(r.stderr or r.stdout).strip()}")
        print(f"  You can retry manually: pp server new {name} --channels {channels}")
        return None
    try:
        info = json.loads(r.stdout)
        print(f"  ok: server/{info.get('name', name)} at {info.get('worktree', '?')}")
    except json.JSONDecodeError:
        print(f"  ok (raw): {r.stdout.strip()}")
    return name


# ---- upgrade flow ----

def upgrade_flow(existing_version, install_method, args):
    """Refresh skill + slash commands + verify config; preserve env vars.

    v0.4 wheel-based install means the package itself is upgraded via
    `./install.ps1` (or `uv tool upgrade pair-pressure`), NOT inside
    pp-setup. This flow refreshes the user-visible artifacts that
    don't live in the wheel (skill copy in ~/.claude, slash commands)
    and validates the existing config.
    """
    print(f"\nFound existing pair-pressure {existing_version} "
          f"(installed via: {install_method})")
    if existing_version != __version__:
        print(f"pp-setup bundled with {__version__}; pp on PATH is {existing_version}.")
        print("Run `./install.ps1` (or `uv tool upgrade pair-pressure`) to update "
              "the package first, then re-run pp-setup.")
        if not yes_no("Refresh skill + slash commands anyway?", default_yes=False):
            sys.exit(0)
    elif not yes_no("Refresh skill + slash commands (preserves your env vars)?",
                    default_yes=True):
        print("Cancelled.")
        sys.exit(0)
    print()

    print("[1/4] Refreshing skill at "
          f"{USER_SKILL_PATH} (copy from bundled wheel)...")
    print(f"   skill: {install_skill()}")

    print("[2/4] Refreshing slash commands (preserving any you customized)...")
    actions = install_slash_commands(
        bin_name=args.bin_name,
        force_overwrite=args.overwrite_commands,
    )
    print(f"   new={actions['new']} updated={actions['updated']} "
          f"kept-customized={actions['kept']} unchanged={actions['unchanged']}")

    print("[3/4] Preserving existing settings.local.json env vars...")
    existing = {}
    if SETTINGS_PATH.exists():
        try:
            existing = (json.loads(SETTINGS_PATH.read_text(encoding="utf-8-sig") or "{}")
                        .get("env", {}))
        except json.JSONDecodeError:
            pass
    for k in PP_ENV_KEYS:
        v = existing.get(k)
        mark = "OK" if v else "MISSING"
        print(f"   {k}: {mark}{' = ' + v if v else ''}")

    print("[4/4] Running verification...")
    repo = existing.get("PAIR_PRESSURE_REPO")
    author = existing.get("PAIR_PRESSURE_AUTHOR")
    server = existing.get("PAIR_PRESSURE_SERVER")
    if repo and author:
        status, msg = verify(Path(repo), author, server=server)
        print(f"   {status}: {msg}")
    else:
        print("   skipped (env vars incomplete) -- run `pp-setup` without --yes to fix")

    print(f"\nRefreshed for {__version__}. Restart Claude Code if env vars changed.")


# ---- fresh install flow ----

def fresh_install_flow(args):
    print("pair-pressure setup wizard")
    print()

    # Identity
    default_author = git_default("user.name") or os.environ.get("USER") or os.environ.get("USERNAME")
    default_email  = git_default("user.email")
    author = args.author or prompt(
        "Author username",
        default=default_author,
        validate=lambda s: None if s else "must not be blank",
    )
    email = args.email or prompt(
        "Author email",
        default=default_email or "",
    )

    # AI alias: random default per install. Distinguishes AI-composed posts
    # (signed `<author>/<alias>`) from human verbatim posts (signed `<author>`).
    # --no-alias skips the prompt and writes no PAIR_PRESSURE_ALIAS at all.
    alias = None
    if not args.no_alias:
        alias = args.alias or prompt(
            "AI nickname",
            default=random_alias(),
            validate=lambda s: None if _ALIAS_RE.match(s)
                                    else "must match ^[A-Za-z][A-Za-z0-9_-]{0,31}$",
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

    # First-server bootstrap (if registry is empty)
    first_server = maybe_create_first_server(chat_repo, args, author)
    if first_server and not PromptCtx.non_interactive and not args.no_default_server:
        set_default = args.set_default_server or yes_no(
            f"Set PAIR_PRESSURE_SERVER={first_server} as the default for this shell?",
            default_yes=True,
        )
    else:
        set_default = bool(first_server and args.set_default_server)

    # Settings merge -- write to BOTH settings.local.json and settings.json,
    # plus the user's shell profile, so env vars are picked up regardless
    # of which mechanism the Claude Code build honors.
    env_updates = {
        "PAIR_PRESSURE_REPO": str(chat_repo),
        "PAIR_PRESSURE_AUTHOR": author,
    }
    if alias:
        env_updates["PAIR_PRESSURE_ALIAS"] = alias
    if set_default and first_server:
        env_updates["PAIR_PRESSURE_SERVER"] = first_server
    print(f"\nMerging env vars into:")
    print(f"  {SETTINGS_PATH}")
    print(f"  {SETTINGS_GLOBAL_PATH}")
    print(f"  + PAIR_PRESSURE_REPO   = {chat_repo}")
    print(f"  + PAIR_PRESSURE_AUTHOR = {author}")
    if alias:
        print(f"  + PAIR_PRESSURE_ALIAS  = {alias}")
    if "PAIR_PRESSURE_SERVER" in env_updates:
        print(f"  + PAIR_PRESSURE_SERVER = {env_updates['PAIR_PRESSURE_SERVER']}")
    merge_settings(env_updates)
    merge_permissions(bin_name=args.bin_name or "pp")
    print(f"  + permissions.allow: pp / pp-init / pp-setup / pp-install / pair-pressure-mcp (no-confirm)")
    for path, action in write_shell_profile(env_updates):
        print(f"  shell profile: {action} {path}")

    # Optional: MCP config snippets for non-Claude clients (opt-in).
    if args.mcp_client:
        print("\nMCP client config snippets (non-Claude clients):")
        print("  prerequisite: install the MCP extra so the server can start:")
        print('    pip install "pair-pressure[mcp]"  (or: uv tool install "pair-pressure[mcp]")')
        for client in args.mcp_client:
            snippet, dest = write_mcp_client_config(
                client, chat_repo, author, alias)
            print(f"  {client}: wrote {snippet}")
            print(f"    -> merge into {dest}")
        print("  (see docs/CLIENTS.md for per-client wiring details)")

    # Verify
    resolved_server = env_updates.get("PAIR_PRESSURE_SERVER")
    label = (f"pp list-channels --server {resolved_server}"
             if resolved_server else "pp servers")
    print(f"\nRunning verification ({label})...")
    status, msg = verify(chat_repo, author, server=resolved_server)
    print(f"  {status}: {msg}")

    print()
    print("Done. If you have Claude Code open, restart it so env vars take effect.")


# ---- main ----

def main():
    # `prog` follows argv[0]: when invoked as `pp-install` the help text
    # still shows that name, which keeps muscle-memory working.
    prog = Path(sys.argv[0]).stem if sys.argv and sys.argv[0] else "pp-setup"
    if prog not in ("pp-setup", "pp-install"):
        prog = "pp-setup"
    ap = argparse.ArgumentParser(
        prog=prog,
        description="pair-pressure setup wizard",
    )
    ap.add_argument("--version", action="version", version=f"{prog} {__version__}")
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
    ap.add_argument("--alias",  default=None,
                    help="PAIR_PRESSURE_ALIAS value (AI nickname; defaults to "
                         "a random pick from the bundled pool when not given)")
    ap.add_argument("--no-alias", action="store_true",
                    help="skip the alias prompt entirely (no PAIR_PRESSURE_ALIAS written)")
    ap.add_argument("--email",  default=None, help="git user.email on the chat repo clone")
    ap.add_argument("--repo",   default=None, help="path to chat repo (skips repo prompt)")
    ap.add_argument("--remote", default=None, help="remote URL when initialising a fresh chat repo")
    ap.add_argument("--channels", default="general,planning,brainstorm",
                    help="channels when --create-if-missing creates a fresh chat repo")
    ap.add_argument("--create-if-missing", action="store_true",
                    help="if --repo path doesn't exist, run pp-init on it")

    # Server / first-server options
    ap.add_argument("--server-name", default=None,
                    help="name for the first server (skips prompt if registry is empty)")
    ap.add_argument("--set-default-server", action="store_true",
                    help="write PAIR_PRESSURE_SERVER=<first-server> to env vars")
    ap.add_argument("--no-default-server", action="store_true",
                    help="skip the prompt about PAIR_PRESSURE_SERVER")

    # Yes/no toggles for skill+commands
    ap.add_argument("--skill",    action="store_true", help="install the skill (default: prompt yes)")
    ap.add_argument("--no-skill", action="store_true", help="skip skill install")
    ap.add_argument("--commands",    action="store_true", help="install slash commands (default: prompt yes)")
    ap.add_argument("--no-commands", action="store_true", help="skip slash commands install")
    ap.add_argument("--mcp-client", action="append", default=None,
                    choices=sorted(MCP_CLIENTS),
                    metavar="CLIENT",
                    help="generate an MCP config snippet for a non-Claude "
                         "client (codex|opencode|cline|cursor|kilo); "
                         "repeatable. Opt-in: omitted = Claude Code only.")

    args = ap.parse_args()

    if args.yes:
        PromptCtx.non_interactive = True

    require_git()

    # Upgrade vs fresh
    if not args.reinstall:
        existing = detect_existing_install()
        if existing and existing[0] != __version__:
            upgrade_flow(existing[0], existing[1], args)
            return
        if existing and existing[0] == __version__:
            print(f"pair-pressure {__version__} already installed.")
            if PromptCtx.non_interactive or not yes_no(
                "Update config?", default_yes=True
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
