"""Tests for the path command."""

from __future__ import annotations

import shutil
import subprocess

import pytest
from typer.testing import CliRunner

from sessionmemory.cli import app
from sessionmemory.commands.path import PathKind
from sessionmemory.lib import paths

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


@pytest.mark.parametrize(
    ("kind", "tail"),
    [
        ("project", "projects/demo"),
        ("learnings", "projects/demo/learnings"),
        ("logs", "projects/demo/logs"),
        ("specs", "projects/demo/specs"),
        ("plans", "projects/demo/plans"),
        ("runbooks", "projects/demo/runbooks"),
        ("reference", "projects/demo/reference"),
        ("backlog", "projects/demo/backlog.md"),
        ("local", "projects/demo/local"),
    ],
)
def test_path_prints_one_absolute_path(workspace, kind, tail):
    """Verify path prints exactly one absolute path for each kind."""
    vault, _ = workspace

    result = runner.invoke(app, ["path", kind])

    assert result.exit_code == 0
    assert result.stdout == f"{vault / tail}\n"


@pytest.mark.parametrize(
    ("kind", "tail"),
    [
        ("runbooks", "projects/demo/runbooks"),
        ("backlog", "projects/demo/backlog.md"),
        ("local", "projects/demo/local"),
    ],
)
def test_path_prints_a_path_that_does_not_exist_yet(workspace, kind, tail):
    """Verify a missing folder or file is still printed."""
    vault, _ = workspace
    target = vault / tail
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink(missing_ok=True)
    assert not target.exists()

    result = runner.invoke(app, ["path", kind])

    assert result.exit_code == 0
    assert result.stdout == f"{target}\n"
    assert not target.exists()


def test_path_kinds_match_the_project_paths_keys(workspace):
    """Verify every kind maps to a `project_paths` key and every key has a kind."""
    vault, _ = workspace
    keys = set(paths.project_paths(vault, "demo"))

    renamed = {"project": "project_dir"}
    kinds = {renamed.get(kind.value, kind.value) for kind in PathKind}

    assert kinds == keys


def test_path_fails_outside_a_registered_project(workspace, tmp_path):
    """Verify an unregistered directory exits 1 with nothing on stdout."""
    unregistered = tmp_path / "elsewhere"
    unregistered.mkdir()

    result = runner.invoke(app, ["path", "specs", "--cwd", str(unregistered)])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "project --register" in result.stderr


def test_path_rejects_an_unknown_kind(workspace):
    """Verify a kind outside the fixed set is a usage error."""
    assert runner.invoke(app, ["path", "notes"]).exit_code == 2
