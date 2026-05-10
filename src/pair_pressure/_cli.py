"""Entry point for the `pp` console script."""
from __future__ import annotations

from runpy import run_path

from ._paths import pp_script


def main() -> None:
    run_path(str(pp_script()), run_name="__main__")
