"""Tests for the search command."""

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


def test_search_finds_a_page_written_moments_ago(workspace):
    """Verify search needs no reindex first and prints path, title, and summary."""
    vault, _ = workspace
    runner.invoke(
        app, ["new", "learning", "--title", "Pytest cov warning", "--summary", "the summary"]
    )

    result = runner.invoke(app, ["search", "pytest", "--max-distance", "2"])

    assert result.exit_code == 0, result.output
    assert str(vault / "projects" / "demo" / "learnings" / "pytest-cov-warning.md") in result.stdout
    assert "the summary" in result.stdout


def test_search_json_carries_distance(workspace):
    """Verify the payload exposes the cosine distance so a caller can threshold."""
    runner.invoke(app, ["new", "learning", "--title", "A", "--summary", "s"])

    payload = json.loads(
        runner.invoke(app, ["search", "q", "--json", "--max-distance", "2"]).stdout
    )

    assert set(payload[0]) == {"path", "title", "summary", "distance"}


def test_search_logs_flag_searches_the_logs_field(workspace):
    """Verify --logs searches logs and not learnings."""
    runner.invoke(app, ["new", "learning", "--title", "Learning", "--summary", "s"])
    runner.invoke(app, ["log", "--session-id", "s", "--title", "Session", "--body", "b"])

    payload = json.loads(
        runner.invoke(app, ["search", "q", "--logs", "--json", "--max-distance", "2"]).stdout
    )

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


def test_search_drops_hits_beyond_max_distance(workspace):
    """Verify --max-distance bounds what counts as a hit, and 0 leaves nothing."""
    runner.invoke(app, ["new", "learning", "--title", "A", "--summary", "s"])

    everything = json.loads(
        runner.invoke(app, ["search", "q", "--json", "--max-distance", "2"]).stdout
    )
    nothing = runner.invoke(app, ["search", "q", "--max-distance", "0"])

    assert len(everything) == 1
    assert nothing.exit_code == 0
    assert "no results within distance 0.0" in nothing.output
    assert "--max-distance" in nothing.output


def test_search_refuses_a_max_distance_outside_the_cosine_range(workspace):
    """Verify a cutoff outside 0 to 2 is refused, since cosine distance cannot reach it."""
    result = runner.invoke(app, ["search", "q", "--max-distance", "2.5"])

    assert result.exit_code != 0
    assert "--max-distance" in result.output


def test_search_read_prints_each_hit_in_full(workspace):
    """Verify --read prints every hit's whole file under its path, so one call replaces N reads."""
    vault, _ = workspace
    runner.invoke(
        app, ["new", "learning", "--title", "Full", "--summary", "s", "--body", "the body text"]
    )
    path = vault / "projects" / "demo" / "learnings" / "full.md"

    result = runner.invoke(app, ["search", "q", "--read", "--max-distance", "2"])

    assert result.exit_code == 0, result.output
    assert result.stdout.startswith(f"{path}\n---\n")
    assert "title: Full" in result.stdout
    assert "the body text" in result.stdout


def test_search_read_json_carries_the_content(workspace):
    """Verify --read --json adds the whole file as content beside the display fields."""
    vault, _ = workspace
    runner.invoke(app, ["new", "learning", "--title", "Full", "--summary", "s", "--body", "b"])
    path = vault / "projects" / "demo" / "learnings" / "full.md"

    payload = json.loads(
        runner.invoke(app, ["search", "q", "--read", "--json", "--max-distance", "2"]).stdout
    )

    assert set(payload[0]) == {"path", "title", "summary", "distance", "content"}
    assert payload[0]["content"] == path.read_text(encoding="utf-8")
