"""Tests for the log command."""

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


def test_log_creates_then_updates(workspace):
    """Verify two calls with one session id produce one file with the second body."""
    vault, _ = workspace
    first = runner.invoke(
        app, ["log", "--session-id", "s1", "--title", "Work", "--body", "one", "--json"]
    )
    second = runner.invoke(
        app, ["log", "--session-id", "s1", "--title", "Work", "--body", "two", "--json"]
    )

    assert json.loads(first.stdout)["action"] == "created"
    assert json.loads(second.stdout)["action"] == "updated"
    logs = list((vault / "projects" / "demo" / "logs").glob("*.md"))
    assert len(logs) == 1
    assert logs[0].read_text(encoding="utf-8").endswith("two\n")


def test_log_with_unusable_title_fails(workspace):
    """Verify a title that yields no filename is refused."""
    result = runner.invoke(app, ["log", "--session-id", "s1", "--title", "!!!"])

    assert result.exit_code == 1
