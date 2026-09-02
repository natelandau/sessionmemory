"""Shared in-memory Store factory for the hook tests.

Several test modules need a Store rooted at a tmp_path with no real project
resolution or filesystem IO, so the constructor call lives in one place rather
than drifting across the suite.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sessionhooks.store import Store  # ty: ignore[unresolved-import]

if TYPE_CHECKING:
    from pathlib import Path


def store_at(tmp_path: Path) -> Store:
    """Build a Store rooted at `tmp_path`/state (constructs only, no IO)."""
    return Store(key="k", state_dir=tmp_path / "state")
