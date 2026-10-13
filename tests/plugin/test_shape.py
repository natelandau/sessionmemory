"""Tests for the page-shape checks the sweep applies to what it wrote."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sessionhooks.shape import (  # ty: ignore[unresolved-import]
    learning_problem,
    log_problem,
    stray_problem,
)

if TYPE_CHECKING:
    from pathlib import Path

GOOD = '---\ntitle: "A title"\nuuid: abc\nsummary: "What it says"\ncreated: "2026-10-04"\nupdated: "2026-10-04"\n---\n\nBody.\n'


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_learning_problem_accepts_a_conformant_page(tmp_path: Path) -> None:
    """Verify a page with the five fields and a conformant name passes."""
    assert learning_problem(_write(tmp_path / "a-title.md", GOOD)) is None


def test_learning_problem_rejects_auto_memory_frontmatter(tmp_path: Path) -> None:
    """Verify auto-memory keys are reported as unexpected."""
    text = "---\nname: x\ndescription: y\nmetadata:\n  type: user\n---\nBody\n"
    problem = learning_problem(_write(tmp_path / "x.md", text))
    assert problem == "unexpected keys: name, description, metadata"


def test_learning_problem_rejects_a_missing_summary(tmp_path: Path) -> None:
    """Verify a page without a summary is reported."""
    problem = learning_problem(_write(tmp_path / "x.md", '---\ntitle: "T"\n---\nBody\n'))
    assert problem == "missing summary"


def test_learning_problem_rejects_a_blank_title(tmp_path: Path) -> None:
    """Verify an empty quoted title counts as missing."""
    text = '---\ntitle: ""\nsummary: "S"\n---\nBody\n'
    assert learning_problem(_write(tmp_path / "x.md", text)) == "missing title"


def test_learning_problem_rejects_a_page_without_frontmatter(tmp_path: Path) -> None:
    """Verify a page with no leading block is reported."""
    assert learning_problem(_write(tmp_path / "x.md", "Body\n")) == "no frontmatter"


def test_learning_problem_rejects_a_nonconformant_name(tmp_path: Path) -> None:
    """Verify MEMORY.md fails on its name."""
    assert learning_problem(_write(tmp_path / "MEMORY.md", GOOD)) == "nonconformant name"


def test_log_problem_checks_only_the_name(tmp_path: Path) -> None:
    """Verify a dated log name passes and an uppercase one fails."""
    assert log_problem(_write(tmp_path / "2026-10-04-2026-10-04.md", "x")) is None
    assert log_problem(_write(tmp_path / "Notes.md", "x")) == "nonconformant name"


def test_stray_problem_flags_top_level_files_except_the_backlog(tmp_path: Path) -> None:
    """Verify only backlog.md may sit directly in the project folder."""
    project = tmp_path / "x"
    assert stray_problem(_write(project / "MEMORY.md", "x"), project) == "stray top-level file"
    assert stray_problem(_write(project / "backlog.md", "x"), project) is None
    assert stray_problem(_write(project / "specs" / "a.md", "x"), project) is None
