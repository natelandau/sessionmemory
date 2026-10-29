"""Record the local entries a person declined to adopt, so sync never offers them again.

A no to "Adopt <entry> into the vault?" has to outlive the run that asked it and reach
every checkout of the project, so it lives in the project's `local/` folder as
`declined.toml`. Only `local decline`, a no in `local sync`, and adopt write the file;
adopting an entry removes its name. A person may edit it by hand, so a file `read`
cannot parse cleanly is never rewritten.

Nothing prunes a name whose entry is gone from one checkout, since another checkout may
still hold it and pruning would bring the question back there.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from typing import TYPE_CHECKING

import tomli_w

from sessionmemory.lib import atomic, localmanifest

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

DECLINED_NAME = "declined.toml"
_HEADER = (
    "# Entries a person declined to adopt with local decline or local sync. Neither\n"
    "# local sync nor session start offers them again in any checkout. Remove a name\n"
    "# to be asked again.\n"
)


class DeclinedError(Exception):
    """A declined list that cannot be updated without losing what a person wrote in it."""

    def __init__(self, message: str, details: list[str] | None = None) -> None:
        """Create the error.

        Args:
            message (str): The one-line reason.
            details (list[str] | None): Follow-up lines for the reader.
        """
        super().__init__(message)
        self.message = message
        self.details = details or []


@dataclass(frozen=True)
class Declined:
    """The declined names a file lists, and what in it could not be used."""

    names: tuple[str, ...]
    problems: tuple[str, ...]


def read(local: Path) -> Declined:
    """Read the declined list in a project's `local/` folder, keeping every usable name.

    Args:
        local (Path): The project's `local/` folder.

    Returns:
        Declined: The valid names in file order, without repeats, and one problem line
            per thing that could not be used. A missing file lists nothing.
    """
    path = local / DECLINED_NAME
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Declined(names=(), problems=())
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as err:
        return Declined(names=(), problems=(f"the file cannot be read: {err}",))
    raw = data.get("names", [])
    if not isinstance(raw, list):
        return Declined(names=(), problems=("names must be a list of strings",))
    found: list[str] = []
    # Folded, since a case-insensitive file system makes `skills/A` the same entry.
    seen: set[str] = set()
    problems: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            problems.append(f"{item!r} is not a string")
        elif item.casefold() in seen:
            problems.append(f"{item} is listed twice")
        elif not localmanifest.is_kind_name(item):
            problems.append(f"{item} is not CLAUDE.local.md, skills/<name>, or rules/<name>")
        else:
            found.append(item)
            seen.add(item.casefold())
    return Declined(names=tuple(found), problems=tuple(problems))


def add(local: Path, names: Iterable[str]) -> list[str]:
    """Record declined names, keeping the list sorted.

    Args:
        local (Path): The project's `local/` folder.
        names (Iterable[str]): Entry names such as `skills/<name>`.

    Returns:
        list[str]: The names that were not listed before, in the order given.

    Raises:
        DeclinedError: When the file holds anything `read` reports as a problem, or a
            name is not one an adopt question can offer.
    """
    current = read(local)
    if current.problems:
        msg = f"{local / DECLINED_NAME} has problems, so nothing was added to it"
        raise DeclinedError(msg, [*current.problems, "Fix the file, then run this again."])
    wanted = list(dict.fromkeys(names))
    for name in wanted:
        if not localmanifest.is_kind_name(name):
            msg = f"{name} is not CLAUDE.local.md, skills/<name>, or rules/<name>"
            raise DeclinedError(msg)
    added = [name for name in wanted if name not in current.names]
    if added:
        _write(local, [*current.names, *added])
    return added


def remove(local: Path, name: str) -> bool:
    """Drop a declined name, as adopting the entry does.

    A file with problems, or one that does not list the name, is left as it is.

    Args:
        local (Path): The project's `local/` folder.
        name (str): The entry name to drop.

    Returns:
        bool: True when the name was dropped.
    """
    current = read(local)
    if current.problems or name not in current.names:
        return False
    _write(local, [n for n in current.names if n != name])
    return True


def _write(local: Path, names: list[str]) -> None:
    """Write the declined list holding `names`, sorted, under its explanatory header."""
    body = tomli_w.dumps({"names": sorted(set(names))})
    atomic.write_text(local / DECLINED_NAME, f"{_HEADER}{body}")
