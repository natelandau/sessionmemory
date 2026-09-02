"""Tests for atomic file primitives."""

from __future__ import annotations

import pytest

from sessionmemory.lib import atomic


def test_claim_creates_an_empty_file(tmp_path):
    """Create the file and report success when nothing was there before."""
    target = tmp_path / "a.md"
    assert atomic.claim(target) is True
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == ""


def test_write_text_removes_the_temporary_file_on_failure(tmp_path, mocker):
    """Verify a failed write cleans up its temp file and leaves prior content untouched."""
    target = tmp_path / "a.md"
    target.write_text("original", encoding="utf-8")
    mocker.patch("sessionmemory.lib.atomic.os.umask", side_effect=OSError("boom"))

    with pytest.raises(OSError, match="boom"):
        atomic.write_text(target, "new")

    assert target.read_text(encoding="utf-8") == "original"
    assert list(tmp_path.iterdir()) == [target]


def test_claim_refuses_an_existing_file(tmp_path):
    """Report failure and leave the existing file untouched."""
    target = tmp_path / "a.md"
    target.write_text("original", encoding="utf-8")

    assert atomic.claim(target) is False
    assert target.read_text(encoding="utf-8") == "original"


def test_claim_creates_missing_parent_directories(tmp_path):
    """Create the parent directory chain, matching write_text's own behavior."""
    target = tmp_path / "a" / "b" / "c.md"
    assert atomic.claim(target) is True
    assert target.is_file()


def _filesystem_is_case_insensitive(tmp_path) -> bool:
    """Report whether `tmp_path` treats two names differing only in case as one file.

    Case sensitivity is a property of the filesystem the test runs on, not of the
    operating system: an APFS or NTFS volume can be formatted case-sensitive, and a
    Linux box can mount a case-insensitive one. A probe file proves it directly rather
    than assuming from `sys.platform`.

    Args:
        tmp_path (Path): A writable directory to probe.

    Returns:
        bool: True when a differently-cased sibling name resolves to the same file.
    """
    probe = tmp_path / "case-probe.tmp"
    probe.write_text("x", encoding="utf-8")
    try:
        return (tmp_path / "CASE-PROBE.tmp").exists()
    finally:
        probe.unlink()


def test_claim_refuses_a_name_differing_only_in_case(tmp_path):
    """Refuse a case-only variant of an existing name, and leave that file untouched.

    On a case-insensitive filesystem, `Typer.md` and `typer.md` name the same file, so
    a second claim for the other case must not succeed, and the first file's content
    must survive. Skipped on a case-sensitive filesystem, where the two names are
    unrelated and this guarantee does not apply there; `notes.create`'s own defense
    against this collision runs through a case-folded stem comparison ahead of the
    claim and does not depend on filesystem behavior either way.
    """
    if not _filesystem_is_case_insensitive(tmp_path):
        pytest.skip("this filesystem is case-sensitive; Typer.md and typer.md are distinct names")

    target = tmp_path / "Typer.md"
    target.write_text("hand written", encoding="utf-8")

    other_case = tmp_path / "typer.md"
    assert atomic.claim(other_case) is False
    assert target.read_text(encoding="utf-8") == "hand written"
