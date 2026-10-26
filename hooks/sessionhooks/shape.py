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

# Mirror `field._PAGE_NAME` and `field.is_debris`; hooks import nothing from `src`.
PAGE_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.md$")
DEBRIS_NAMES = frozenset({".DS_Store", "desktop.ini", "Thumbs.db"})
PAGE_FIELDS = frozenset({"title", "uuid", "summary", "created", "updated"})

# What `learning_problem` reports for a page it cannot read, which the sweep sets aside
# rather than judging, since its content is unknown.
UNREADABLE = "unreadable"

_KEY = re.compile(r"^([A-Za-z_][\w-]*):(.*)$")
_FENCE = "---"
_BACKLOG = "backlog.md"
LOCAL_MD = "CLAUDE.local.md"
LOCAL_DIR = "local"


def is_debris(name: str) -> bool:
    """Report whether a filename is sync, editor, or OS debris rather than a page."""
    return name in DEBRIS_NAMES or ".sync-conflict-" in name or name.endswith("~")


def _frontmatter(lines: list[str]) -> dict[str, str] | None:
    """Return the top-level keys of the leading `---` block, or None when there is none.

    Only a column-0 fence closes the block, since the CLI's serializer may wrap a value
    onto an indented continuation line that reads `---`.
    """
    if not lines or lines[0].rstrip("\r") != _FENCE:
        return None
    keys: dict[str, str] = {}
    for line in lines[1:]:
        if line.rstrip("\r") == _FENCE:
            return keys
        match = _KEY.match(line)
        if match:
            keys[match.group(1)] = match.group(2).strip().strip("\"'").strip()
    return None  # the block never closed


def learning_problem(path: Path) -> str | None:
    """Return why `path` is not a well-formed learning page, or None when it is."""
    if not PAGE_NAME.match(path.name):
        return "nonconformant name"
    try:
        # utf-8-sig drops a byte order mark an editor put ahead of the opening fence.
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return UNREADABLE
    keys = _frontmatter(text.split("\n"))
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


def local_problem(path: Path, project_dir: Path) -> str | None:
    """Return a reason when `path` is the person's, under `local/`, which the sweep never edits.

    A CLAUDE.local.md still at the project folder's top counts too, since the CLI moves it
    into `local/` only when one of its `local` commands next runs.
    """
    project = project_dir.resolve()
    resolved = path.resolve()
    if project / LOCAL_DIR in resolved.parents:
        return "local/ is the person's, not the sweep's"
    if resolved.parent == project and path.name == LOCAL_MD:
        return "CLAUDE.local.md is the person's, not the sweep's"
    return None


def nested_problem(path: Path, fields: tuple[Path, ...]) -> str | None:
    """Return a reason when `path` sits below a field rather than directly in it.

    A field is flat, and the CLI reads no page in a sub-directory of one.
    """
    if any(field in path.parents and path.parent != field for field in fields):
        return "nested in a field"
    return None
