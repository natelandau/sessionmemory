"""Content-hash snapshots of a folder, so a run's writes are known by what changed.

A modification time cannot tell a file the run wrote from one a concurrent session
touched, and it misses a file rewritten within the clock's resolution. A hash of
every file before and after the run names exactly the files whose content changed.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class Changes:
    """The files a run created, modified, and deleted."""

    created: tuple[Path, ...] = ()
    modified: tuple[Path, ...] = ()
    deleted: tuple[Path, ...] = ()

    def touched(self) -> tuple[Path, ...]:
        """Return every file that exists after the run with content the run produced."""
        return (*self.created, *self.modified)

    def describe(self) -> str:
        """Return the counts of each kind of change for the log line."""
        return (
            f"{len(self.created)} created, {len(self.modified)} modified, "
            f"{len(self.deleted)} deleted"
        )


def take(root: Path) -> dict[Path, str]:
    """Hash every regular file under `root`, skipping symlinks and index files; never raises.

    The derived `.sqlite3` indexes are rewritten by any read of a field and hold
    nothing a person wrote, so they would only add noise to the diff.
    """
    hashes: dict[Path, str] = {}
    try:
        paths = sorted(root.rglob("*"))
    except OSError:
        return hashes
    for path in paths:
        if ".sqlite3" in path.name:
            continue
        try:
            # A symlink resolves outside the folder, and judging it would move the target.
            if path.is_symlink() or not path.is_file():
                continue
            hashes[path.resolve()] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return hashes


def diff(before: Mapping[Path, str], after: Mapping[Path, str]) -> Changes:
    """Classify each path whose hash differs between two snapshots."""
    return Changes(
        created=tuple(sorted(p for p in after if p not in before)),
        modified=tuple(sorted(p for p in after if p in before and after[p] != before[p])),
        deleted=tuple(sorted(p for p in before if p not in after)),
    )
