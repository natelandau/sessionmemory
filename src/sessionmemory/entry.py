"""The console script, which answers a bare `--version` before building the CLI.

The plugin asks for the version on every session start to pick which CLI to run, and
importing every command module to answer it costs most of the process's startup.
"""

from __future__ import annotations

import importlib.metadata
import sys


def main() -> None:
    """Run the CLI, printing the installed version bare when that is all that was asked."""
    if sys.argv[1:] == ["--version"]:
        sys.stdout.write(f"{importlib.metadata.version('sessionmemory')}\n")
        return

    from sessionmemory.cli import main as run_cli

    run_cli()
