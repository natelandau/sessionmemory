"""Tests for the doctor command."""

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


def test_doctor_json_on_clean_vault_returns_empty_list(workspace):
    """Verify sessionmemory doctor --json on a fresh workspace returns [] with exit 0."""
    result = runner.invoke(app, ["doctor", "--json"])

    assert result.exit_code == 0, result.output
    output = json.loads(result.stdout)
    assert output == []


def test_doctor_prose_on_clean_vault_reports_nothing(workspace):
    """Verify sessionmemory doctor's prose output says nothing to report on a fresh workspace."""
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "nothing to report" in result.output


def test_doctor_prose_reports_a_bad_filename(workspace):
    """Verify a finding's prose names the check, path, and message."""
    vault, _ = workspace

    directory = vault / "projects" / "demo" / "learnings"
    directory.mkdir(parents=True)
    (directory / "Bad Name.md").write_text("x")

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "1 suggestion" in result.output
    assert "filename" in result.output
    assert str(directory / "Bad Name.md") in result.output


def test_doctor_json_reports_a_bad_filename(workspace):
    """Verify a page with a bad name appears in sessionmemory doctor --json output."""
    vault, _ = workspace

    directory = vault / "projects" / "demo" / "learnings"
    directory.mkdir(parents=True)
    (directory / "Bad Name.md").write_text("x")

    result = runner.invoke(app, ["doctor", "--json"])

    assert result.exit_code == 0, result.output
    output = json.loads(result.stdout)
    assert len(output) == 1
    assert output[0]["check"] == "filename"
    assert str(directory / "Bad Name.md") in output[0]["path"]
