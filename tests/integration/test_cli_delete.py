"""Tests for the delete command."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

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


def test_delete_removes_page_and_its_index_row(workspace):
    """Verify a learning is gone from disk and from the next search."""
    created = json.loads(
        runner.invoke(
            app, ["new", "learning", "--title", "Doomed", "--summary", "s", "--json"]
        ).stdout
    )
    runner.invoke(app, ["reindex"])

    result = runner.invoke(app, ["delete", created["path"], "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == [{"path": created["path"], "deleted": True}]
    assert not Path(created["path"]).exists()
    assert json.loads(runner.invoke(app, ["search", "doomed", "--json"]).stdout) == []


def test_delete_prose_reports_each_deleted_path(workspace):
    """Verify prose output names each successfully deleted path."""
    created = json.loads(
        runner.invoke(
            app, ["new", "learning", "--title", "Doomed Too", "--summary", "s", "--json"]
        ).stdout
    )

    result = runner.invoke(app, ["delete", created["path"]])

    assert result.exit_code == 0, result.output
    assert f"deleted {created['path']}" in result.output


def test_delete_prose_reports_a_missing_path(workspace):
    """Verify prose output names a path that was not found, and exits non-zero."""
    vault, _ = workspace
    missing = str(vault / "projects" / "demo" / "learnings" / "absent.md")

    result = runner.invoke(app, ["delete", missing])

    assert result.exit_code == 1
    assert f"not found: {missing}" in result.output


def test_delete_a_page_outside_any_field_directory_skips_the_index(workspace):
    """Verify deleting a file that is not in a field directory never touches the index.

    A backlog file, for example, has no index row to forget; only `paths.FIELD_DIRS`
    directories carry one.
    """
    vault, _ = workspace
    stray = vault / "projects" / "demo" / "backlog.md"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("# Backlog\n", encoding="utf-8")

    result = runner.invoke(app, ["delete", str(stray), "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == [{"path": str(stray), "deleted": True}]
    assert not stray.exists()


def test_delete_refuses_a_path_outside_the_vault(workspace, tmp_path):
    """Verify delete never unlinks outside the vault."""
    outside = tmp_path / "elsewhere.md"
    outside.write_text("x")

    result = runner.invoke(app, ["delete", str(outside)])

    assert result.exit_code == 1
    assert outside.exists()


def test_delete_missing_path_exits_one_after_deleting_the_rest(workspace):
    """Verify one bad path does not stop the good ones."""
    vault, _ = workspace
    created = json.loads(
        runner.invoke(app, ["new", "learning", "--title", "A", "--summary", "s", "--json"]).stdout
    )

    result = runner.invoke(
        app,
        [
            "delete",
            created["path"],
            str(vault / "projects" / "demo" / "learnings" / "absent.md"),
            "--json",
        ],
    )

    assert result.exit_code == 1
    assert not Path(created["path"]).exists()
    assert json.loads(result.stdout)[1] == {
        "path": str(vault / "projects" / "demo" / "learnings" / "absent.md"),
        "deleted": False,
    }


def test_delete_a_symlink_removes_the_link_not_its_target(workspace):
    """Verify deleting a symlink unlinks the link and leaves the real page in place."""
    created = json.loads(
        runner.invoke(
            app, ["new", "learning", "--title", "Linked", "--summary", "s", "--json"]
        ).stdout
    )
    page = Path(created["path"])
    link = page.parent / "link.md"
    link.symlink_to(page)

    result = runner.invoke(app, ["delete", str(link), "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == [{"path": str(link), "deleted": True}]
    assert not link.exists()
    assert not link.is_symlink()
    assert page.exists()


def test_delete_batch_with_an_outside_path_deletes_nothing(workspace, tmp_path):
    """Verify delete refuses a batch when any path is outside the vault."""
    _, _ = workspace
    created = json.loads(
        runner.invoke(app, ["new", "learning", "--title", "B", "--summary", "s", "--json"]).stdout
    )
    outside = tmp_path / "elsewhere.md"
    outside.write_text("x")

    result = runner.invoke(app, ["delete", created["path"], str(outside)])

    assert result.exit_code == 1
    assert Path(created["path"]).exists()
    assert "nothing was deleted" in result.output
