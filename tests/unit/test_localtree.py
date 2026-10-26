"""Tests for the folder copy, compare, and link primitives."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sessionmemory.lib import localtree

if TYPE_CHECKING:
    from pathlib import Path


def _folder(root: Path) -> Path:
    (root / "sub").mkdir(parents=True)
    (root / "a.md").write_text("a\n")
    (root / "sub" / "b.bin").write_bytes(b"\x00\x01")
    return root


def test_create_link_refuses_an_existing_entry(tmp_path):
    """Verify create_link never replaces what is already at the path."""
    link = tmp_path / "CLAUDE.local.md"
    link.write_text("x")

    assert localtree.create_link(link, tmp_path / "target") is False
    assert link.read_text() == "x"


def test_same_tree_compares_paths_and_bytes(tmp_path):
    """Verify trees match only with the same relative files and bytes."""
    left = _folder(tmp_path / "left")
    right = _folder(tmp_path / "right")
    assert localtree.same_tree(left, right) is True

    (right / "sub" / "b.bin").write_bytes(b"\x00")
    assert localtree.same_tree(left, right) is False

    (right / "sub" / "b.bin").write_bytes(b"\x00\x01")
    (right / "extra").write_text("x")
    assert localtree.same_tree(left, right) is False


def test_first_symlink_finds_a_nested_link(tmp_path):
    """Verify the first symlink under a folder is found and a plain folder has none."""
    root = _folder(tmp_path / "root")
    assert localtree.first_symlink(root) is None

    inner = root / "sub" / "link"
    inner.symlink_to(tmp_path)
    assert localtree.first_symlink(root) == inner


def test_copy_tree_places_a_verified_copy(tmp_path):
    """Verify a copy lands at the destination and leaves no hidden sibling."""
    source = _folder(tmp_path / "source")
    dest = tmp_path / "vault" / "dest"

    assert localtree.copy_tree(source, dest) is True
    assert localtree.same_tree(source, dest)
    assert [p.name for p in dest.parent.iterdir()] == ["dest"]


def test_copy_tree_mismatch_leaves_nothing(tmp_path, monkeypatch):
    """Verify a copy that does not match is removed and nothing is placed."""
    source = _folder(tmp_path / "source")
    dest = tmp_path / "vault" / "dest"
    monkeypatch.setattr(localtree, "same_tree", lambda _a, _b: False)

    assert localtree.copy_tree(source, dest) is False
    assert list(dest.parent.iterdir()) == []


def test_set_aside_and_restore(tmp_path):
    """Verify a set-aside folder is hidden and returns to its name on restore."""
    folder = _folder(tmp_path / "skill")

    aside = localtree.set_aside(folder)

    assert aside.name.startswith(".skill.")
    assert not folder.exists()
    aside.rename(folder)
    assert (folder / "a.md").read_text() == "a\n"
