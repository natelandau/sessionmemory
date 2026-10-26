"""Tests for the log command."""

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
    assert runner.invoke(app, ["project", "register", "--cwd", str(repo)]).exit_code == 0
    monkeypatch.chdir(repo)
    return vault, repo


def test_log_creates_then_updates(workspace):
    """Verify two calls with one session id produce one file with the second body."""
    vault, _ = workspace
    first = runner.invoke(
        app, ["log", "--session-id", "s1", "--title", "Work", "--body", "one", "--json"]
    )
    second = runner.invoke(
        app, ["log", "--session-id", "s1", "--title", "Work", "--body", "two", "--json"]
    )

    assert json.loads(first.stdout)["action"] == "created"
    assert json.loads(second.stdout)["action"] == "updated"
    logs = list((vault / "projects" / "demo" / "logs").glob("*.md"))
    assert len(logs) == 1
    assert logs[0].read_text(encoding="utf-8").endswith("two\n")


def test_log_with_unusable_title_fails(workspace):
    """Verify a title that yields no filename is refused."""
    result = runner.invoke(app, ["log", "--session-id", "s1", "--title", "!!!"])

    assert result.exit_code == 1


def test_log_records_transcript_and_url(workspace):
    """Verify --transcript and --url land in the page's frontmatter."""
    vault, _ = workspace
    result = runner.invoke(
        app,
        [
            "log",
            "--session-id",
            "s2",
            "--title",
            "Linked",
            "--transcript",
            "/home/me/.claude/projects/x/s2.jsonl",
            "--url",
            "https://claude.ai/code/session_01ABC",
        ],
    )

    assert result.exit_code == 0, result.output
    page = next((vault / "projects" / "demo" / "logs").glob("*.md"))
    text = page.read_text(encoding="utf-8")
    assert "transcript: /home/me/.claude/projects/x/s2.jsonl" in text
    assert "session_url: https://claude.ai/code/session_01ABC" in text


def test_log_title_comes_from_the_environment_over_the_flag(workspace, monkeypatch):
    """Verify the environment's title wins over the --title flag."""
    vault, _ = workspace
    monkeypatch.setenv("SESSIONMEMORY_LOG_TITLE", "2026-10-05 22:42")

    result = runner.invoke(app, ["log", "--session-id", "s3", "--title", "2026-10-06 00:42"])

    assert result.exit_code == 0, result.output
    page = next((vault / "projects" / "demo" / "logs").glob("*.md"))
    assert page.name.startswith("2026-10-05-")
    assert "title: 2026-10-05 22:42" in page.read_text(encoding="utf-8")


def test_log_without_a_title_uses_the_environment(workspace, monkeypatch):
    """Verify --title may be omitted when the environment supplies one."""
    monkeypatch.setenv("SESSIONMEMORY_LOG_TITLE", "2026-10-05 22:42")

    result = runner.invoke(app, ["log", "--session-id", "s4"])

    assert result.exit_code == 0, result.output


def test_log_without_a_title_or_environment_is_refused(workspace, monkeypatch):
    """Verify a log with no title from either source exits 1 and names --title."""
    monkeypatch.delenv("SESSIONMEMORY_LOG_TITLE", raising=False)

    result = runner.invoke(app, ["log", "--session-id", "s5"])

    assert result.exit_code == 1
    assert "--title" in result.output


def test_log_prints_only_the_page_path(workspace):
    """Verify log without --json prints the page's path and nothing else, as `new` does."""
    vault, _ = workspace
    first = runner.invoke(app, ["log", "--session-id", "s6", "--title", "Work", "--body", "one"])
    second = runner.invoke(app, ["log", "--session-id", "s6", "--title", "Work", "--body", "two"])

    page = next((vault / "projects" / "demo" / "logs").glob("*.md"))
    assert first.exit_code == 0, first.output
    assert first.stdout == f"{page}\n"
    assert second.stdout == f"{page}\n"


def test_log_strips_a_padded_title_flag(workspace, monkeypatch):
    """Verify a --title with surrounding whitespace is written without it."""
    vault, _ = workspace
    monkeypatch.delenv("SESSIONMEMORY_LOG_TITLE", raising=False)
    result = runner.invoke(app, ["log", "--session-id", "s7", "--title", "  Padded work \t"])

    assert result.exit_code == 0, result.output
    page = next((vault / "projects" / "demo" / "logs").glob("*.md"))
    assert page.name.endswith("-padded-work.md")
    assert "title: Padded work\n" in page.read_text(encoding="utf-8")
