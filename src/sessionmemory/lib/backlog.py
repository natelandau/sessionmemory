"""Append an item to a project's backlog checklist.

This is the one place the item line's shape is known. A backlog line carries a kind that
must match a `## <kind>` heading, a size from a fixed set, a date, and a topic tag, and
the inject count and the backlog skill both depend on that shape being exact. Ticking
and deleting a line need no validation and stay direct edits.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sessionmemory.lib import atomic

if TYPE_CHECKING:
    from pathlib import Path

KINDS = ("feat", "fix", "refactor", "perf", "docs", "test", "build", "ci")
SIZES = ("S", "M", "L")
OPEN_ITEM = "- [ ]"
HEADING = "# Backlog"
_SECTION_PREFIX = "## "


class BacklogError(ValueError):
    """An item field that cannot be written as a well-formed line."""


def format_item(*, kind: str, size: str, description: str, topic: str | None, today: str) -> str:
    """Return the checklist line for one open item, validating every field.

    Args:
        kind (str): One of `KINDS`; the heading the line belongs under.
        size (str): One of `SIZES`.
        description (str): The imperative description, a single line.
        topic (str | None): The tag written as `[#topic]`, or None for no tag. A leading
            `#` is accepted and dropped.
        today (str): The date as `YYYY-MM-DD`.

    Returns:
        str: The line, without a trailing newline.

    Raises:
        BacklogError: A field is empty, spans lines, contains whitespace where a tag
            cannot, or names an unknown kind or size.
    """
    if kind not in KINDS:
        msg = f"kind must be one of {', '.join(KINDS)}, not {kind!r}"
        raise BacklogError(msg)
    if size not in SIZES:
        msg = f"size must be one of {', '.join(SIZES)}, not {size!r}"
        raise BacklogError(msg)
    description = description.strip()
    if not description:
        msg = "the description is empty"
        raise BacklogError(msg)
    if "\n" in description:
        msg = "the description must be a single line"
        raise BacklogError(msg)
    line = f"{OPEN_ITEM} [{size}] {description} - {today}"
    if topic is None:
        return line
    topic = topic.strip().removeprefix("#")
    if not topic:
        msg = "the topic is empty"
        raise BacklogError(msg)
    if any(character.isspace() for character in topic):
        msg = "the topic cannot contain whitespace"
        raise BacklogError(msg)
    return f"{line} [#{topic}]"


def _insert(lines: list[str], kind: str, item: str) -> list[str]:
    """Place `item` at the end of the `## kind` section, or open that section last."""
    heading = f"{_SECTION_PREFIX}{kind}"
    start = next((i for i, line in enumerate(lines) if line.rstrip() == heading), None)
    if start is None:
        return [*lines, "", heading, "", item]
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith(_SECTION_PREFIX)),
        len(lines),
    )
    last = end
    while last > start + 1 and not lines[last - 1].strip():
        last -= 1
    if last == start + 1:
        return [*lines[:last], "", item, *lines[last:]]
    return [*lines[:last], item, *lines[last:]]


def add_item(
    path: Path, *, kind: str, size: str, description: str, topic: str | None, today: str
) -> str:
    """Append one open item under its kind heading and return the line written.

    A missing file starts with `# Backlog`. An existing file keeps whatever heads it,
    since the `# Backlog` heading is never inserted into a file someone else structured.

    Args:
        path (Path): The project's `backlog.md`.
        kind (str): One of `KINDS`.
        size (str): One of `SIZES`.
        description (str): The imperative description.
        topic (str | None): The tag, or None for none.
        today (str): The date as `YYYY-MM-DD`.

    Returns:
        str: The line written, without a trailing newline.

    Raises:
        BacklogError: A field cannot be written as a well-formed line.
    """
    item = format_item(kind=kind, size=size, description=description, topic=topic, today=today)
    lines = path.read_text(encoding="utf-8").rstrip().splitlines() if path.is_file() else [HEADING]
    atomic.write_text(path, "\n".join(_insert(lines, kind=kind, item=item)) + "\n")
    return item
