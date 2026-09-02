"""Verify the symlink-hardened containment helpers the sweep's write backstop uses."""

from __future__ import annotations

from pathlib import Path

from sessionhooks.paths import (  # ty: ignore[unresolved-import]
    is_within_root,
    realpath_nearest_existing,
)


def test_realpath_nearest_existing_resolves_a_symlinked_ancestor(tmp_path: Path) -> None:
    """Verify a symlinked intermediate directory is resolved rather than trusted."""
    # Given a real directory and a symlink pointing at it
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real_dir)
    # When canonicalizing a not-yet-created path through the symlink
    result = realpath_nearest_existing(link / "note.md")
    # Then the symlink is resolved before the missing tail is appended
    assert result == real_dir.resolve() / "note.md"


def test_realpath_nearest_existing_stops_at_the_filesystem_root(monkeypatch) -> None:
    """Verify the walk-up loop terminates at the root rather than looping forever.

    `exists()` is forced to always report False so the loop cannot stop early on a
    real ancestor, exercising the `parent == current` guard that ends the walk.
    """
    # Given exists() forced to always report False, so no ancestor looks real
    monkeypatch.setattr(Path, "exists", lambda self: False)

    # When canonicalizing a path that can never be found to exist
    result = realpath_nearest_existing(Path("/some/deeply/nested/path"))

    # Then the walk still terminates and returns the path lexically rebuilt
    assert result == Path("/some/deeply/nested/path")


def test_is_within_root_true_for_the_root_itself(tmp_path: Path) -> None:
    """Verify a target equal to the root is reported as contained."""
    # Given a root / When checking the root itself / Then it is contained
    assert is_within_root(tmp_path, tmp_path) is True


def test_is_within_root_false_for_an_escape_through_a_symlink(tmp_path: Path) -> None:
    """Verify a symlinked escape out of the root is not reported as contained."""
    # Given a root whose child symlinks out to a sibling directory
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    escape = root / "escape"
    escape.symlink_to(outside)

    # When checking a path reached through the symlink
    result = is_within_root(escape / "note.md", root)

    # Then it is not reported as contained
    assert result is False
