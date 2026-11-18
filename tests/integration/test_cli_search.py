"""Tests for the search command."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sessionmemory.cli import app
from sessionmemory.lib import fieldindex

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
    # The stub's distances are random, so the measured cutoffs would turn every page away.
    admit_all = fieldindex.Cutoffs(max_distance=2.0, min_margin=0.0, min_drop=1.0, drop_window=5)
    monkeypatch.setattr(fieldindex, "default_cutoffs", lambda _field_dir: admit_all)
    return vault, repo


def test_search_finds_a_page_written_moments_ago(workspace):
    """Verify search needs no reindex first and prints path, title, and summary."""
    vault, _ = workspace
    runner.invoke(
        app, ["new", "learning", "--title", "Pytest cov warning", "--summary", "the summary"]
    )

    result = runner.invoke(app, ["search", "pytest"])

    assert result.exit_code == 0, result.output
    assert str(vault / "projects" / "demo" / "learnings" / "pytest-cov-warning.md") in result.stdout
    assert "the summary" in result.stdout


def test_search_json_carries_distance(workspace):
    """Verify the payload exposes the cosine distance so a caller can threshold."""
    runner.invoke(app, ["new", "learning", "--title", "A", "--summary", "s"])

    payload = json.loads(runner.invoke(app, ["search", "q", "--json"]).stdout)

    assert set(payload[0]) == {"path", "title", "summary", "distance"}


def test_search_logs_flag_searches_the_logs_field(workspace):
    """Verify --logs searches logs and not learnings."""
    runner.invoke(app, ["new", "learning", "--title", "Learning", "--summary", "s"])
    runner.invoke(app, ["log", "--session-id", "s", "--title", "Session", "--body", "b"])

    payload = json.loads(runner.invoke(app, ["search", "q", "--logs", "--json"]).stdout)

    assert [hit["title"] for hit in payload] == ["Session"]


def test_search_empty_project_says_no_results(workspace):
    """Verify a project with no pages yet reports no results and exits 0."""
    result = runner.invoke(app, ["search", "q"])

    assert result.exit_code == 0
    assert "no results" in result.output


def test_search_refuses_a_limit_below_one(workspace):
    """Verify --limit 0 is refused rather than silently returning nothing."""
    result = runner.invoke(app, ["search", "q", "--limit", "0"])

    assert result.exit_code == 1
    assert "--limit must be at least 1" in result.output


def test_search_read_prints_each_hit_in_full(workspace):
    """Verify --read prints every hit's whole file under its path, so one call replaces N reads."""
    vault, _ = workspace
    runner.invoke(
        app, ["new", "learning", "--title", "Full", "--summary", "s", "--body", "the body text"]
    )
    path = vault / "projects" / "demo" / "learnings" / "full.md"

    result = runner.invoke(app, ["search", "q", "--read"])

    assert result.exit_code == 0, result.output
    assert result.stdout.startswith(f"{path}\n---\n")
    assert "title: Full" in result.stdout
    assert "the body text" in result.stdout


def test_search_read_json_carries_the_content(workspace):
    """Verify --read --json adds the whole file as content beside the display fields."""
    vault, _ = workspace
    runner.invoke(app, ["new", "learning", "--title", "Full", "--summary", "s", "--body", "b"])
    path = vault / "projects" / "demo" / "learnings" / "full.md"

    payload = json.loads(runner.invoke(app, ["search", "q", "--read", "--json"]).stdout)

    assert set(payload[0]) == {"path", "title", "summary", "distance", "content"}
    assert payload[0]["content"] == path.read_text(encoding="utf-8")


def _new_spec(title: str, body: str, status: str | None = None) -> Path:
    path = Path(runner.invoke(app, ["new", "spec", "--title", title]).stdout.strip())
    front = f"---\nstatus: {status}\n---\n" if status else ""
    path.write_text(f"{front}# {title}\n\n{body}\n")
    return path


def test_search_specs_prints_path_title_and_summary(workspace):
    """Verify --specs prints each spec's path, title, and derived summary."""
    path = _new_spec("Pricing", "## Goal\n\nWho pays.")

    result = runner.invoke(app, ["search", "q", "--specs"])

    assert result.exit_code == 0, result.output
    assert str(path) in result.stdout
    assert "  Pricing" in result.stdout
    assert "  Who pays." in result.stdout


