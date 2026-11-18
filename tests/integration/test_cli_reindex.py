"""Tests for the reindex command."""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from sessionmemory.cli import app

if TYPE_CHECKING:
    from pathlib import Path

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
    assert runner.invoke(app, ["project", "register", "--cwd", str(repo)]).exit_code == 0
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


def _register(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git("init", "-q", ".", cwd=repo)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
    _git("remote", "add", "origin", f"git@example.org:nate/{name}.git", cwd=repo)
    assert runner.invoke(app, ["project", "register", "--cwd", str(repo)]).exit_code == 0
    return repo


def test_reindex_all_walks_every_project_folder(vault, tmp_path):
    """Verify --all refreshes every project's fields, registered or not, keyed by slug."""
    alpha = _register(tmp_path, "alpha")
    beta = _register(tmp_path, "beta")
    runner.invoke(app, ["new", "learning", "--title", "A", "--summary", "s", "--cwd", str(alpha)])
    runner.invoke(app, ["new", "learning", "--title", "B", "--summary", "s", "--cwd", str(beta)])
    # A folder left behind by an unregistered project still holds pages worth indexing.
    orphan = vault / "projects" / "orphan" / "learnings"
    orphan.mkdir(parents=True)
    (orphan / "kept.md").write_text("---\ntitle: kept\nsummary: s\n---\nx\n", encoding="utf-8")

    result = runner.invoke(app, ["reindex", "--all", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert list(payload) == ["alpha", "beta", "orphan"]
    assert payload["alpha"]["learnings"]["added"] == 1
    assert payload["beta"]["learnings"]["added"] == 1
    assert payload["orphan"]["learnings"]["added"] == 1
    assert "logs" not in payload["orphan"]
    for slug in ("alpha", "beta", "orphan"):
        assert (vault / "projects" / slug / "learnings" / "stub.sqlite3").is_file()


def test_reindex_all_needs_no_registered_project(vault, tmp_path, monkeypatch):
    """Verify --all runs from a directory that is not a project."""
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["reindex", "--all"])

    assert result.exit_code == 0, result.output


def test_reindex_all_rejects_cwd(vault, tmp_path):
    """Verify --all and --cwd are refused together."""
    result = runner.invoke(app, ["reindex", "--all", "--cwd", str(tmp_path)])

    assert result.exit_code == 1
    assert "mutually exclusive" in result.output


def test_reindex_reports_specs_beside_the_fields(workspace):
    """Verify reindex refreshes the specs directory and writes its index there."""
    vault, _ = workspace
    runner.invoke(app, ["new", "spec", "--title", "A"])

    result = runner.invoke(app, ["reindex", "--json"])

    payload = json.loads(result.stdout)
    assert payload["specs"]["added"] == 1
    assert (vault / "projects" / "demo" / "specs" / "stub.sqlite3").is_file()


def test_reindex_all_includes_specs(workspace):
    """Verify --all nests specs under each project beside learnings and logs."""
    runner.invoke(app, ["new", "spec", "--title", "A"])

    result = runner.invoke(app, ["reindex", "--all", "--json"])

    assert "specs" in json.loads(result.stdout)["demo"]


def test_reindex_prints_a_grid_with_totals(workspace):
    """Verify reindex reports one row per field and a totals footer."""
    runner.invoke(app, ["new", "learning", "--title", "A", "--summary", "s"])

    result = runner.invoke(app, ["reindex"])

    assert result.exit_code == 0, result.output
    lines = [line.split() for line in result.output.splitlines()]
    assert lines[0] == ["field", "added", "updated", "removed", "unchanged"]
    assert ["learnings", "1", "0", "0", "0"] in lines
    assert lines[-1] == ["total", "1", "0", "0", "0"]


def test_reindex_all_names_each_project_once(vault, tmp_path):
    """Verify --all leads each project's group of fields with its slug, shown once."""
    alpha = _register(tmp_path, "alpha")
    runner.invoke(app, ["new", "learning", "--title", "A", "--summary", "s", "--cwd", str(alpha)])

    result = runner.invoke(app, ["reindex", "--all"])

    assert result.exit_code == 0, result.output
    lines = [line.split() for line in result.output.splitlines()]
    assert lines[0][0] == "project"
    assert lines[1] == ["alpha", "learnings", "1", "0", "0", "0"]
    assert lines[2][0] == "logs"
    assert sum(line[0] == "alpha" for line in lines) == 1


def test_reindex_all_reports_an_empty_vault(vault, tmp_path, monkeypatch):
    """Verify --all on a vault with no fields says so instead of printing an empty grid."""
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["reindex", "--all"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "no fields to reindex"


def test_reindex_all_prints_a_folder_name_holding_markup_verbatim(vault):
    """Verify a project folder whose name reads as Rich markup is printed, not parsed."""
    field = vault / "projects" / "odd[bold]" / "learnings"
    field.mkdir(parents=True)
    (field / "kept.md").write_text("---\ntitle: kept\nsummary: s\n---\nx\n", encoding="utf-8")

    result = runner.invoke(app, ["reindex", "--all"])

    assert result.exit_code == 0, result.output
    assert "odd[bold]" in result.output


def test_reindex_all_lists_only_changed_projects(vault, tmp_path):
    """Verify --all hides projects with nothing to refresh and counts them instead."""
    alpha = _register(tmp_path, "alpha")
    _register(tmp_path, "beta")
    _register(tmp_path, "gamma")
    runner.invoke(app, ["reindex", "--all"])
    runner.invoke(app, ["new", "learning", "--title", "A", "--summary", "s", "--cwd", str(alpha)])

    result = runner.invoke(app, ["reindex", "--all"])

    assert result.exit_code == 0, result.output
    lines = [line.split() for line in result.output.splitlines()]
    assert ["alpha", "learnings", "1", "0", "0", "0"] in lines
    assert not any(line and line[0] in {"beta", "gamma"} for line in lines)
    assert lines[-1] == ["2", "projects", "already", "up", "to", "date"]


def test_reindex_all_skips_the_grid_when_nothing_changed(vault, tmp_path):
    """Verify --all on an up-to-date vault prints only the up-to-date count."""
    _register(tmp_path, "alpha")
    _register(tmp_path, "beta")
    runner.invoke(app, ["reindex", "--all"])

    result = runner.invoke(app, ["reindex", "--all"])

    assert result.exit_code == 0, result.output
    assert result.output.strip().splitlines() == ["all 2 projects already up to date"]
