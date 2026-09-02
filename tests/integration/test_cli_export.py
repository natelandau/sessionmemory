"""Tests for the export command."""

from __future__ import annotations

import json
import subprocess
import zipfile

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


def test_export_with_json_reports_path_and_page_count(workspace):
    """Verify export creates a zip and reports the path and page count."""
    _ = workspace
    create_result = runner.invoke(
        app, ["new", "learning", "--title", "Test Learning", "--summary", "s"]
    )
    assert create_result.exit_code == 0

    export_result = runner.invoke(app, ["export", "--json"])

    assert export_result.exit_code == 0
    payload = json.loads(export_result.stdout)
    assert "path" in payload
    assert payload["pages"] == 1
    with zipfile.ZipFile(payload["path"]) as archive:
        assert sorted(archive.namelist()) == [
            "stub.sqlite3",
            "test-learning.md",
        ]


def test_export_honors_an_explicit_output_path(workspace, tmp_path):
    """Verify --output writes the zip there instead of the default <slug>.memoryfield.zip."""
    _ = workspace
    assert (
        runner.invoke(
            app, ["new", "learning", "--title", "Test Learning", "--summary", "s"]
        ).exit_code
        == 0
    )
    destination = tmp_path / "field.zip"

    result = runner.invoke(app, ["export", "--output", str(destination), "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["path"] == str(destination)
    assert destination.is_file()


def test_export_warns_prose_when_the_field_does_not_exist(workspace):
    """Verify prose mode warns when the field was never created, rather than staying silent."""
    vault, _ = workspace

    result = runner.invoke(app, ["export"])

    assert result.exit_code == 0, result.output
    assert "does not exist" in result.output
    assert str(vault / "projects" / "demo" / "learnings") in result.output


def test_export_json_stays_silent_when_the_field_does_not_exist(workspace):
    """Verify --json reports pages: 0 with no warning mixed into the payload."""
    _ = workspace

    result = runner.invoke(app, ["export", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["pages"] == 0


def test_export_reports_an_unwritable_output_path(workspace):
    """Verify an --output whose parent directory is missing fails cleanly."""
    _ = workspace
    assert (
        runner.invoke(
            app, ["new", "learning", "--title", "Test Learning", "--summary", "s"]
        ).exit_code
        == 0
    )

    result = runner.invoke(app, ["export", "--output", "/does/not/exist/field.zip"])

    assert result.exit_code == 1
    assert "cannot write" in result.output


def test_export_prose_reports_the_page_count_and_path(workspace):
    """Verify the prose success line names the page count and where the zip landed."""
    _ = workspace
    assert (
        runner.invoke(
            app, ["new", "learning", "--title", "Test Learning", "--summary", "s"]
        ).exit_code
        == 0
    )

    result = runner.invoke(app, ["export"])

    assert result.exit_code == 0, result.output
    assert "exported 1 page" in result.output
    assert "exported 1 pages" not in result.output
