"""Tests for the new command group."""

from __future__ import annotations

import json
import subprocess

import pytest
from typer.testing import CliRunner

from sessionmemory.cli import app
from sessionmemory.lib.config import today
from sessionmemory.lib.frontmatter import parse

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


def test_new_learning_writes_a_page_into_the_project_field(workspace):
    """Verify a learning lands in projects/<slug>/learnings with the five fields."""
    vault, _ = workspace

    result = runner.invoke(
        app, ["new", "learning", "--title", "Typer vendored click", "--summary", "s"]
    )

    assert result.exit_code == 0, result.output
    path = vault / "projects" / "demo" / "learnings" / "typer-vendored-click.md"
    assert path.is_file()
    meta, _ = parse(path.read_text(encoding="utf-8"))
    assert list(meta) == ["title", "uuid", "summary", "created", "updated"]


def test_new_learning_json_names_path_title_and_uuid(workspace):
    """Verify the payload carries what a caller needs to write the body next."""
    result = runner.invoke(app, ["new", "learning", "--title", "T", "--summary", "s", "--json"])

    payload = json.loads(result.stdout)
    assert set(payload) == {"path", "title", "uuid"}
    assert payload["path"].endswith("/learnings/t.md")


def test_new_learning_body_from_stdin(workspace):
    """Verify --body-file - reads the body from stdin."""
    vault, _ = workspace

    result = runner.invoke(
        app,
        ["new", "learning", "--title", "T", "--summary", "s", "--body-file", "-"],
        input="Prose.\n",
    )

    assert result.exit_code == 0
    _, body = parse(
        (vault / "projects" / "demo" / "learnings" / "t.md").read_text(encoding="utf-8")
    )
    assert body == "Prose.\n"


@pytest.mark.parametrize(("kind", "folder"), [("spec", "specs"), ("plan", "plans")])
def test_new_document_writes_title_and_dates_only(workspace, kind, folder):
    """Verify a spec or plan is a dated, titled file with no uuid or summary."""
    vault, _ = workspace

    result = runner.invoke(app, ["new", kind, "--title", "A Thing"])

    assert result.exit_code == 0, result.output
    path = vault / "projects" / "demo" / folder / f"{today()}-a-thing.md"
    meta, _ = parse(path.read_text(encoding="utf-8"))
    assert list(meta) == ["title", "created", "updated"]


def test_new_document_json_carries_no_uuid(workspace):
    """Verify a spec's --json payload has no uuid, unlike a learning's."""
    result = runner.invoke(app, ["new", "spec", "--title", "A Plan", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert set(payload) == {"path", "title"}


def test_new_learning_outside_a_registered_project_fails(vault, tmp_path, monkeypatch):
    """Verify an unregistered directory is refused with the fix named."""
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["new", "learning", "--title", "T", "--summary", "s"])

    assert result.exit_code == 1
    assert "sessionmemory project --register" in result.output


def test_new_learning_with_unusable_title_fails(workspace):
    """Verify a title that yields no filename is refused."""
    result = runner.invoke(app, ["new", "learning", "--title", "!!!", "--summary", "s"])

    assert result.exit_code == 1


def test_new_backlog_writes_a_dated_item_under_its_kind_heading(workspace):
    """Verify the item lands in projects/<slug>/backlog.md with today's date and the tag."""
    vault, _ = workspace

    result = runner.invoke(
        app,
        [
            "new",
            "backlog",
            "--kind",
            "feat",
            "--size",
            "S",
            "--title",
            "cache the model",
            "--topic",
            "index",
        ],
    )

    assert result.exit_code == 0, result.output
    path = vault / "projects" / "demo" / "backlog.md"
    assert path.read_text(encoding="utf-8") == (
        f"# Backlog\n\n## feat\n\n- [ ] [S] cache the model - {today()} [#index]\n"
    )
    assert "- [ ] [S] cache the model" in result.output


def test_new_backlog_json_names_path_and_line(workspace):
    """Verify the payload carries where the item went and exactly what was written."""
    result = runner.invoke(
        app, ["new", "backlog", "--kind", "fix", "--size", "M", "--title", "t", "--json"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert set(payload) == {"path", "line"}
    assert payload["path"].endswith("/demo/backlog.md")
    assert payload["line"] == f"- [ ] [M] t - {today()}"


def test_new_backlog_rejects_an_unknown_kind_and_names_the_allowed_set(workspace):
    """Verify a kind outside the heading set fails at parse time with the choices listed."""
    result = runner.invoke(
        app, ["new", "backlog", "--kind", "chore", "--size", "S", "--title", "t"]
    )

    assert result.exit_code != 0
    assert "chore" in result.output
    assert "refactor" in result.output


def test_new_backlog_item_is_counted_by_inject(workspace):
    """Verify the line the command writes is the shape the session start counts."""
    runner.invoke(app, ["new", "backlog", "--kind", "docs", "--size", "L", "--title", "t"])

    result = runner.invoke(app, ["inject", "--json"])

    assert json.loads(result.stdout)["open_backlog"] == 1


def test_new_backlog_with_empty_title_fails(workspace):
    """Verify a blank description is refused rather than written as an empty item."""
    result = runner.invoke(app, ["new", "backlog", "--kind", "docs", "--size", "S", "--title", " "])

    assert result.exit_code == 1
