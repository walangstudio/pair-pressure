"""Entry point for the `pp-setup` console script (also exposed as the
legacy `pp-install` name for backward compatibility)."""
from __future__ import annotations

from runpy import run_path

from ._paths import pp_setup_script


def main() -> None:
    run_path(str(pp_setup_script()), run_name="__main__")
