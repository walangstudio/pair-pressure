#!/usr/bin/env python3
"""pp-setup: interactive onboarding wizard for pair-pressure.

(Also reachable as the legacy name `pp-install`; both console scripts
dispatch here.)

After the bootstrap installer (install.ps1 / install.sh) has placed `pp` on
PATH, this wizard:

  - prompts for author identity (default from `git config user.name`)
  - registers your first chat server (`pp server add <name> <url>` — clone,
    adopt an existing clone, or bootstrap an empty remote)
  - asks which AI CLIs to wire: claude / codex / opencode / cursor / cline /
    kilo. Claude Code gets the skill + /pp-chat:* slash commands + env vars
    in ~/.claude/settings*; every other client gets an MCP config snippet +
    an AGENTS.md snippet.
  - runs `pp status` to verify

Re-running upgrades in place. On a MAJOR version bump the installed skill
and slash commands are overwritten without prompting — stale commands would
call verbs that no longer exist.

Usage:
    pp-setup                              fully interactive
    pp-setup --yes                        use defaults; fail if no default
    pp-setup --author X --server team --remote URL    non-interactive
    pp-setup --reinstall                  skip upgrade detection
"""
from __future__ import annotations

import argparse
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


def _major(version: str) -> int:
    try:
        return int(version.split(".")[0])
    except (ValueError, AttributeError):
        return -1


# `__file__` resolves to one of:
#   - editable install: <repo>/src/pair_pressure/_data/scripts/pp-setup.py
#   - wheel install:    <venv>/Lib/site-packages/pair_pressure/_data/scripts/pp-setup.py
DATA_ROOT = Path(__file__).resolve().parent.parent  # _data/


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
AGENTS_SNIPPET_SOURCE = SKILL_DIR / "templates" / "AGENTS-snippet.md"
CLAUDE_HOME = Path.home() / ".claude"
SETTINGS_PATH = CLAUDE_HOME / "settings.local.json"
SETTINGS_GLOBAL_PATH = CLAUDE_HOME / "settings.json"
USER_SKILL_PATH = CLAUDE_HOME / "skills" / "pair-pressure"
USER_COMMANDS_PATH = CLAUDE_HOME / "commands" / "pp-chat"
PP_HOME = Path.home() / ".pair-pressure"

# Markers for the shell-profile env-var block. Used to find + replace
# idempotently rather than appending duplicates on re-runs. The marker text
# stays `(pp-install)` even after the pp-install → pp-setup rename so existing
# profile blocks written by older versions are still matched and replaced
# rather than duplicated.
PROFILE_BEGIN = "# >>> pair-pressure env vars (pp-install) >>>"
PROFILE_END   = "# <<< pair-pressure env vars <<<"
# v1.0: servers come from the registry; only identity lives in env.
PP_ENV_KEYS = ("PAIR_PRESSURE_AUTHOR", "PAIR_PRESSURE_ALIAS")

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
_SERVER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


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


# ---- settings.local.json merge (Claude Code adapter) ----

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
    values updates in place (and a v0.x block carrying the retired
    PAIR_PRESSURE_REPO/SERVER vars is replaced wholesale).
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


# ---- multi-CLI client wiring ----
#
# pair-pressure's core (pp CLI, MCP server, OS toasts, watcher) is
# client-agnostic. Claude Code is one adapter (skill + slash commands +
# statusline badge); every other client gets the MCP server + an AGENTS.md
# snippet with the agent instructions.

