"""Symlink-escape-hardened path containment for the post-sweep write backstop.

The sweep runs a skip-permissions agent that could be steered by prompt
injection into writing outside the trusted memory store. Before trusting any
file the agent reports writing, the sweep confirms it stays inside the store. A
plain `resolve()` plus membership test can be defeated two ways: an intermediate
directory may be a symlink pointing out of the root, or the target may not exist
yet so there is nothing to canonicalize.

`realpath_nearest_existing` closes both gaps: it canonicalizes the nearest
*existing* ancestor (resolving its symlinks) and re-appends the not-yet-created
tail lexically, so a symlinked intermediate is resolved while a missing leaf is
still placed correctly. `is_within_root` is the containment check built on it.
"""

from __future__ import annotations

from pathlib import Path


def realpath_nearest_existing(target: Path) -> Path:
    """Canonicalize `target`, resolving symlinks across its existing prefix.

    Walk up to the nearest ancestor that exists, resolve that ancestor
    (following symlinks), then re-append the missing tail components lexically.
    A target that does not exist yet is canonicalized without inventing it, and
    a symlinked intermediate directory is resolved rather than trusted, which
    defeats escapes through a symlink that points out of an intended root.
    Relative targets are anchored to the current working directory first.
    """
    current = target if target.is_absolute() else Path.cwd() / target
    tail: list[str] = []
    # `exists()` follows symlinks, so a broken symlink is treated as missing
    # and its name handled lexically rather than followed off the root.
    while not current.exists():
        parent = current.parent
        if parent == current:  # reached the filesystem root
            break
        tail.append(current.name)
        current = parent
    real = current.resolve()
    for name in reversed(tail):
        real = real / name
    return real


def _contains(real_root: Path, real_target: Path) -> bool:
    """Return whether already-canonicalized `real_target` is at or under `real_root`."""
    return real_target == real_root or real_root in real_target.parents


def is_within_root(target: Path, root: Path) -> bool:
    """Return whether `target` resolves to `root` itself or a path beneath it.

    Symlinks are resolved on both sides via `realpath_nearest_existing`, so an
    intermediate symlink cannot smuggle the target outside `root`.
    """
    return _contains(realpath_nearest_existing(root), realpath_nearest_existing(target))
