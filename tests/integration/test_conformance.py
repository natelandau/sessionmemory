"""Assert a vault the CLI builds satisfies the memoryfield spec's MUSTs."""

from __future__ import annotations

import re
import sqlite3
import subprocess

import pytest
from typer.testing import CliRunner

from sessionmemory.cli import app
from sessionmemory.lib.embed import MODEL_CODE, FastEmbedder

runner = CliRunner()
PAGE_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.md$")


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


def test_a_built_field_conforms(workspace):
    """Verify flat layout, filename rule, quoted datetimes, and the spec's index schema."""
    vault, _ = workspace
    assert (
        runner.invoke(
            app, ["new", "learning", "--title", "Carbon Fiber Woks", "--summary", "s"]
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            app, ["log", "--session-id", "s", "--title", "Session", "--body", "b"]
        ).exit_code
        == 0
    )
    assert runner.invoke(app, ["reindex"]).exit_code == 0

    for name in ("learnings", "logs"):
        directory = vault / "projects" / "demo" / name
        # flat: no sub-directories with pages
        assert not [p for p in directory.rglob("*.md") if p.parent != directory]
        for page in directory.glob("*.md"):
            assert PAGE_NAME.match(page.name)
            text = page.read_text(encoding="utf-8")
            assert re.search(
                r"^created: '\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z'$", text, re.MULTILINE
            )
        conn = sqlite3.connect(directory / "stub.sqlite3")
        columns = [row[1] for row in conn.execute("PRAGMA table_info(pages)")]
        assert columns == ["filename", "frontmatter", "last_modified", "sha256_hash", "embedding"]
        conn.close()


def test_the_real_embedder_is_named_for_the_spec_model():
    """Verify the production index file is nomic-embed-text-v1.5.sqlite3."""
    assert (
        f"{FastEmbedder().name}.sqlite3"
        == f"{MODEL_CODE}.sqlite3"
        == "nomic-embed-text-v1.5.sqlite3"
    )
