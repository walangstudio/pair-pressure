"""Locate the source-of-truth scripts so console-script entry points can run them.

The scripts live where the Claude skill expects them. This module finds the
repo root from the installed package location (works with `pip install -e .`)
or from PAIR_PRESSURE_HOME if set (works with a copied tree).
"""
from __future__ import annotations

import os
from pathlib import Path


def repo_root() -> Path:
    env = os.environ.get("PAIR_PRESSURE_HOME")
    if env:
        return Path(env).expanduser().resolve()
    # src/pair_pressure/_paths.py → src/pair_pressure → src → repo root
    return Path(__file__).resolve().parents[2]


def pp_script() -> Path:
    return repo_root() / ".claude" / "skills" / "pair-pressure" / "scripts" / "pp.py"


def pp_init_script() -> Path:
    return repo_root() / "scripts" / "pp-init.py"


def pp_install_script() -> Path:
    return repo_root() / "scripts" / "pp-install.py"


def mcp_server_script() -> Path:
    return repo_root() / "mcp" / "server.py"
