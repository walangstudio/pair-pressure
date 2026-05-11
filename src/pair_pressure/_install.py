"""Entry point for the `pp-install` console script."""
from __future__ import annotations

from runpy import run_path

from ._paths import pp_install_script


def main() -> None:
    run_path(str(pp_install_script()), run_name="__main__")
