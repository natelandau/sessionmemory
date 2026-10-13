"""Tests for the new command group."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

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


LIVING = pytest.mark.parametrize(
    ("kind", "folder"), [("reference", "reference"), ("runbook", "runbooks")]
)


@LIVING
def test_new_living_document_writes_an_undated_titled_file(workspace, kind, folder):
    """Verify a reference doc or runbook is named for its title alone and carries only title and dates."""
    vault, _ = workspace

    result = runner.invoke(app, ["new", kind, "--title", "Hosting", "--body", "Notes."])

    assert result.exit_code == 0, result.output
    path = vault / "projects" / "demo" / folder / "hosting.md"
    meta, body = parse(path.read_text(encoding="utf-8"))
    assert list(meta) == ["title", "created", "updated"]
    assert body == "Notes.\n"


@LIVING
def test_new_living_document_refuses_a_taken_name(workspace, kind, folder):
    """Verify a second doc with the same title is refused rather than written beside the first."""
    vault, _ = workspace
    runner.invoke(app, ["new", kind, "--title", "Hosting", "--body", "Original."])

    result = runner.invoke(app, ["new", kind, "--title", "Hosting"])

    assert result.exit_code == 1
    assert "already exists" in result.output
    directory = vault / "projects" / "demo" / folder
    assert sorted(path.name for path in directory.iterdir()) == ["hosting.md"]
    assert "Original." in (directory / "hosting.md").read_text(encoding="utf-8")


@LIVING
def test_new_living_document_names_the_doc_holding_a_shared_slug(workspace, kind, folder):
    """Verify a different title cut to the same slug is told which doc holds the name, not to edit it."""
    long = "Hosting settings for every environment, dashboard, credential, and bucket "
    runner.invoke(app, ["new", kind, "--title", f"{long}in production"])

    result = runner.invoke(app, ["new", kind, "--title", f"{long}in staging"])

    assert result.exit_code == 1
    assert "choose another title" in result.output
    assert "edit it instead" not in result.output


def test_new_spec_prints_only_the_created_path(workspace):
    """Verify prose output is the created file's path and nothing else."""
    result = runner.invoke(app, ["new", "spec", "--title", "Only a path"])

    assert result.exit_code == 0, result.output
    printed = Path(result.stdout.strip())
    assert len(result.stdout.strip().splitlines()) == 1
    assert printed.is_file()


def test_new_backlog_prints_the_line_then_the_path(workspace):
    """Verify prose output is the written line, then the backlog path."""
    result = runner.invoke(app, ["new", "backlog", "--kind", "fix", "--size", "S", "--title", "t"])

    assert result.exit_code == 0, result.output
    line, path = result.stdout.strip().splitlines()
    assert line == f"- [S] t - {today()}"
    assert Path(path).is_file()


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
        f"# Backlog\n\n## feat\n\n- [S] cache the model - {today()} [#index]\n"
    )
    assert "- [S] cache the model" in result.output


def test_new_backlog_json_names_path_and_line(workspace):
    """Verify the payload carries where the item went and exactly what was written."""
    result = runner.invoke(
        app, ["new", "backlog", "--kind", "fix", "--size", "M", "--title", "t", "--json"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert set(payload) == {"path", "line"}
    assert payload["path"].endswith("/demo/backlog.md")
    assert payload["line"] == f"- [M] t - {today()}"


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


def _new_backlog(*extra: str, title: str = "t") -> Result:
    return runner.invoke(
        app, ["new", "backlog", "--kind", "feat", "--size", "S", "--title", title, *extra]
    )


def test_new_backlog_warns_about_a_topic_no_line_uses(workspace):
    """Verify a topic absent from a backlog that has topics warns and still writes."""
    vault, _ = workspace
    assert _new_backlog("--topic", "web").exit_code == 0

    result = _new_backlog("--topic", "webb", title="second")

    assert result.exit_code == 0, result.output
    assert "[#webb]" in (vault / "projects" / "demo" / "backlog.md").read_text(encoding="utf-8")
    assert "#webb is a new topic" in result.stderr
    assert "in use: web\n" in result.stderr


def test_new_backlog_does_not_warn_on_the_first_topic(workspace):
    """Verify the first topic in an empty backlog raises no warning."""
    result = _new_backlog("--topic", "web")

    assert result.exit_code == 0, result.output
    assert result.stderr == ""


def test_new_backlog_warns_about_a_long_description(workspace):
    """Verify a description over 200 characters warns and still writes the line."""
    vault, _ = workspace

    result = _new_backlog(title="x" * 201)

    assert result.exit_code == 0, result.output
    assert "x" * 201 in (vault / "projects" / "demo" / "backlog.md").read_text(encoding="utf-8")
    assert "200" in result.stderr


def test_new_backlog_json_stays_parseable_when_it_warns(workspace):
    """Verify warnings go to stderr so the --json payload on stdout still parses."""
    assert _new_backlog("--topic", "web").exit_code == 0

    result = _new_backlog("--topic", "other", "--json", title="x" * 201)

    assert result.exit_code == 0, result.output
    assert result.stderr
    payload = json.loads(result.stdout)
    assert {"path", "line"} <= set(payload)


def test_new_backlog_accepts_kind_decide(workspace):
    """Verify decide is an accepted kind that writes under ## decide."""
    vault, _ = workspace

    result = runner.invoke(
        app, ["new", "backlog", "--kind", "decide", "--size", "S", "--title", "which cache?"]
    )

    assert result.exit_code == 0, result.output
    assert "## decide\n" in (vault / "projects" / "demo" / "backlog.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("topic", ["web", "#web"])
def test_new_backlog_does_not_warn_on_a_topic_already_in_use(workspace, topic):
    """Verify reusing a topic, with or without the hash, prints no warning."""
    assert _new_backlog("--topic", "web").exit_code == 0

    result = _new_backlog("--topic", topic, title="second")

    assert result.exit_code == 0, result.output
    assert result.stderr == ""


def test_new_backlog_warns_only_past_the_description_limit(workspace):
    """Verify a 200-character description is silent and a 201-character one warns."""
    assert _new_backlog(title="x" * 200).stderr == ""
    assert "201" in _new_backlog(title="y" * 201).stderr


def test_new_backlog_prints_bracketed_markup_literally(workspace):
    """Verify a closed Rich tag in a title that warns prints literally in the line."""
    title = "[bold]x[/bold] " + "y" * 200

    result = _new_backlog(title=title)

    assert result.exit_code == 0, result.output
    assert f"{len(title)} characters" in result.stderr
    assert f"- [S] {title} - " in result.stdout.splitlines()[0]


def test_new_backlog_prints_no_topic_warning_for_a_rejected_topic(workspace):
    """Verify an invalid topic fails without first warning that it is new."""
    assert _new_backlog("--topic", "web").exit_code == 0

    result = _new_backlog("--topic", "a b", title="second")

    assert result.exit_code == 1
    assert "new topic" not in result.stderr
    assert "whitespace" in result.stderr


def test_new_backlog_prints_no_length_warning_for_a_rejected_item(workspace):
    """Verify a long description on an item that fails validation prints no length warning."""
    result = _new_backlog("--topic", "a b", title="x" * 201)

    assert result.exit_code == 1
    assert "201" not in result.stderr
