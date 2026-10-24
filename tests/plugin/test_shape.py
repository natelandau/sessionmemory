"""Tests for the page-shape checks the sweep applies to what it wrote."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sessionhooks.shape import (  # ty: ignore[unresolved-import]
    UNREADABLE,
    is_debris,
    learning_problem,
    local_md_problem,
    log_problem,
    nested_problem,
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


def test_local_md_problem_flags_only_the_top_level_file(tmp_path: Path) -> None:
    """Verify only CLAUDE.local.md directly in the project folder is the person's."""
    project = tmp_path / "x"
    message = "CLAUDE.local.md is the person's, not the sweep's"
    assert local_md_problem(_write(project / "CLAUDE.local.md", "x"), project) == message
    assert local_md_problem(_write(project / "learnings" / "CLAUDE.local.md", "x"), project) is None
    assert local_md_problem(_write(project / "backlog.md", "x"), project) is None


def test_learning_problem_reads_past_an_indented_fence(tmp_path: Path) -> None:
    """Verify an indented `---` inside a value does not close the frontmatter early."""
    # Given a page whose wrapped summary carries an indented `---` line before a bad key
    text = '---\ntitle: "T"\nsummary: S\n  ---\nname: x\n---\n\nBody\n'
    page = _write(tmp_path / "learnings" / "page.md", text)
    # When checked / Then the key after it is still seen
    assert learning_problem(page) == "unexpected keys: name"


def test_learning_problem_accepts_a_crlf_page(tmp_path: Path) -> None:
    """Verify a page saved with CRLF line endings still reads its closing fence."""
    page = tmp_path / "learnings" / "page.md"
    page.parent.mkdir(parents=True)
    page.write_bytes(GOOD.replace("\n", "\r\n").encode("utf-8"))
    assert learning_problem(page) is None


def test_learning_problem_accepts_a_page_with_a_byte_order_mark(tmp_path: Path) -> None:
    """Verify a BOM ahead of the opening fence does not hide the frontmatter."""
    page = _write(tmp_path / "learnings" / "page.md", "\ufeff" + GOOD)
    assert learning_problem(page) is None


def test_learning_problem_reports_an_unreadable_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify a page that cannot be read is reported as unreadable."""
    page = _write(tmp_path / "learnings" / "page.md", GOOD)

    def _deny(*_args: object, **_kwargs: object) -> str:
        message = "permission denied"
        raise PermissionError(message)

    monkeypatch.setattr(type(page), "read_text", _deny)
    assert learning_problem(page) == UNREADABLE


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (".DS_Store", True),
        ("desktop.ini", True),
        ("Thumbs.db", True),
        ("page.sync-conflict-20260101-120000-ABCDEFG.md", True),
        ("page.md~", True),
        ("page.md", False),
        ("MEMORY.md", False),
    ],
)
def test_is_debris_matches_the_cli_rule(name: str, expected: bool) -> None:
    """Verify the debris rule names sync, editor, and OS files and nothing else."""
    assert is_debris(name) is expected


def test_nested_problem_flags_a_file_below_a_field(tmp_path: Path) -> None:
    """Verify a file in a sub-directory of a field is named, and a direct child is not."""
    learnings = tmp_path / "learnings"
    logs = tmp_path / "logs"
    fields = (learnings, logs)
    assert nested_problem(learnings / "sub" / "page.md", fields) == "nested in a field"
    assert nested_problem(logs / "a" / "b" / "page.md", fields) == "nested in a field"
    assert nested_problem(learnings / "page.md", fields) is None
    assert nested_problem(tmp_path / "specs" / "x.md", fields) is None
