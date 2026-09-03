"""Tests for the inject command."""

from __future__ import annotations

import json
import subprocess

import pytest
from typer.testing import CliRunner

from sessionmemory.cli import app

runner = CliRunner()


def _git(*args, cwd) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def workspace(vault, tmp_path, monkeypatch):
    """Build a vault plus a registered repository, and chdir into the repository."""
    repo = tmp_path / "demo"
    repo.mkdir()
    _git("init", "-q", ".", cwd=repo)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
    _git("remote", "add", "origin", "git@example.org:nate/demo.git", cwd=repo)
    assert runner.invoke(app, ["project", "--register", "--cwd", str(repo)]).exit_code == 0
    monkeypatch.chdir(repo)
    return vault, repo


def test_inject_lists_titles_and_exits_zero(workspace):
    """Verify inject prints the guidance and every learning's title."""
    runner.invoke(app, ["new", "learning", "--title", "A learning", "--summary", "s"])

    result = runner.invoke(app, ["inject", "--command", "/x/sessionmemory"])

    assert result.exit_code == 0
    assert "## Using this vault" in result.stdout
    assert "  - A learning" in result.stdout
    assert "/x/sessionmemory search" in result.stdout


def test_inject_json_names_the_same_fields_as_the_prose(workspace):
    """Verify --json carries the guidance, titles, and open work the prose renders."""
    runner.invoke(app, ["new", "learning", "--title", "A learning", "--summary", "s"])

    result = runner.invoke(app, ["inject", "--command", "/x/sessionmemory", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["project"] == "demo"
    assert payload["titles"] == ["A learning"]
    assert "/x/sessionmemory search" in payload["guidance"]


def test_inject_tolerates_a_page_with_invalid_utf8(workspace):
    """Verify inject exits 0 rather than crashing on a page saved with invalid UTF-8."""
    vault, _ = workspace
    learnings = vault / "projects" / "demo" / "learnings"
    learnings.mkdir(parents=True, exist_ok=True)
    (learnings / "invalid.md").write_bytes(b"---\ntitle: t\nsummary: s\n---\n\xff\xfe\n")

    result = runner.invoke(app, ["inject"])

    assert result.exit_code == 0, result.output


def test_inject_unregistered_fails(vault, tmp_path):
    """Verify an unregistered directory is refused, not injected empty."""
    result = runner.invoke(app, ["inject", "--cwd", str(tmp_path)])

    assert result.exit_code == 1


def test_inject_names_the_cli_by_its_own_command_by_default(workspace):
    """Verify the guidance spells the CLI as `sessionmemory` when no --command is given."""
    result = runner.invoke(app, ["inject"])

    assert result.exit_code == 0
    assert "`sessionmemory search" in result.stdout
    assert "`vault search" not in result.stdout
