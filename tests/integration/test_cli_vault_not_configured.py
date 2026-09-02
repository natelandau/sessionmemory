"""Tests for the shared guard every command uses to locate the vault.

`config.vault_root` raises when `SESSIONMEMORY_VAULT` is unset or names a missing
directory. Every command resolves the vault, so an unhandled raise here would surface
as a raw traceback the first time a new user forgets to export the variable.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from sessionmemory.cli import app

runner = CliRunner()

COMMANDS = [
    ["project"],
    ["project", "--register"],
]


@pytest.mark.parametrize("command", COMMANDS, ids=lambda c: c[0])
def test_unset_vault_reports_a_clean_error(command, monkeypatch):
    """Report the missing variable instead of a traceback when it is unset."""
    monkeypatch.delenv("SESSIONMEMORY_VAULT", raising=False)
    result = runner.invoke(app, command)
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "SESSIONMEMORY_VAULT" in result.output


@pytest.mark.parametrize("command", COMMANDS, ids=lambda c: c[0])
def test_missing_vault_directory_reports_a_clean_error(command, tmp_path, monkeypatch):
    """Report the missing directory instead of a traceback when it does not exist."""
    monkeypatch.setenv("SESSIONMEMORY_VAULT", str(tmp_path / "nope"))
    result = runner.invoke(app, command)
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "does not exist" in result.output


@pytest.mark.parametrize("command", COMMANDS, ids=lambda c: c[0])
def test_uninitialized_vault_directory_reports_a_clean_error(command, tmp_path, monkeypatch):
    """Report the missing marker instead of scattering notes into an unmarked directory."""
    monkeypatch.setenv("SESSIONMEMORY_VAULT", str(tmp_path))
    result = runner.invoke(app, command)
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "run: sessionmemory init" in result.output
