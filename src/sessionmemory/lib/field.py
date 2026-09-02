"""A page in a field: a markdown file with optional YAML frontmatter in a flat directory.

This is the one place a page's shape is known. The filename rule, the debris rule, and
the 8KB soft limit come from the memoryfield spec, and a page without frontmatter is a
valid page there, so `read_page` never refuses one.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from itertools import islice
from typing import TYPE_CHECKING, Any

from sessionmemory.lib import atomic
from sessionmemory.lib.frontmatter import FrontmatterError, parse, serialize
from sessionmemory.lib.ids import id_candidates, slugify

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

PAGE_LIMIT = 8192

_PAGE_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.md$")
_DEBRIS_NAMES = frozenset({".DS_Store", "desktop.ini", "Thumbs.db"})
_MAX_CLAIM_ATTEMPTS = 1000


class PageError(ValueError):
    """Raised when a page cannot be created as asked."""


@dataclass(frozen=True)
class Page:
    """One page as read from disk."""

    path: Path
    meta: dict[str, Any]
    body: str

    def _text(self, key: str) -> str:
        value = self.meta.get(key)
        return value if isinstance(value, str) else ""

    @property
    def title(self) -> str:
        """The page's title, or empty."""
        return self._text("title")

    @property
    def uuid(self) -> str:
        """The page's uuid, or empty."""
        return self._text("uuid")

    @property
    def summary(self) -> str:
        """The page's one-line summary, or empty."""
        return self._text("summary")

    @property
    def created(self) -> str:
        """The page's creation datetime, or empty."""
        return self._text("created")

    @property
    def updated(self) -> str:
        """The page's last-updated datetime, or empty."""
        return self._text("updated")

    @property
    def size(self) -> int:
        """The file's size in bytes."""
        return self.path.stat().st_size


def is_page_name(name: str) -> bool:
    """Report whether a filename conforms to the spec's page filename rule."""
    return _PAGE_NAME.match(name) is not None


def is_debris(name: str) -> bool:
    """Report whether a filename is sync, editor, or OS debris the spec says to ignore."""
    return name in _DEBRIS_NAMES or ".sync-conflict-" in name or name.endswith("~")


def iter_pages(directory: Path) -> list[Path]:
    """List the pages in one field, sorted by name.

    Only conformant names at the top level count; the spec forbids indexing pages in
    sub-directories, and a non-conformant name is reported by `doctor` rather than read.
    """
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and not is_debris(path.name) and is_page_name(path.name)
    )


def read_page(path: Path) -> Page:
    """Read a page, tolerating a missing frontmatter block or invalid UTF-8."""
    text = path.read_bytes().decode("utf-8", errors="replace")
    try:
        meta, body = parse(text)
    except FrontmatterError:
        return Page(path=path, meta={}, body=text)
    return Page(path=path, meta=meta, body=body)


def _already_says(path: Path, meta: Mapping[str, Any], body: str) -> bool:
    try:
        current_meta, current_body = parse(path.read_text(encoding="utf-8"))
    except (OSError, FrontmatterError, UnicodeDecodeError):
        return False
    return current_meta == dict(meta) and current_body.rstrip() == body.rstrip()


def write_page(path: Path, meta: Mapping[str, Any], body: str) -> None:
    """Write a page atomically, leaving a file that already says this untouched.

    A formatter restyles frontmatter quoting and spacing; comparing parsed content rather
    than bytes keeps this CLI and a formatter from trading edits forever.
    """
    if _already_says(path, meta, body):
        return
    atomic.write_text(path, serialize(meta, body))


def claim_filename(directory: Path, title: str, *, stem: str | None = None) -> Path:
    """Create an empty file for a new page, taking the first free name.

    Exclusive creation is the gate, so two writers racing for one title get two files.

    Raises:
        PageError: If the title yields no slug or no name is free.
    """
    if stem is None:
        try:
            stem = slugify(title)
        except ValueError as error:
            raise PageError(str(error)) from error
    taken = {path.stem for path in iter_pages(directory)}
    for candidate in islice(id_candidates(stem, taken), _MAX_CLAIM_ATTEMPTS):
        path = directory / f"{candidate}.md"
        if atomic.claim(path):
            return path
    msg = f"no free filename for {stem!r} in {directory}"
    raise PageError(msg)


def _create(directory: Path, meta: dict[str, Any], body: str, *, stem: str | None) -> Path:
    path = claim_filename(directory, str(meta["title"]), stem=stem)
    try:
        atomic.write_text(path, serialize(meta, body))
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def new_page(directory: Path, *, title: str, summary: str, body: str, now: str) -> Path:
    """Create a memory page with the five fields the spec defines."""
    meta = {
        "title": title,
        "uuid": str(uuid.uuid4()),
        "summary": summary,
        "created": now,
        "updated": now,
    }
    return _create(directory, meta, body, stem=None)


def new_document(
    directory: Path, *, title: str, body: str, now: str, stem: str | None = None
) -> Path:
    """Create a spec, plan, or log: a titled, dated file that is not a memory page."""
    meta = {"title": title, "created": now, "updated": now}
    return _create(directory, meta, body, stem=stem)
