"""Append an item to a project's backlog and recognize one.

This is the one place the item line's shape is known. A line written here carries a size
from a fixed set, a date, and an optional topic tag under a `## <kind>` heading. Counting
does not depend on that shape: a person writes lines by hand, often as plain prose, and
every top-level bullet with text is an item. The line has no checkbox: finished work is
deleted, and git history is the record of it, so there is nothing to tick. Deleting a
line needs no validation and stays a direct edit.

Context that would crowd the line goes in an optional detail block: indented bullets
directly under it. Counting ignores indented lines, so the headline stays the item and
the detail never inflates the count.
"""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sessionmemory.lib import atomic

if TYPE_CHECKING:
    from pathlib import Path

KINDS = ("feat", "fix", "refactor", "perf", "docs", "test", "build", "ci", "decide")
SIZES = ("S", "M", "L")
HEADING = "# Backlog"
_SECTION_PREFIX = "## "
LONG_DESCRIPTION = 150
LONG_DETAIL = 1500
DETAIL_BULLET = "  - "
_DETAIL_CONTINUATION = "    "
DETAIL_WIDTH = 88
_TICKED = ("- [x]", "- [X]")
_TOPIC_TAG = re.compile(r"\[#([^\s\]]+)\]")
_BULLET_MARKER = re.compile(r"^-(\s+|$)")


class BacklogError(ValueError):
    """An item field that cannot be written as a well-formed line."""


@dataclass(frozen=True)
class ShapeWarning:
    """A field that is written as given but likely misplaced, with what to do instead."""

    message: str
    advice: str


def _tag(topic: str) -> str:
    return topic.strip().removeprefix("#")


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
        BacklogError: A field is empty, spans lines, contains whitespace or a bracket
            where a tag cannot, or names an unknown kind or size.
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
    line = f"- [{size}] {description} - {today}"
    if topic is None:
        return line
    topic = _tag(topic)
    if not topic:
        msg = "the topic is empty"
        raise BacklogError(msg)
    if any(character.isspace() for character in topic):
        msg = "the topic cannot contain whitespace"
        raise BacklogError(msg)
    if "[" in topic or "]" in topic:
        msg = f"the topic cannot contain a bracket, which ends its [#topic] tag: {topic!r}"
        raise BacklogError(msg)
    return f"{line} [#{topic}]"


def format_detail(detail: str | None) -> list[str]:
    """Return the nested bullets that carry an item's detail under its headline.

    Each line of `detail` becomes one bullet, so a markdown viewer shows the lines apart
    instead of joining them into the item's paragraph. A bullet the caller wrote keeps
    its text without gaining a second marker. Blank lines are dropped so the item stays
    one tight block and the next item's insert point is unambiguous.

    Args:
        detail (str | None): Free text, possibly several lines, or None for no detail.

    Returns:
        list[str]: The lines to write, each bullet starting with `DETAIL_BULLET` and
            wrapped onto indented continuation lines, or an empty list when there is no
            detail.
    """
    texts = (_BULLET_MARKER.sub("", line.strip()) for line in (detail or "").splitlines())
    return [
        wrapped
        for text in texts
        if text
        for wrapped in textwrap.wrap(
            text,
            width=DETAIL_WIDTH,
            initial_indent=DETAIL_BULLET,
            subsequent_indent=_DETAIL_CONTINUATION,
            break_long_words=False,
            break_on_hyphens=False,
        )
    ]


