"""pair-pressure: shared chat among AI agents and humans, backed by a git repo."""
from importlib.resources import files


def _read_version() -> str:
    # Single source of truth: _data/skill/VERSION (same file pyproject.toml,
    # pp.py and pp-setup.py read). Avoids version drift across the package.
    try:
        return (files("pair_pressure") / "_data" / "skill" / "VERSION").read_text(
            encoding="utf-8"
        ).strip()
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return "0.0.0+unknown"


__version__ = _read_version()
