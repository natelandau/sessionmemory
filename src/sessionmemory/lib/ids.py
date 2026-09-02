"""Turn a title into a filename stem.

A stem only has to be unique inside the flat directory it is written to. The slug
alphabet is the memoryfield spec's: lowercase ASCII letters, digits, and hyphens.
"""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Container, Iterator

MAX_ID_LENGTH = 72

_NON_SLUG = re.compile(r"[^a-z0-9]+")

# What a leading date costs an id, reserved out of the length budget so a dated id
# still fits MAX_ID_LENGTH.
DATE_PREFIX_WIDTH = len("2026-08-18-")


def slugify(title: str, *, max_length: int = MAX_ID_LENGTH) -> str:
    """Reduce a title to a lowercase, hyphenated, ascii slug.

    Args:
        title (str): The note title.
        max_length (int): The longest slug to return. A caller whose id will carry a
            prefix passes a smaller budget so the finished id still fits the limit.

    Returns:
        str: The slug.

    Raises:
        ValueError: If the title contains no characters that survive slugification.
    """
    folded = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    slug = _NON_SLUG.sub("-", folded.lower()).strip("-")

    if not slug:
        msg = f"title {title!r} has no usable characters for an id"
        raise ValueError(msg)

    if len(slug) > max_length:
        slug = slug[:max_length].rsplit("-", 1)[0].strip("-")

    return slug


def strip_date(title: str, created: str) -> str:
    """Remove `created` from `title`, which the filename is about to supply anyway.

    Only a date equal to the note's own creation date is removed, because the problem is
    redundancy rather than discovery: matching any date-shaped run of digits would eat a
    version number or a date the title is genuinely about. The title itself is never
    rewritten, only the stem derived from it, so a wrong guess costs an odd filename and
    not a renamed note.

    Args:
        title (str): The note title.
        created (str): The note's creation date as an ISO string.

    Returns:
        str: The title with the date removed and its whitespace collapsed, or the title
            unchanged when removing the date would leave nothing to slugify.
    """
    stripped = " ".join(title.replace(created, " ").split())
    return stripped or title


def id_candidates(base: str, taken: Container[str]) -> Iterator[str]:
    """Yield `base`, then `base-2`, `base-3`, ..., skipping anything `taken` reports as claimed.

    `base` is expected to already be `slugify` output; its length guarantee (at most
    `MAX_ID_LENGTH`) is what keeps the first candidate within the limit. `taken` is a
    filter to skip likely collisions, not the final word on which candidate is free: a
    caller with a stronger source of truth, such as an exclusive filesystem claim, is
    expected to test each yielded candidate itself and keep pulling from this generator
    on rejection.

    Args:
        base (str): The desired slug.
        taken (Container[str]): Ids known to be in use, case folded. Every candidate
            tested against it is lowercase, since `base` is a slug.

    Yields:
        str: Each untried candidate, in order, without limit.
    """
    if base not in taken:
        yield base

    counter = 2
    while True:
        suffix = f"-{counter}"
        trimmed = base[: MAX_ID_LENGTH - len(suffix)].strip("-")
        candidate = f"{trimmed}{suffix}"
        if candidate not in taken:
            yield candidate
        counter += 1
