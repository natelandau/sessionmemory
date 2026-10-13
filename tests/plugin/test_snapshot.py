"""Verify content-hash snapshots and the change set between two of them."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from sessionhooks import snapshot  # ty: ignore[unresolved-import]
from sessionhooks.snapshot import Changes  # ty: ignore[unresolved-import]

if TYPE_CHECKING:
    import pytest


def test_take_hashes_every_file_and_skips_indexes(tmp_path: Path) -> None:
    """Verify every regular file is hashed and index files are left out."""
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.md").write_text("alpha", encoding="utf-8")
    (tmp_path / "sub" / "b.md").write_text("beta", encoding="utf-8")
    (tmp_path / "nomic.sqlite3").write_bytes(b"\x00")
    (tmp_path / "nomic.sqlite3-journal").write_bytes(b"\x00")

    result = snapshot.take(tmp_path)

    assert result == {
        (tmp_path / "a.md").resolve(): hashlib.sha256(b"alpha").hexdigest(),
        (tmp_path / "sub" / "b.md").resolve(): hashlib.sha256(b"beta").hexdigest(),
    }


def test_take_of_a_missing_directory_is_empty(tmp_path: Path) -> None:
    """Verify a directory that does not exist yields an empty snapshot."""
    assert snapshot.take(tmp_path / "absent") == {}


def test_diff_reports_created_modified_and_deleted() -> None:
    """Verify each path lands in exactly one bucket, sorted."""
    before = {Path("/p/same"): "1", Path("/p/edit"): "1", Path("/p/gone"): "1"}
    after = {Path("/p/same"): "1", Path("/p/edit"): "2", Path("/p/new"): "1"}

    changes = snapshot.diff(before, after)

    assert changes == Changes(
        created=(Path("/p/new"),), modified=(Path("/p/edit"),), deleted=(Path("/p/gone"),)
    )
    assert changes.touched() == (Path("/p/new"), Path("/p/edit"))


def test_diff_of_identical_snapshots_is_empty() -> None:
    """Verify two equal snapshots report no change."""
    same = {Path("/p/a"): "1"}

    assert snapshot.diff(same, dict(same)) == Changes()


def _unreadable(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Make `Path.read_bytes` fail for every file named `name`."""
    real = Path.read_bytes

    def _read(self: Path) -> bytes:
        if self.name == name:
            message = "permission denied"
            raise OSError(message)
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", _read)


def test_take_records_an_unreadable_file_without_its_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify an unreadable file is recorded as present rather than failing the snapshot."""
    good = tmp_path / "good.md"
    bad = tmp_path / "bad.md"
    good.write_text("ok", encoding="utf-8")
    bad.write_text("no", encoding="utf-8")
    _unreadable(monkeypatch, "bad.md")

    result = snapshot.take(tmp_path)

    assert result[bad.resolve()] == snapshot.UNREADABLE
    assert result[good.resolve()] == hashlib.sha256(b"ok").hexdigest()


def test_a_file_unreadable_before_the_run_reads_as_modified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify a file that becomes readable during a run is modified, not created."""
    # Given a file the first snapshot could not read
    page = tmp_path / "page.md"
    page.write_text("text", encoding="utf-8")
    with monkeypatch.context() as patch:
        _unreadable(patch, "page.md")
        before = snapshot.take(tmp_path)
    # When the second snapshot reads it
    changes = snapshot.diff(before, snapshot.take(tmp_path))
    # Then it is a modification
    assert changes == Changes(modified=(page.resolve(),))


def test_a_file_unreadable_after_the_run_is_not_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify a file that still exists but cannot be read is never reported as deleted."""
    # Given a readable file before the run
    page = tmp_path / "page.md"
    page.write_text("text", encoding="utf-8")
    before = snapshot.take(tmp_path)
    # When the second snapshot cannot read it
    _unreadable(monkeypatch, "page.md")
    changes = snapshot.diff(before, snapshot.take(tmp_path))
    # Then it is a modification rather than a deletion, and named as unreadable
    assert changes == Changes(modified=(page.resolve(),), unreadable=(page.resolve(),))


def test_diff_names_a_created_file_it_could_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify a file created unreadable is named as unreadable as well as created."""
    # Given an empty folder, then a new file the second snapshot cannot read
    before = snapshot.take(tmp_path)
    page = tmp_path / "page.md"
    page.write_text("text", encoding="utf-8")
    _unreadable(monkeypatch, "page.md")
    # When diffing
    changes = snapshot.diff(before, snapshot.take(tmp_path))
    # Then it is created and unreadable
    assert changes == Changes(created=(page.resolve(),), unreadable=(page.resolve(),))


def test_describe_counts_each_kind() -> None:
    """Verify describe prints the three counts in order."""
    changes = Changes(created=(Path("/a"),), modified=(Path("/b"), Path("/c")), deleted=())

    assert changes.describe() == "1 created, 2 modified, 0 deleted"