# Each entry: (snippet filename, shape, human-readable canonical destination,
# AGENTS.md destination or None).
# We write a ready-to-use snippet rather than mutating the client's real
# config in place -- the on-disk location of several of these (Cline/Kilo
# live in editor extension storage) varies by OS/editor/version, and a
# wrong-path or malformed write is worse than a copy-paste. CLIENTS.md lists
# the canonical paths.
MCP_CLIENTS = {
    "codex":    ("codex.config.toml", "toml",
                 "~/.codex/config.toml",
                 "~/.codex/AGENTS.md (global) or <project>/AGENTS.md"),
    "opencode": ("opencode.json",     "opencode",
                 "~/.config/opencode/opencode.json",
                 "~/.config/opencode/AGENTS.md (global) or <project>/AGENTS.md"),
    "cursor":   ("cursor.mcp.json",   "mcpservers",
                 "~/.cursor/mcp.json (global) or <project>/.cursor/mcp.json",
                 "<project>/AGENTS.md (or .cursor/rules)"),
    "cline":    ("cline.mcp.json",    "mcpservers",
                 "Cline panel > MCP Servers > Configure (cline_mcp_settings.json)",
                 "<project>/AGENTS.md (or .clinerules)"),
    "kilo":     ("kilo.mcp.json",     "mcpservers",
                 "Kilo Code > MCP settings (mcp_settings.json)",
                 "<project>/AGENTS.md"),
}
ALL_CLIENTS = ("claude",) + tuple(MCP_CLIENTS)


def _mcp_env(author, alias=None):
    env = {"PAIR_PRESSURE_AUTHOR": author}
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


def write_mcp_client_config(client, author, alias=None):
    """Write a ready-to-use MCP config snippet for `client` under
    ~/.pair-pressure/mcp/. Returns (snippet_path, canonical_destination).
    Idempotent: overwrites the snippet each run."""
    filename, shape, dest, _agents_dest = MCP_CLIENTS[client]
    env = _mcp_env(author, alias)
    content = _mcp_snippet(shape, env)
    out_dir = PP_HOME / "mcp"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / filename
    path.write_text(content, encoding="utf-8")
    return path, dest


def write_agents_snippet():
    """Copy the client-neutral AGENTS.md snippet (agent instructions for
    non-Claude CLIs) to ~/.pair-pressure/AGENTS-pair-pressure.md. Returns
    the path, or None when the bundled source is missing."""
    if not AGENTS_SNIPPET_SOURCE.is_file():
        return None
    out = PP_HOME / "AGENTS-pair-pressure.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(AGENTS_SNIPPET_SOURCE.read_text(encoding="utf-8"),
                   encoding="utf-8")
    return out


def claude_code_detected():
    return CLAUDE_HOME.is_dir()


def pick_clients(args):
    """Resolve which clients to wire. --clients wins; else prompt;
    non-interactive default = claude when ~/.claude exists, else none."""
    if args.clients:
        chosen = [c.strip().lower() for c in args.clients.split(",") if c.strip()]
        bad = [c for c in chosen if c not in ALL_CLIENTS]
        if bad:
            die(f"unknown client(s): {', '.join(bad)} "
                f"(valid: {', '.join(ALL_CLIENTS)})")
        return chosen
    if args.mcp_client:
        # Legacy spelling: --mcp-client codex --mcp-client opencode
        chosen = list(args.mcp_client)
        if claude_code_detected():
            chosen.insert(0, "claude")
        return chosen
    default = "claude" if claude_code_detected() else ""
    if PromptCtx.non_interactive:
        return [c for c in default.split(",") if c]
    print("\nWhich AI CLIs should pair-pressure be wired into?")
    print(f"  options: {', '.join(ALL_CLIENTS)}  (comma-separated)")
    if claude_code_detected():
        print("  (Claude Code detected at ~/.claude)")
    raw = prompt("clients", default=default)
    chosen = [c.strip().lower() for c in raw.split(",") if c.strip()]
    bad = [c for c in chosen if c not in ALL_CLIENTS]
    if bad:
        print(f"  ignoring unknown client(s): {', '.join(bad)}")
        chosen = [c for c in chosen if c in ALL_CLIENTS]
    return chosen


# ---- skill install (Claude Code adapter) ----

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


def installed_skill_version() -> str:
    """Version of the skill copy at ~/.claude/skills/pair-pressure, if any."""
    try:
        return (USER_SKILL_PATH / ".pp-version").read_text(
            encoding="utf-8").strip()
    except OSError:
        return ""


