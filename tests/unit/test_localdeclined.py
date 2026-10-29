"""Tests for the list of local entries a person declined to adopt."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from sessionmemory.lib import localdeclined

if TYPE_CHECKING:
    from pathlib import Path


def test_read_missing_file_is_empty(tmp_path: Path) -> None:
    """Verify a folder with no declined file lists nothing and reports no problem."""
    assert localdeclined.read(tmp_path) == localdeclined.Declined(names=(), problems=())


def test_add_writes_sorted_names_and_reads_back(tmp_path: Path) -> None:
    """Verify add returns only new names, keeps the file sorted, and writes the header."""
    assert localdeclined.add(tmp_path, ["skills/z", "rules/a.md"]) == ["skills/z", "rules/a.md"]
    assert localdeclined.add(tmp_path, ["skills/z"]) == []

    assert localdeclined.read(tmp_path).names == ("rules/a.md", "skills/z")
    assert "local decline" in (tmp_path / "declined.toml").read_text()


def test_add_refuses_a_name_that_is_not_an_entry(tmp_path: Path) -> None:
    """Verify a path entry's name is refused and nothing is written."""
    with pytest.raises(localdeclined.DeclinedError):
        localdeclined.add(tmp_path, [".claude/agents"])

    assert not (tmp_path / "declined.toml").exists()


def test_read_reports_bad_items_and_keeps_good_ones(tmp_path: Path) -> None:
    """Verify each repeated, non-string, or non-entry item is a problem while good names survive."""
    (tmp_path / "declined.toml").write_text('names = ["skills/a", "skills/a", 3, "../x"]\n')

    declined = localdeclined.read(tmp_path)

    assert declined.names == ("skills/a",)
    assert len(declined.problems) == 3


def test_unparsable_file_is_reported_and_never_rewritten(tmp_path: Path) -> None:
    """Verify a file that does not parse is reported, refused by add, and left as written."""
    path = tmp_path / "declined.toml"
    path.write_text("names = [\n")

    declined = localdeclined.read(tmp_path)

    assert declined.names == ()
    assert len(declined.problems) == 1
    with pytest.raises(localdeclined.DeclinedError):
        localdeclined.add(tmp_path, ["skills/a"])
    assert localdeclined.remove(tmp_path, "skills/a") is False
    assert path.read_text() == "names = [\n"


def test_remove_drops_one_name(tmp_path: Path) -> None:
    """Verify remove drops a listed name once and reports a name it did not hold."""
    localdeclined.add(tmp_path, ["skills/a", "skills/b"])

    assert localdeclined.remove(tmp_path, "skills/a") is True
    assert localdeclined.remove(tmp_path, "skills/a") is False
    assert localdeclined.read(tmp_path).names == ("skills/b",)


def test_read_reports_a_name_repeated_in_another_case(tmp_path: Path) -> None:
    """Verify a name repeated with different case is a problem, as macOS folds case."""
    (tmp_path / "declined.toml").write_text('names = ["skills/a", "skills/A"]\n')

    declined = localdeclined.read(tmp_path)

    assert declined.names == ("skills/a",)
    assert declined.problems == ("skills/A is listed twice",)
