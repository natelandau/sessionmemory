"""Read and write the YAML frontmatter block that heads every note.

Dates are normalized to ISO strings on the way in and quoted on the way out. PyYAML
would otherwise load `created: 2026-08-01` as a `datetime.date` and dump it back
unquoted, so a value's type would depend on whether the note had been round tripped.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from collections.abc import Mapping

DELIMITER = "---"
BOM = "\ufeff"


class FrontmatterError(ValueError):
    """Raised when a note's frontmatter is absent or malformed."""


class MissingFrontmatterError(FrontmatterError):
    """Raised when a note has no frontmatter block at all."""


def _stringify_dates(value: Any) -> Any:  # noqa: ANN401
    """Convert any date or datetime in a nested structure to an ISO string.

    Args:
        value (Any): A value that may contain dates at any depth.

    Returns:
        Any: The same structure with dates replaced by ISO strings.
    """
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _stringify_dates(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_stringify_dates(v) for v in value]
    return value


def _split(text: str) -> tuple[str, str]:
    """Return the raw YAML block and the body, tolerating a BOM and CRLF endings.

    Raises:
        FrontmatterError: If the block is missing or unterminated.
    """
    text = text.removeprefix(BOM).replace("\r\n", "\n")

    if not text.startswith(f"{DELIMITER}\n"):
        msg = "note has no frontmatter block"
        raise MissingFrontmatterError(msg)

    rest = text[len(DELIMITER) + 1 :]
    if rest.startswith(f"{DELIMITER}\n"):
        # An empty block's closing delimiter sits immediately after the opening
        # one, so the "\n---\n" search below never gets the leading newline it
        # needs: the opening strip already consumed it.
        raw_meta = ""
        body = rest[len(DELIMITER) + 1 :]
    elif rest == DELIMITER:
        # Same empty-block case, but the closing delimiter is also the end of
        # the file, so there is no body to slice off.
        raw_meta = ""
        body = ""
    else:
        end = rest.find(f"\n{DELIMITER}\n")
        if end == -1:
            if rest.endswith(f"\n{DELIMITER}"):
                # Closing delimiter is the last thing in the file, so there is no
                # trailing "\n" to anchor the usual end + len(DELIMITER) + 2 offset.
                raw_meta = rest[: -len(DELIMITER) - 1]
                body = ""
            else:
                msg = "frontmatter block was never closed"
                raise FrontmatterError(msg)
        else:
            raw_meta = rest[:end]
            body = rest[end + len(DELIMITER) + 2 :]
    return raw_meta, body


def _load(raw_meta: str) -> dict[str, Any]:
    """Load a raw YAML block as the mapping it must be, with values as YAML typed them.

    Raises:
        FrontmatterError: If the block is not valid YAML or not a mapping.
    """
    try:
        loaded = yaml.safe_load(raw_meta)
    except yaml.YAMLError as error:
        msg = f"frontmatter is not valid YAML: {error}"
        raise FrontmatterError(msg) from error
    loaded = {} if loaded is None else loaded
    if not isinstance(loaded, dict):
        msg = f"frontmatter must be a mapping, got {type(loaded).__name__}"
        raise FrontmatterError(msg)
    return loaded


def parse(text: str) -> tuple[dict[str, Any], str]:
    """Split a note into its frontmatter mapping and its markdown body.

    A leading byte order mark and Windows line endings are tolerated. Notes are
    hand-edited in whatever editor is at hand, and a note is still a note when it comes
    back with a BOM or CRLF endings, so refusing it would refuse a legible file.

    Args:
        text (str): The full contents of a note file.

    Returns:
        tuple[dict[str, Any], str]: The metadata mapping and the body.

    Raises:
        FrontmatterError: If the block is missing, unterminated, or not a mapping.
    """
    raw_meta, body = _split(text)
    return _stringify_dates(_load(raw_meta)), body


def unquoted_datetime_keys(text: str) -> list[str]:
    """Return the top-level keys whose bare values YAML typed as a date or datetime.

    The memoryfield spec requires quoting datetimes, since a YAML 1.1 parser types a bare
    one and a YAML 1.2 parser does not, so what the page says would depend on the reader.

    Args:
        text (str): The full contents of a note file.

    Returns:
        list[str]: The offending keys in file order, empty when every value is quoted.

    Raises:
        FrontmatterError: If the block is missing, unterminated, or not a mapping.
    """
    raw_meta, _body = _split(text)
    return [key for key, value in _load(raw_meta).items() if isinstance(value, datetime.date)]


def serialize(meta: Mapping[str, Any], body: str) -> str:
    """Render a metadata mapping and a body back into note file contents.

    Args:
        meta (Mapping[str, Any]): The metadata to write.
        body (str): The markdown body.

    Returns:
        str: The complete file contents, ending in a single newline.
    """
    dumped = yaml.safe_dump(
        dict(meta),
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=88,
    )
    return f"{DELIMITER}\n{dumped}{DELIMITER}\n{body.rstrip()}\n"
