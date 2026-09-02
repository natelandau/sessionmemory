"""Tests for the reindex command."""

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


def test_reindex_reports_both_fields(workspace):
    """Verify reindex refreshes learnings and logs and reports counts for each."""
    runner.invoke(app, ["new", "learning", "--title", "A", "--summary", "s"])

    result = runner.invoke(app, ["reindex", "--json"])

    payload = json.loads(result.stdout)
    assert payload["learnings"]["added"] == 1
    assert payload["logs"] == {"added": 0, "updated": 0, "removed": 0, "unchanged": 0}


def test_reindex_writes_the_index_beside_the_pages(workspace):
    """Verify the index file sits inside the field, named for the model."""
    vault, _ = workspace
    runner.invoke(app, ["new", "learning", "--title", "A", "--summary", "s"])

    runner.invoke(app, ["reindex"])

    assert (vault / "projects" / "demo" / "learnings" / "stub.sqlite3").is_file()
