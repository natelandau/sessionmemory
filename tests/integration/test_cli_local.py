"""Tests for the local command group."""

from __future__ import annotations

import json
import subprocess
import types

import pytest
from typer.testing import CliRunner

from sessionmemory.cli import app
from sessionmemory.lib import localmd

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


def test_adopt_prints_only_the_vault_path(workspace):
    """Verify adopt prints the vault path alone on stdout and links the checkout."""
    vault, repo = workspace
    (repo / "CLAUDE.local.md").write_text("notes\n")

    result = runner.invoke(app, ["local", "adopt"])

    assert result.exit_code == 0
    assert result.stdout == f"{vault}/projects/demo/CLAUDE.local.md\n"
    assert (repo / "CLAUDE.local.md").is_symlink()


def test_adopt_warns_on_relative_import(workspace):
    """Verify adopt warns about an import that resolves from the vault copy's folder."""
    _, repo = workspace
    (repo / "CLAUDE.local.md").write_text("see @./sibling.md\n")

    result = runner.invoke(app, ["local", "adopt"])

    assert result.exit_code == 0
    assert "@./sibling.md" in result.stderr
    assert "commit the vault copy" in result.stderr


def test_adopt_when_already_linked_prints_the_path(workspace):
    """Verify a second adopt prints the vault path and exits 0."""
    vault, repo = workspace
    (repo / "CLAUDE.local.md").write_text("notes\n")
    runner.invoke(app, ["local", "adopt"])

    result = runner.invoke(app, ["local", "adopt"])

    assert result.exit_code == 0
    assert result.stdout == f"{vault}/projects/demo/CLAUDE.local.md\n"


def test_adopt_without_a_file_is_refused(workspace):
    """Verify adopt exits 1 when there is nothing to adopt."""
    result = runner.invoke(app, ["local", "adopt"])

    assert result.exit_code == 1


def test_link_json_created_then_present(workspace):
    """Verify link reports created, then present, and the symlink exists."""
    vault, repo = workspace
    target = vault / "projects" / "demo" / "CLAUDE.local.md"
    target.write_text("notes\n")

    first = runner.invoke(app, ["local", "link", "--json"])
    second = runner.invoke(app, ["local", "link", "--json"])

    assert first.exit_code == 0
    assert json.loads(first.stdout) == {
        "status": "created",
        "link": str(repo.resolve() / "CLAUDE.local.md"),
        "target": str(target),
    }
    assert second.exit_code == 0
    assert json.loads(second.stdout)["status"] == "present"
    assert (repo / "CLAUDE.local.md").is_symlink()


def test_link_prose_prints_the_link_path(workspace):
    """Verify link without --json prints the link path alone."""
    vault, repo = workspace
    (vault / "projects" / "demo" / "CLAUDE.local.md").write_text("notes\n")

    result = runner.invoke(app, ["local", "link"])

    assert result.exit_code == 0
    assert result.stdout == f"{repo.resolve() / 'CLAUDE.local.md'}\n"


def test_link_conflict_exits_1_with_json_status(workspace):
    """Verify a regular file beside a vault copy exits 1 and still prints the status."""
    vault, repo = workspace
    (vault / "projects" / "demo" / "CLAUDE.local.md").write_text("vault\n")
    (repo / "CLAUDE.local.md").write_text("local\n")

    result = runner.invoke(app, ["local", "link", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["status"] == "conflict"


def test_link_conflict_in_prose_names_the_next_commands(workspace):
    """Verify the prose conflict error points at adopt and status."""
    vault, repo = workspace
    (vault / "projects" / "demo" / "CLAUDE.local.md").write_text("vault\n")
    (repo / "CLAUDE.local.md").write_text("local\n")

    result = runner.invoke(app, ["local", "link"])

    assert result.exit_code == 1
    assert "local adopt" in result.stderr
    assert "local status" in result.stderr


def test_link_none_exits_0(workspace):
    """Verify link exits 0 when the vault holds no copy."""
    result = runner.invoke(app, ["local", "link", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "none"


def test_link_none_in_prose_prints_nothing_on_stdout(workspace):
    """Verify link prints an info line, not a value, when there is no vault copy."""
    result = runner.invoke(app, ["local", "link"])

    assert result.exit_code == 0
    assert "holds no CLAUDE.local.md" in result.stdout + result.stderr
    assert str(workspace[1]) not in result.stdout


def test_status_json_reports_state_and_ignore(workspace):
    """Verify status reports the state, ignore flag, and content match."""
    vault, repo = workspace
    (vault / "projects" / "demo" / "CLAUDE.local.md").write_text("same\n")
    (repo / "CLAUDE.local.md").write_text("same\n")

    result = runner.invoke(app, ["local", "status", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["state"] == "diverged"
    assert payload["ignored"] is False
    assert payload["tracked"] is False
    assert payload["match"] is True


def test_status_prose_lists_state_and_contents(workspace):
    """Verify status prose shows the state and, when diverged, the contents verdict."""
    vault, repo = workspace
    (vault / "projects" / "demo" / "CLAUDE.local.md").write_text("a\n")
    (repo / "CLAUDE.local.md").write_text("b\n")

    result = runner.invoke(app, ["local", "status"])

    assert result.exit_code == 0
    assert "state: diverged" in result.stdout
    assert "ignored: no" in result.stdout
    assert "tracked: no" in result.stdout
    assert "contents: different" in result.stdout


def test_unregistered_directory_is_refused(vault, tmp_path):
    """Verify a directory no project owns is refused."""
    other = tmp_path / "other"
    other.mkdir()

    result = runner.invoke(app, ["local", "status", "--cwd", str(other)])

    assert result.exit_code == 1


def test_outside_git_is_refused(workspace, monkeypatch):
    """Verify a registered directory outside a git work tree is refused by the checkout."""

    def not_a_work_tree(
        _cwd: object, *_args: str, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=128, stdout="", stderr="fatal")

    monkeypatch.setattr(localmd, "gitinfo", types.SimpleNamespace(run_git=not_a_work_tree))

    result = runner.invoke(app, ["local", "status"])

    assert result.exit_code == 1
    assert "is not inside a git work tree" in result.stderr


def test_status_reports_a_git_failure(workspace, monkeypatch):
    """Verify status exits 1 with a message when git cannot run."""

    def broken(cwd, *_args) -> None:
        msg = f"git could not run in {cwd}"
        raise localmd.LocalMdError(msg)

    monkeypatch.setattr(localmd, "_git", broken)

    result = runner.invoke(app, ["local", "status"])

    assert result.exit_code == 1
    assert "git could not run" in result.stderr


def test_status_prose_reports_a_tracked_file(workspace):
    """Verify status prose says when git tracks the checkout's file."""
    _, repo = workspace
    (repo / "CLAUDE.local.md").write_text("committed\n")
    _git("add", "-f", "CLAUDE.local.md", cwd=repo)

    result = runner.invoke(app, ["local", "status"])

    assert result.exit_code == 0
    assert "tracked: yes" in result.stdout


def test_adopt_on_a_linked_checkout_restores_the_ignore_entry(workspace):
    """Verify adopt on an already linked checkout prints only the path and ignores the link."""
    vault, repo = workspace
    copy = vault / "projects" / "demo" / "CLAUDE.local.md"
    copy.write_text("notes\n")
    (repo / "CLAUDE.local.md").symlink_to(copy)

    result = runner.invoke(app, ["local", "adopt"])

    assert result.exit_code == 0
    assert result.stdout == f"{copy}\n"
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "CLAUDE.local.md"], cwd=repo, check=False
    )
    assert ignored.returncode == 0
