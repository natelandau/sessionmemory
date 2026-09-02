"""Tests for the console-script entry point.

Every other integration test invokes the Typer `app` directly through `CliRunner`, which
never runs `cli.main`. That leaves the entry point itself, `def main(): app()`, unmeasured
even though `bin/sessionmemory` and the installed console script both call nothing else.
"""

from __future__ import annotations

import sys

import pytest

from sessionmemory.cli import main


def test_main_runs_the_typer_app(monkeypatch, capsys):
    """Verify main() invokes the Typer app with the process's own argv."""
    monkeypatch.setattr(sys, "argv", ["vault", "--help"])

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 0
    assert "Usage:" in capsys.readouterr().out
