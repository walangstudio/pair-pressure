"""Entry point for the `pair-pressure-mcp` console script."""
from __future__ import annotations

from runpy import run_path

from ._paths import mcp_server_script


def main() -> None:
    run_path(str(mcp_server_script()), run_name="__main__")


if __name__ == "__main__":
    main()