def install_skill():
    """Copy the bundled skill tree into ~/.claude/skills/pair-pressure.

    The wheel ships skill data inside the package
    (`pair_pressure._data.skill`), so the source IS the install. We copy
    rather than junction so the user's source clone can be deleted/moved
    without breaking the skill.

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


# ---- slash command install (Claude Code adapter) ----

def install_slash_commands(bin_name="pp", force_overwrite=False):
    """Copy slash command sources into ~/.claude/commands/pp-chat/.

    Stale commands from a previous MAJOR version are removed (they dispatch
    to verbs that no longer exist) and current ones are overwritten without
    prompting. Within the same major, a customized file prompts before
    overwrite (or honors force_overwrite).

    If bin_name != 'pp', also rewrite the file content so `pp ` becomes
    `<bin_name> ` before writing.
    """
    if not COMMAND_SOURCES.is_dir():
        die(f"missing canonical slash command sources at {COMMAND_SOURCES}")
    prev = installed_skill_version()
    major_bump = bool(prev) and _major(prev) != _major(__version__)
    if major_bump:
        force_overwrite = True
    USER_COMMANDS_PATH.mkdir(parents=True, exist_ok=True)
    actions = {"new": 0, "updated": 0, "kept": 0, "unchanged": 0, "removed": 0}
    canonical = {p.name for p in COMMAND_SOURCES.glob("*.md")}
    if major_bump:
        # e.g. peek.md / repo.md from v0.x would call dead verbs.
        for stale in USER_COMMANDS_PATH.glob("*.md"):
            if stale.name not in canonical:
                stale.unlink(missing_ok=True)
                actions["removed"] += 1
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


# ---- server registration ----

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


def _registered_servers():
    """Names in ~/.pair-pressure/servers.json (the machine registry)."""
    p = PP_HOME / "servers.json"
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        return [s.get("name") for s in data.get("servers", []) if s.get("name")]
    except (OSError, json.JSONDecodeError):
        return []


def _pp_server_add(name, url, author, path=None):
    """Run `pp server add` (clone/bootstrap + register). Returns (ok, msg)."""
    pp = shutil.which("pp")
    if not pp:
        return (False, "pp not on PATH yet — restart your shell, then run "
                       f"`pp server add {name} {url}` yourself")
    env = os.environ.copy()
    env["PAIR_PRESSURE_AUTHOR"] = author
    cmd = [pp, "server", "add", name, url]
    if path:
        cmd += ["--path", str(path), "--no-clone"]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        return (False, (r.stderr or r.stdout).strip())
    try:
        info = json.loads(r.stdout)
        suffix = " (default)" if info.get("default") else ""
        return (True, f"registered '{name}' at {info.get('path')}{suffix}")
    except json.JSONDecodeError:
        return (True, r.stdout.strip())


def setup_server(args, author):
    """Register the first chat server. Returns the server name or None.

    Non-interactive: honors --server/--remote/--path. Interactive: offers
    clone-from-remote / adopt-existing-clone / skip. An empty remote is
    bootstrapped by `pp server add` itself (pp-init + push).
    """
    existing = _registered_servers()
    if existing:
        print(f"\nRegistered servers: {', '.join(existing)} (keeping them)")
        if not (args.server and args.remote):
            return None

    name, url, path = args.server, args.remote, args.path
    if not (name and url):
        if PromptCtx.non_interactive:
            return None
        print("\nConnect a chat server (one GitHub repo = one server).")
        print("  1) Clone from a remote URL (bootstraps an empty repo)")
        print("  2) Adopt an existing local clone")
        print("  3) Skip — register later with `pp server add <name> <url>`")
        choice = prompt("choice", default="1", choices=["1", "2", "3"])
        if choice == "3":
            return None
        url = url or prompt("Remote URL (git@... or https://...)")
        derived = repo_name_from_url(url)
        name = name or prompt(
            "Server name", default=derived or "team",
            validate=lambda s: None if _SERVER_NAME_RE.match(s)
                                    else "must match ^[a-z0-9][a-z0-9._-]{0,63}$",
        )
        if choice == "2" and not path:
            path = prompt("Path to the existing clone")

    if not _SERVER_NAME_RE.match(name or ""):
        die("server name must match ^[a-z0-9][a-z0-9._-]{0,63}$")
    print(f"Registering server '{name}' ...")
    ok, msg = _pp_server_add(name, url, author, path=path)
    print(f"  {'ok' if ok else 'failed'}: {msg}")
    return name if ok else None


# ---- verification ----

def verify(author):
    """Sanity-check via `pp status` (never needs a configured server)."""
    pp = shutil.which("pp")
    if not pp:
        return ("skip", "pp not on PATH yet — restart your shell and re-run "
                        "pp-setup to verify")
    env = os.environ.copy()
    env["PAIR_PRESSURE_AUTHOR"] = author
    r = subprocess.run([pp, "status"], env=env, capture_output=True, text=True)
    if r.returncode != 0:
        return ("fail", (r.stderr or r.stdout).strip())
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return ("fail", "pp status did not return JSON")
    verdict = data.get("verdict", "?")
    where = data.get("where") or "(no server)"
    return ("ok" if verdict in ("ready", "needs_restart") else verdict,
            f"verdict={verdict}; where={where}; {data.get('message', '')}")


# ---- upgrade flow ----

def upgrade_flow(existing_version, install_method, args):
    """Refresh skill + slash commands + verify config; preserve env vars.

    The package itself is upgraded via `./install.ps1` (or
    `uv tool upgrade pair-pressure`), NOT inside pp-setup. This flow
    refreshes the user-visible artifacts that don't live in the wheel
    (skill copy in ~/.claude, slash commands) and validates the config.

    On a MAJOR bump the skill + commands are force-overwritten — v0.x slash
    commands dispatch to verbs that no longer exist in v1.0.
    """
    print(f"\nFound existing pair-pressure {existing_version} "
          f"(installed via: {install_method})")
    major_bump = _major(existing_version) != _major(__version__)
    if existing_version != __version__:
        print(f"pp-setup bundled with {__version__}; pp on PATH is {existing_version}.")
        print("Run `./install.ps1` (or `uv tool upgrade pair-pressure`) to update "
              "the package first, then re-run pp-setup.")
        if major_bump:
            print("MAJOR version change: the installed skill + slash commands "
                  "will be replaced (old ones call removed verbs).")
            if _major(existing_version) < 1 <= _major(__version__):
                print("v1.0 is a clean break: v2 chat repos are not migrated. "
                      "Re-init each repo with `pp-init --force`, then "
                      "`pp server add <name> <url>`.")
        if not yes_no("Refresh skill + slash commands anyway?",
                      default_yes=major_bump):
            sys.exit(0)
    elif not yes_no("Refresh skill + slash commands (preserves your env vars)?",
                    default_yes=True):
        print("Cancelled.")
        sys.exit(0)
    print()

    print("[1/4] Refreshing skill at "
          f"{USER_SKILL_PATH} (copy from bundled wheel)...")
    print(f"   skill: {install_skill()}")

    print("[2/4] Refreshing slash commands...")
    actions = install_slash_commands(
        bin_name=args.bin_name,
        force_overwrite=args.overwrite_commands or major_bump,
    )
    print(f"   new={actions['new']} updated={actions['updated']} "
          f"kept-customized={actions['kept']} unchanged={actions['unchanged']} "
          f"removed-stale={actions['removed']}")

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

    print("[4/4] Running verification (pp status)...")
    author = existing.get("PAIR_PRESSURE_AUTHOR")
    if author:
        status, msg = verify(author)
        print(f"   {status}: {msg}")
    else:
        print("   skipped (PAIR_PRESSURE_AUTHOR unset) -- run `pp-setup` "
              "without --yes to fix")

    print(f"\nRefreshed for {__version__}. Restart your CLI if env vars changed.")


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

    # Identity env: written to the shell profile for every CLI; Claude Code
    # additionally gets the settings.json merge below.
    env_updates = {"PAIR_PRESSURE_AUTHOR": author}
    if alias:
        env_updates["PAIR_PRESSURE_ALIAS"] = alias
    os.environ.update(env_updates)

    # First server (registry-based; `pp server add` clones + bootstraps).
    server = setup_server(args, author)
    if server and email:
        # Give the fresh clone the user's git identity.
        reg = PP_HOME / "servers.json"
        try:
            data = json.loads(reg.read_text(encoding="utf-8-sig"))
            entry = next((s for s in data.get("servers", [])
                          if s.get("name") == server), None)
            if entry and entry.get("path"):
                run("git", "-C", entry["path"], "config", "user.email", email,
                    check=False)
                run("git", "-C", entry["path"], "config", "user.name", author,
                    check=False)
        except (OSError, json.JSONDecodeError):
            pass

    # Clients
    clients = pick_clients(args)
    wire_claude = "claude" in clients
    mcp_clients = [c for c in clients if c in MCP_CLIENTS]

    if wire_claude:
        do_skill = not args.no_skill and (
            args.skill or PromptCtx.non_interactive or yes_no(
                f"\nInstall the Claude Code skill at {USER_SKILL_PATH}?",
                default_yes=True,
            )
        )
        if do_skill:
            print(f"  skill: {install_skill()}")
        do_commands = not args.no_commands and (
            args.commands or PromptCtx.non_interactive or yes_no(
                f"Install /pp-chat:* slash commands at {USER_COMMANDS_PATH}?",
                default_yes=True,
            )
        )
        if do_commands:
            actions = install_slash_commands(
                bin_name=args.bin_name,
                force_overwrite=args.overwrite_commands)
            print(f"  slash commands: new={actions['new']} "
                  f"updated={actions['updated']} kept-customized={actions['kept']} "
                  f"unchanged={actions['unchanged']} removed-stale={actions['removed']}")

        print(f"\nMerging env vars into:")
        print(f"  {SETTINGS_PATH}")
        print(f"  {SETTINGS_GLOBAL_PATH}")
        for k, v in env_updates.items():
            print(f"  + {k} = {v}")
        merge_settings(env_updates)
        merge_permissions(bin_name=args.bin_name or "pp")
        print(f"  + permissions.allow: pp / pp-init / pp-setup / pp-install / pair-pressure-mcp (no-confirm)")

    for path, action in write_shell_profile(env_updates):
        print(f"  shell profile: {action} {path}")

    # MCP + AGENTS.md snippets for non-Claude clients.
    if mcp_clients:
        agents = write_agents_snippet()
        print("\nNon-Claude clients (MCP + AGENTS.md):")
        print("  prerequisite: install the MCP extra so the server can start:")
        print('    pip install "pair-pressure[mcp]"  (or: uv tool install "pair-pressure[mcp]")')
        for client in mcp_clients:
            snippet, dest = write_mcp_client_config(client, author, alias)
            print(f"  {client}: wrote {snippet}")
            print(f"    -> merge into {dest}")
            if agents:
                print(f"    -> agent instructions: append {agents}")
                print(f"       to {MCP_CLIENTS[client][3]}")
        print("  (see docs/CLIENTS.md for per-client wiring details)")

    # Verify
    print(f"\nRunning verification (pp status)...")
    status, msg = verify(author)
    print(f"  {status}: {msg}")

    print()
    print("Done. Restart your CLI session so the env vars take effect, then:")
    print("  pp where        # see where you are")
    print("  pp send         # post to #general")
    if not server:
        print("  pp server add <name> <url>   # connect your first server")


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
    ap.add_argument("--email",  default=None, help="git user.email on the server clone")

    # Server registration
    ap.add_argument("--server", default=None,
                    help="name for the first server (with --remote)")
    ap.add_argument("--remote", default=None,
                    help="remote URL of the first server's GitHub repo")
    ap.add_argument("--path", default=None,
                    help="adopt an existing local clone at this path")

    # Client wiring
    ap.add_argument("--clients", default=None,
                    help="comma-separated CLIs to wire: "
                         f"{','.join(ALL_CLIENTS)} (default: prompt; "
                         "claude when ~/.claude exists)")
    ap.add_argument("--mcp-client", action="append", default=None,
                    choices=sorted(MCP_CLIENTS),
                    metavar="CLIENT",
                    help="legacy spelling for --clients (repeatable; "
                         "implies claude too when detected)")

    # Yes/no toggles for skill+commands (Claude adapter)
    ap.add_argument("--skill",    action="store_true", help="install the skill (default: prompt yes)")
    ap.add_argument("--no-skill", action="store_true", help="skip skill install")
    ap.add_argument("--commands",    action="store_true", help="install slash commands (default: prompt yes)")
    ap.add_argument("--no-commands", action="store_true", help="skip slash commands install")

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
