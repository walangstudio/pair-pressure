"""CLI adapter seam.

One adapter per target AI CLI. Each knows where that CLI looks for skills,
slash commands, and env-var config. v0.3 ships exactly one adapter
(ClaudeCodeAdapter). The seam exists so v0.4+ can drop in OpencodeAdapter,
CodexAdapter, ClaudeDesktopAdapter without touching the wizard's prompt
logic.

The wizard (`scripts/pp-install.py`) currently calls into the Claude-Code-
specific install helpers directly. The plan is to migrate that to walk
`ADAPTERS`, calling `.detect()` on each, then `.install_skill()` etc. on the
ones that apply to the current machine. That migration is intentionally
deferred until a second adapter exists -- premature abstraction otherwise.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional


class CliAdapter:
    """Base class. Concrete adapters override every method."""
    name: str = ""

    def detect(self) -> bool:
        """Return True iff this CLI is installed/configured on the current box."""
        raise NotImplementedError

    def install_skill(self, repo_root: Path) -> str:
        """Place the pair-pressure skill where this CLI will find it.
        Returns one of 'installed' | 'already-installed' | 'exists-different-target'.
        """
        raise NotImplementedError

    def install_commands(self, repo_root: Path, bin_name: str = "pp") -> dict:
        """Copy slash command sources into this CLI's commands dir.
        Returns a dict of action counts: {new, updated, kept, unchanged}.
        """
        raise NotImplementedError

    def settings_path(self) -> Path:
        """Path to the JSON file where this CLI reads its env-var config."""
        raise NotImplementedError


class ClaudeCodeAdapter(CliAdapter):
    """Adapter for Claude Code CLI (claude.exe on Win, claude on POSIX).

    Skills:   ~/.claude/skills/<name>
    Commands: ~/.claude/commands/<namespace>/<verb>.md
    Settings: ~/.claude/settings.local.json   (top-level `env` key)
    """
    name = "claude-code"

    def detect(self) -> bool:
        return (Path.home() / ".claude").exists()

    def install_skill(self, repo_root: Path) -> str:
        src = repo_root / ".claude" / "skills" / "pair-pressure"
        dst = Path.home() / ".claude" / "skills" / "pair-pressure"
        if dst.exists() or dst.is_symlink():
            try:
                if dst.resolve(strict=False) == src.resolve():
                    return "already-installed"
            except OSError:
                pass
            return "exists-different-target"
        dst.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            r = subprocess.run(
                ["cmd", "/c", "mklink", "/j", str(dst), str(src)],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                raise RuntimeError(
                    f"mklink /j failed: {r.stderr.strip() or r.stdout.strip()}"
                )
        else:
            dst.symlink_to(src)
        return "installed"

    def install_commands(self, repo_root: Path, bin_name: str = "pp") -> dict:
        # Defer to pp-setup.py's implementation; this method exists so a
        # future adapter can supply its own copy logic if its commands dir
        # has different conventions.
        from importlib import util
        # Tolerate the old `pp-install.py` name for source trees that
        # haven't pulled the rename yet.
        for candidate in ("pp-setup.py", "pp-install.py"):
            script = repo_root / "scripts" / candidate
            if script.exists():
                break
        else:
            raise RuntimeError("could not find pp-setup.py or pp-install.py")
        spec = util.spec_from_file_location("_pp_setup_mod", script)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"could not load {script}")
        mod = util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod.install_slash_commands(bin_name=bin_name)

    def settings_path(self) -> Path:
        return Path.home() / ".claude" / "settings.local.json"


# v0.3 ships exactly one. Future adapters slot in here.
ADAPTERS: list[CliAdapter] = [ClaudeCodeAdapter()]


def active_adapters() -> list[CliAdapter]:
    """Return adapters whose target CLI is detected on this machine."""
    return [a for a in ADAPTERS if a.detect()]
