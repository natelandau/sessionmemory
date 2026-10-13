"""The shape a sweep-written page must have, checked without importing the CLI.

The sweep model sometimes writes in another tool's format (auto-memory frontmatter,
an index file named `MEMORY.md`). A page the CLI cannot read as a page is invisible
to search and injection, so the sweep sets such a write aside instead of committing it.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# Mirrors `field._PAGE_NAME`; hooks import nothing from `src`.
PAGE_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.md$")
PAGE_FIELDS = frozenset({"title", "uuid", "summary", "created", "updated"})

_KEY = re.compile(r"^([A-Za-z_][\w-]*):(.*)$")
_FENCE = "---"
_BACKLOG = "backlog.md"


def _frontmatter(path: Path) -> dict[str, str] | None:
    """Return the top-level keys of the leading `---` block, or None when there is none."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    if not lines or lines[0].strip() != _FENCE:
        return None
    keys: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == _FENCE:
            return keys
        match = _KEY.match(line)
        if match:
            keys[match.group(1)] = match.group(2).strip().strip("\"'").strip()
    return None  # the block never closed


def learning_problem(path: Path) -> str | None:
    """Return why `path` is not a well-formed learning page, or None when it is."""
    if not PAGE_NAME.match(path.name):
        return "nonconformant name"
    keys = _frontmatter(path)
    if keys is None:
        return "no frontmatter"
    unexpected = [key for key in keys if key not in PAGE_FIELDS]
    if unexpected:
        return f"unexpected keys: {', '.join(unexpected)}"
    for required in ("title", "summary"):
        if not keys.get(required):
            return f"missing {required}"
    return None


def log_problem(path: Path) -> str | None:
    """Return why `path` is not a well-formed log page name, or None when it is."""
    return None if PAGE_NAME.match(path.name) else "nonconformant name"


def stray_problem(path: Path, project_dir: Path) -> str | None:
    """Return a reason when `path` sits directly in the project folder and is not the backlog."""
    if path.parent.resolve() == project_dir.resolve() and path.name != _BACKLOG:
        return "stray top-level file"
    return None
