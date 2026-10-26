"""Tests for the reconcile command."""

from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
import uuid

import pytest
from typer.testing import CliRunner

from sessionmemory.cli import app
from sessionmemory.lib import commits, field, paths, reconcile, registry

runner = CliRunner()
SLUG = "demo"
ORPHAN = "abc1234"


def _git(*args, cwd) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return done.stdout.strip()


@pytest.fixture
def workspace(vault, tmp_path, monkeypatch):
    """Build a registered repository with a bare origin and a log naming an orphan commit."""
    # The slug comes from the origin URL's last segment.
    origin = tmp_path / "remotes" / f"{SLUG}.git"
    origin.parent.mkdir()
    _git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    repo = tmp_path / SLUG
    repo.mkdir()
    _git("init", "-q", "-b", "main", ".", cwd=repo)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
    _git("remote", "add", "origin", str(origin), cwd=repo)
    _git("push", "-q", "-u", "origin", "main", cwd=repo)
    assert runner.invoke(app, ["project", "--register", "--cwd", str(repo)]).exit_code == 0

    log = paths.logs_dir(vault, SLUG) / "session.md"
    log.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "title": "session",
        "uuid": str(uuid.uuid5(uuid.NAMESPACE_URL, "session")),
        "summary": "s",
        "created": "2026-01-01T00:00:00Z",
        "updated": "2026-01-01T00:00:00Z",
    }
    field.write_page(log, meta, f"## Summary\n\nDid work.\n\n## Changes\n\n{ORPHAN} lost work\n")
    monkeypatch.chdir(repo)
    return vault, repo, log


def test_reconcile_json_reports_and_drops_an_orphan(workspace):
    """Verify --json reports one object per reconciler and the orphan is dropped."""
    _, _, log = workspace

    result = runner.invoke(app, ["reconcile", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [
        {
            "reconciler": "log-commits",
            "changed": [str(log)],
            "replaced": 0,
            "dropped": 1,
            "in_progress": 0,
            "unchecked": 0,
            "notes": [],
        }
    ]
    assert ORPHAN not in log.read_text(encoding="utf-8")


def test_reconcile_dry_run_leaves_the_log(workspace):
    """Verify --dry-run reports the change without writing it."""
    _, _, log = workspace
    before = log.read_bytes()

    result = runner.invoke(app, ["reconcile", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "dry run" in result.output
    assert "1 dropped" in result.output
    assert log.read_bytes() == before


def test_reconcile_prose_without_dry_run_has_no_prefix(workspace):
    """Verify a real run reports counts and omits the dry run marker."""
    result = runner.invoke(app, ["reconcile"])

    assert result.exit_code == 0, result.output
    assert "log-commits: 1 log changed, 0 replaced, 1 dropped" in result.output
    assert "dry run" not in result.output


def test_reconcile_fails_when_root_is_not_a_repository(workspace, tmp_path):
    """Verify a registered root that is a plain directory fails cleanly."""
    _, repo, _ = workspace
    plain = tmp_path / "plain"
    plain.mkdir()
    _git("remote", "remove", "origin", cwd=repo)
    registered = runner.invoke(app, ["project", "--register", "--cwd", str(plain)])
    assert registered.exit_code == 0, registered.output

    result = runner.invoke(app, ["reconcile", "--cwd", str(plain)])

    assert result.exit_code == 1
    assert "not a git repository" in result.output


def test_reconcile_fails_when_git_cannot_answer(workspace, monkeypatch):
    """Verify a CommitsError becomes a clean failure."""
    error = commits.CommitsError("git fetch", "boom")

    def boom(_context: reconcile.Context, *, dry_run: bool) -> list[reconcile.Report]:
        raise error

    monkeypatch.setattr(reconcile, "run", boom)

    result = runner.invoke(app, ["reconcile"])

    assert result.exit_code == 1
    assert "git could not answer for this project" in result.output


def test_reconcile_fails_when_root_no_longer_exists(workspace, tmp_path):
    """Verify a registered root that was deleted fails cleanly."""
    _, repo, _ = workspace
    # A second clone shares the origin, so the project still resolves from it.
    clone = tmp_path / "second-clone"
    origin = _git("remote", "get-url", "origin", cwd=repo)
    _git("clone", "-q", origin, str(clone), cwd=tmp_path)
    shutil.rmtree(repo)

    result = runner.invoke(app, ["reconcile", "--cwd", str(clone)])

    assert result.exit_code == 1, result.output
    assert "project root is not a git repository" in result.output


@pytest.mark.parametrize("root", ["", "relative/path"])
def test_reconcile_fails_when_registered_root_is_empty_or_relative(workspace, root):
    """Verify a root that cannot name a repository fails before git is asked."""
    vault, _, log = workspace
    projects = registry.load(vault)
    projects[SLUG] = dataclasses.replace(projects[SLUG], root=root)
    with registry.locked(vault):
        registry.save(vault, projects)
    before = log.read_bytes()

    result = runner.invoke(app, ["reconcile"])

    assert result.exit_code == 1, result.output
    assert "project has no registered root" in result.output
    assert log.read_bytes() == before


def test_reconcile_fails_when_root_is_a_subdirectory_of_another_repository(workspace):
    """Verify a root inside another repository's tree is not taken for that repository."""
    vault, repo, log = workspace
    sub = repo / "sub"
    sub.mkdir()
    projects = registry.load(vault)
    projects[SLUG] = dataclasses.replace(projects[SLUG], root=str(sub))
    with registry.locked(vault):
        registry.save(vault, projects)
    before = log.read_bytes()

    result = runner.invoke(app, ["reconcile"])

    assert result.exit_code == 1, result.output
    assert "project root is not a git repository" in result.output
    assert log.read_bytes() == before
