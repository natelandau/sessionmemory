"""Read and rewrite the commit list in a session log's `## Changes` section.

The sweep writes the section from `git log --format="%h %s"`, one `<sha> <subject>` line
per commit, and the sweep model sometimes bullets the lines. This module is pure: it
takes a body and returns a body, and never touches anything outside the section. Lines
inside the section that are not commit lines are kept byte for byte.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

CHANGE_LINE = re.compile(
    r"^(?P<prefix>\s*(?:[-*]\s+)?)(?P<sha>[0-9a-f]{7,40})\s+(?P<subject>\S.*)$"
)
_SECTION_HEADING = "## Changes"
_HEADING_PREFIX = "## "


@dataclass(frozen=True)
class ChangeLine:
    """One commit line from the Changes section.

    Attributes:
        prefix (str): Leading whitespace and bullet marker, empty for a bare line.
        sha (str): The abbreviated or full commit hash.
        subject (str): The commit subject.
    """

    prefix: str
    sha: str
    subject: str


def _match(line: str) -> re.Match[str] | None:
    """Match a commit line, ignoring its line ending."""
    return CHANGE_LINE.match(line.rstrip("\r\n"))


def _ending(line: str) -> str:
    """Return the line ending of a line, or an empty string for the last line."""
    return line[len(line.rstrip("\r\n")) :]


def _in_section(lines: list[str]) -> list[bool]:
    """Flag which lines sit inside the Changes section, excluding its heading."""
    flags: list[bool] = []
    inside = False
    for line in lines:
        if line.rstrip() == _SECTION_HEADING:
            inside = True
            flags.append(False)
        elif inside and line.startswith(_HEADING_PREFIX):
            inside = False
            flags.append(False)
        else:
            flags.append(inside)
    return flags


def parse(body: str) -> list[ChangeLine]:
    """Return the commit lines in the `## Changes` section, in order.

    Args:
        body (str): The page body.

    Returns:
        list[ChangeLine]: The commit lines, or an empty list when there is no section.
    """
    lines = body.splitlines(keepends=True)
    found: list[ChangeLine] = []
    for line, inside in zip(lines, _in_section(lines), strict=True):
        match = _match(line) if inside else None
        if match:
            found.append(
                ChangeLine(prefix=match["prefix"], sha=match["sha"], subject=match["subject"])
            )
    return found


def rewrite(body: str, outcome: Mapping[str, str | None]) -> str:
    """Replace or drop commit lines in the `## Changes` section.

    A replacement takes the replaced line's prefix. A replacement whose sha is already
    listed earlier in the section is dropped, which collapses several branch commits into
    one line and avoids duplicating a commit that is already listed.

    Args:
        body (str): The page body.
        outcome (Mapping[str, str | None]): Maps an original sha to a replacement
            `<sha> <subject>`, or to None to drop the line. A sha not in the mapping is kept.

    Returns:
        str: The rewritten body, or `body` itself when nothing changes.
    """
    lines = body.splitlines(keepends=True)
    result: list[str] = []
    seen: set[str] = set()
    added: set[str] = set()
    for line, inside in zip(lines, _in_section(lines), strict=True):
        match = _match(line) if inside else None
        if not match:
            result.append(line)
            continue
        sha = match["sha"]
        if sha not in outcome:
            if sha not in added:
                seen.add(sha)
                result.append(line)
            continue
        replacement = outcome[sha]
        if replacement is None:
            continue
        new_sha = replacement.split(maxsplit=1)[0]
        if new_sha in seen:
            continue
        seen.add(new_sha)
        added.add(new_sha)
        result.append(f"{match['prefix']}{replacement}{_ending(line)}")
    rewritten = "".join(result)
    return body if rewritten == body else rewritten
