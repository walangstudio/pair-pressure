"""Locate the bundled scripts that the console-script entry points execute.

The scripts live inside the package at `pair_pressure/_data/`. Resolving via
`importlib.resources.files()` returns the real filesystem path for both
editable installs (where the package is in the source tree) and wheel
installs (where it's in site-packages). The wheel ships everything under
`_data/` via the `package-data` config in pyproject.toml.
"""
from __future__ import annotations

from importlib.resources import files
from pathlib import Path


def _resource(*parts: str) -> Path:
    traversable = files("pair_pressure")
    for part in parts:
        traversable = traversable / part
    return Path(str(traversable))


def pp_script() -> Path:
    return _resource("_data", "skill", "scripts", "pp.py")


def pp_init_script() -> Path:
    return _resource("_data", "scripts", "pp-init.py")


def pp_install_script() -> Path:
    return _resource("_data", "scripts", "pp-install.py")


def mcp_server_script() -> Path:
    return _resource("_data", "skill", "mcp", "server.py")


def skill_root() -> Path:
    return _resource("_data", "skill")
