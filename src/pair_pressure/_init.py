"""Entry point for the `pp-init` console script."""
from __future__ import annotations

from runpy import run_path

from ._paths import pp_init_script


def main() -> None:
    run_path(str(pp_init_script()), run_name="__main__")