def test_search_specs_hides_retired_unless_asked(workspace):
    """Verify retired specs are hidden by default and marked when included."""
    _new_spec("Current design", "## Goal\n\nKeep.")
    _new_spec("Old design", "## Goal\n\nGone.", status="superseded")

    default = runner.invoke(app, ["search", "q", "--specs"])
    everything = runner.invoke(app, ["search", "q", "--specs", "--include-retired"])

    assert "Current design" in default.stdout
    assert "Old design" not in default.stdout
    assert "Current design" in everything.stdout
    assert "  Old design (superseded)" in everything.stdout


def test_search_specs_json_carries_status(workspace):
    """Verify every spec entry in the JSON payload includes its status."""
    _new_spec("One", "## Goal\n\nA.")
    _new_spec("Two", "## Goal\n\nB.", status="shipped")

    payload = json.loads(runner.invoke(app, ["search", "q", "--specs", "--json"]).stdout)

    assert payload
    for entry in payload:
        assert set(entry) == {"path", "title", "summary", "distance", "status"}


def test_search_specs_read_prints_the_whole_spec(workspace):
    """Verify --read prints text from deep in the spec body."""
    _new_spec("Deep", "## Goal\n\nShort.\n\n## Problem\n\nA line only found deep inside.")

    result = runner.invoke(app, ["search", "q", "--specs", "--read"])

    assert result.exit_code == 0, result.output
    assert "A line only found deep inside." in result.stdout


def test_search_specs_read_json_carries_content_and_status(workspace):
    """Verify --specs --read --json entries carry the whole file and the spec's status."""
    path = _new_spec("Deep", "## Goal\n\nShort.", status="shipped")

    result = runner.invoke(app, ["search", "q", "--specs", "--read", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert set(payload[0]) == {"path", "title", "summary", "distance", "status", "content"}
    assert payload[0]["content"] == path.read_text(encoding="utf-8")
    assert payload[0]["status"] == "shipped"


def test_search_refuses_include_retired_without_specs(workspace):
    """Verify --include-retired alone is a usage error, since only specs are retired."""
    result = runner.invoke(app, ["search", "q", "--include-retired"])

    assert result.exit_code != 0
    assert "--include-retired needs --specs" in result.output


def test_search_learning_with_a_retired_status_prints_no_suffix(workspace):
    """Verify a learning carrying `status: superseded` is not marked as a retired spec."""
    vault, _ = workspace
    runner.invoke(app, ["new", "learning", "--title", "Kept", "--summary", "s"])
    page = vault / "projects" / "demo" / "learnings" / "kept.md"
    page.write_text(
        page.read_text(encoding="utf-8").replace("---\n", "---\nstatus: superseded\n", 1)
    )

    result = runner.invoke(app, ["search", "q"])

    assert result.exit_code == 0, result.output
    assert "  Kept\n" in result.stdout
    assert "(superseded)" not in result.stdout


def test_search_refuses_specs_with_logs(workspace):
    """Verify --specs and --logs together is a usage error."""
    result = runner.invoke(app, ["search", "q", "--specs", "--logs"])

    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


def test_search_empty_learnings_names_specs_and_logs(workspace):
    """Verify an empty learnings search points at the other fields."""
    result = runner.invoke(app, ["search", "q"])

    assert "--specs" in result.output
    assert "--logs" in result.output


def test_search_learnings_json_has_no_status_key(workspace):
    """Verify the learnings payload shape is unchanged."""
    runner.invoke(app, ["new", "learning", "--title", "A", "--summary", "s"])

    payload = json.loads(runner.invoke(app, ["search", "q", "--json"]).stdout)

    assert set(payload[0]) == {"path", "title", "summary", "distance"}


@pytest.mark.parametrize(
    ("flags", "directory"),
    [(["--specs"], "specs"), ([], "learnings"), (["--logs"], "logs")],
)
def test_search_flags_pick_the_directory(workspace, mocker, flags, directory):
    """Verify each flag searches its own directory."""
    search = mocker.patch.object(fieldindex, "search", autospec=True, return_value=[])

    runner.invoke(app, ["search", "q", *flags])

    assert search.call_args.args[0].name == directory


def test_search_joins_an_unquoted_query(workspace, mocker):
    """Verify several words on the command line are searched as one query."""
    spy = mocker.spy(fieldindex, "search")

    result = runner.invoke(app, ["search", "connection", "pool", "size"])

    assert result.exit_code == 0, result.output
    assert spy.call_args.args[2] == "connection pool size"