def shape_warnings(
    *, in_use: list[str], description: str, topic: str | None, detail: str | None
) -> list[ShapeWarning]:
    """Return what is likely misplaced in an item that is otherwise well-formed.

    None of these refuses the write: a new topic may be deliberate, and length is advice.

    Args:
        in_use (list[str]): The topics on the backlog before this item, from `topics`.
        description (str): The item's description as given.
        topic (str | None): The item's topic as given, or None.
        detail (str | None): The item's detail as given, or None.

    Returns:
        list[ShapeWarning]: One per problem, empty when the item is well placed.
    """
    warnings: list[ShapeWarning] = []
    tag = _tag(topic) if topic is not None else ""
    if tag and in_use and tag not in in_use:
        warnings.append(
            ShapeWarning(f"#{tag} is a new topic in this backlog", f"in use: {', '.join(in_use)}")
        )
    length = len(description.strip())
    if length > LONG_DESCRIPTION:
        warnings.append(
            ShapeWarning(
                f"the description is {length} characters, longer than {LONG_DESCRIPTION}",
                "keep the line to one short sentence and move context into --detail",
            )
        )
    detail_length = len((detail or "").strip())
    if detail_length > LONG_DETAIL:
        warnings.append(
            ShapeWarning(
                f"the detail is {detail_length} characters, longer than {LONG_DETAIL}",
                "a design this size belongs in a spec",
            )
        )
    return warnings


def is_ticked(line: str) -> bool:
    """Return whether `line` is a ticked top-level bullet: finished work left in place.

    Args:
        line (str): One line of `backlog.md`.

    Returns:
        bool: True for a `- [x]` or `- [X]` line at column 0.
    """
    return line.startswith(_TICKED)


def is_item(line: str) -> bool:
    """Return whether `line` is an open item, the only lines the session start counts.

    Args:
        line (str): One line of `backlog.md`.

    Returns:
        bool: True for a bullet at column 0 with text after an optional empty `[ ]` box,
            whatever size, date, or topic it carries. False for a ticked `- [x]` line,
            an indented sub-bullet, an empty bullet, and anything that is not a bullet.
    """
    if not line.startswith("- ") or is_ticked(line):
        return False
    return bool(line.removeprefix("- ").removeprefix("[ ]").strip())


def topics(path: Path) -> list[str]:
    """Return the sorted distinct `[#topic]` tags on the item lines of a backlog.

    Args:
        path (Path): The project's `backlog.md`.

    Returns:
        list[str]: The tags without their hash, empty when the file is missing.
    """
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return sorted({tag for line in lines if is_item(line) for tag in _TOPIC_TAG.findall(line)})


def _insert(lines: list[str], kind: str, block: list[str]) -> list[str]:
    """Place `block` at the end of the `## kind` section, or open that section last."""
    heading = f"{_SECTION_PREFIX}{kind}"
    start = next((i for i, line in enumerate(lines) if line.rstrip() == heading), None)
    if start is None:
        return [*lines, "", heading, "", *block]
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith(_SECTION_PREFIX)),
        len(lines),
    )
    last = end
    while last > start + 1 and not lines[last - 1].strip():
        last -= 1
    if last == start + 1:
        return [*lines[:last], "", *block, *lines[last:]]
    return [*lines[:last], *block, *lines[last:]]


def add_item(
    path: Path,
    *,
    kind: str,
    size: str,
    description: str,
    topic: str | None,
    today: str,
    detail: str | None = None,
) -> list[str]:
    """Append one open item under its kind heading and return the lines written.

    A missing file starts with `# Backlog`. An existing file keeps whatever heads it,
    since the `# Backlog` heading is never inserted into a file someone else structured.

    Args:
        path (Path): The project's `backlog.md`.
        kind (str): One of `KINDS`.
        size (str): One of `SIZES`.
        description (str): The imperative description.
        topic (str | None): The tag, or None for none.
        today (str): The date as `YYYY-MM-DD`.
        detail (str | None): Context written as indented lines under the item, or None.

    Returns:
        list[str]: The item line followed by its detail lines, without newlines.

    Raises:
        BacklogError: A field cannot be written as a well-formed line.
    """
    block = [
        format_item(kind=kind, size=size, description=description, topic=topic, today=today),
        *format_detail(detail),
    ]
    lines = path.read_text(encoding="utf-8").rstrip().splitlines() if path.is_file() else [HEADING]
    atomic.write_text(path, "\n".join(_insert(lines, kind=kind, block=block)) + "\n")
    return block
