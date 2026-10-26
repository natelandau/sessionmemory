"""Record which paths a person adopted into a project's `local/` folder by naming them.

`CLAUDE.local.md`, skills, and rules sit at fixed depths, so the vault's folders alone say
where each entry starts. A path adopted by name has no fixed depth: once `.claude/agents`
is in the vault as `claude/agents/foo.md`, nothing there says whether the folder or the
one file was adopted. `entries.toml` in `local/` records each such path, so every
checkout links the same thing.

Only adopt writes the file, and only through `add`. A person may edit it by hand, so a
file `read` cannot parse cleanly is never rewritten.
"""

from __future__ import annotations

import functools
import tomllib
from dataclasses import dataclass
from typing import TYPE_CHECKING

import tomli_w

from sessionmemory.lib import atomic

if TYPE_CHECKING:
    from pathlib import Path

MANIFEST_NAME = "entries.toml"
LOCAL_MD_NAME = "CLAUDE.local.md"
_CHECKOUT_CLAUDE = ".claude"
# No leading dot, since Finder, Obsidian, and `ls` hide dot-folders in the vault.
_VAULT_CLAUDE = "claude"
_KIND_FOLDERS = ("skills", "rules")
# Claude Code refuses to write through a symlinked settings file.
_SETTINGS_FILES = ("settings.json", "settings.local.json")
_HEADER = (
    "# Paths adopted into this folder with local adopt, relative to the checkout's\n"
    "# top level. Each is linked into every checkout of the project.\n"
)


class ManifestError(Exception):
    """A manifest that cannot be updated without losing what a person wrote in it."""

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
class Manifest:
    """The adopted paths a manifest lists, and what in it could not be used."""

    paths: tuple[str, ...]
    problems: tuple[str, ...]


def is_kind_name(relpath: str) -> bool:
    """Report whether a name already means `CLAUDE.local.md`, a skill, or a rule.

    Args:
        relpath (str): The name or path to check.

    Returns:
        bool: True for `CLAUDE.local.md`, or two segments under `skills` or `rules`.
    """
    if relpath == LOCAL_MD_NAME:
        return True
    parts = relpath.split("/")
    return len(parts) == 2 and parts[0] in _KIND_FOLDERS and bool(parts[1])  # noqa: PLR2004


def name_problem(relpath: str) -> str | None:  # noqa: PLR0911
    """Name why a work-tree path cannot be an adopted path, or return None when it can.

    Args:
        relpath (str): A path relative to the work tree's top level, with `/` separators.

    Returns:
        str | None: A one-line reason, or None.
    """
    parts = relpath.split("/")
    if not relpath or relpath.startswith("/") or any(p in {"", ".", ".."} for p in parts):
        return f"{relpath!r} is not a plain path inside the checkout"
    # Folded, since a case-insensitive file system makes `.Claude` the same folder.
    folded = relpath.casefold().split("/")
    if folded[0] == ".git":
        return f"{relpath} is inside git's own folder"
    if folded[0] == _CHECKOUT_CLAUDE:
        if len(folded) == 1:
            return f"{relpath} holds entries of its own; adopt what is inside it"
        if folded[1] in _KIND_FOLDERS:
            return f"{relpath} is a skill or rule, or inside one; adopt the entry by its name"
        if len(folded) == 2 and folded[1] in _SETTINGS_FILES:  # noqa: PLR2004
            return f"{relpath} is a settings file, which Claude Code cannot write through a link"
    if folded[0] == _VAULT_CLAUDE:
        return f"{relpath} would share the vault folder that holds .claude"
    # `settings.local.json` alone is the name of the settings entry.
    if "/".join(folded) in {MANIFEST_NAME, LOCAL_MD_NAME.casefold(), _SETTINGS_FILES[1]}:
        return f"{relpath} would share a path the vault already uses"
    if is_kind_name("/".join(folded)):
        return f"{relpath} reads as the name of a skill or rule in .claude"
    return None


def outer_of(relpath: str, listed: tuple[str, ...] | list[str]) -> str | None:
    """Return a listed path that holds `relpath` or that `relpath` holds, ignoring case.

    Args:
        relpath (str): The path to compare.
        listed (tuple[str, ...] | list[str]): The paths to compare it with.

    Returns:
        str | None: The first listed path nested with `relpath`, or None.
    """
    folded = relpath.casefold()
    for other in listed:
        theirs = other.casefold()
        if theirs != folded and (
            folded.startswith(f"{theirs}/") or theirs.startswith(f"{folded}/")
        ):
            return other
    return None


def vault_relpath(relpath: str) -> str:
    """Map a checkout path to its path under `local/`.

    Args:
        relpath (str): A path relative to the work tree's top level.

    Returns:
        str: The same path, with a leading `.claude/` stored as `claude/`.
    """
    head, sep, rest = relpath.partition("/")
    # Folded like `name_problem`, so `.Claude/x` is never kept as a hidden vault folder.
    if head.casefold() == _CHECKOUT_CLAUDE and sep:
        return f"{_VAULT_CLAUDE}/{rest}"
    return relpath


def read(local: Path) -> Manifest:
    """Read the manifest in a project's `local/` folder, keeping every usable path.

    Args:
        local (Path): The project's `local/` folder.

    Returns:
        Manifest: The valid paths in file order, without repeats, and one problem line
            per thing that could not be used. A missing file lists nothing.
    """
    path = local / MANIFEST_NAME
    try:
        stat = path.stat()
    except FileNotFoundError:
        return Manifest(paths=(), problems=())
    except OSError as err:
        return Manifest(paths=(), problems=(f"the file cannot be read: {err}",))
    return _parse(path, stat.st_ino, stat.st_mtime_ns, stat.st_size)


# Keyed on the file's identity, so one run parses an unchanged manifest once and sees any
# write at once: `add` replaces the file, which changes its inode.
@functools.lru_cache(maxsize=16)
def _parse(path: Path, _ino: int, _mtime_ns: int, _size: int) -> Manifest:
    """Parse a manifest file into its usable paths and its problems."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Manifest(paths=(), problems=())
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as err:
        return Manifest(paths=(), problems=(f"the file cannot be read: {err}",))
    raw = data.get("paths", [])
    if not isinstance(raw, list):
        return Manifest(paths=(), problems=("paths must be a list of strings",))
    found: list[str] = []
    seen: set[str] = set()
    problems: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            problems.append(f"{item!r} is not a string")
        elif item.casefold() in seen:
            problems.append(f"{item} is listed twice")
        elif (reason := name_problem(item)) is not None:
            problems.append(reason)
        else:
            found.append(item)
            seen.add(item.casefold())
    # Of two nested paths only the outer one can be linked, so the inner one is dropped.
    holders = {
        inner: outer
        for inner in found
        for outer in found
        if inner.casefold().startswith(f"{outer.casefold()}/")
    }
    problems.extend(
        f"{inner} is inside {outer}, which is also listed" for inner, outer in holders.items()
    )
    return Manifest(paths=tuple(p for p in found if p not in holders), problems=tuple(problems))


def add(local: Path, relpath: str) -> bool:
    """List an adopted path in the manifest, keeping the list sorted.

    Args:
        local (Path): The project's `local/` folder.
        relpath (str): The checkout path to list.

    Returns:
        bool: True when it was added, False when it was already listed.

    Raises:
        ManifestError: When the manifest holds anything `read` reports as a problem.
    """
    current = read(local)
    if current.problems:
        msg = f"{local / MANIFEST_NAME} has problems, so {relpath} was not added to it"
        raise ManifestError(msg, [*current.problems, "Fix the file, then run this again."])
    if relpath in current.paths:
        return False
    other = same_but_case(relpath, current.paths)
    if other is not None:
        msg = f"{relpath} differs only in case from {other}, which is already listed"
        raise ManifestError(msg, [f"Adopt it as {other}."])
    _write(local, [*current.paths, relpath])
    return True


def discard(local: Path, relpath: str) -> None:
    """Drop a path from the manifest, as an adopt that failed after listing it does.

    A manifest with problems, or one that does not list the path, is left as it is.

    Args:
        local (Path): The project's `local/` folder.
        relpath (str): The checkout path to drop.
    """
    current = read(local)
    if current.problems or relpath not in current.paths:
        return
    _write(local, [p for p in current.paths if p != relpath])


def same_but_case(relpath: str, listed: tuple[str, ...] | list[str]) -> str | None:
    """Return a listed path spelled like `relpath` but for case, which `read` counts as a repeat.

    Args:
        relpath (str): The path to compare.
        listed (tuple[str, ...] | list[str]): The paths to compare it with.

    Returns:
        str | None: The first listed path equal to `relpath` ignoring case only, or None.
    """
    folded = relpath.casefold()
    return next((o for o in listed if o != relpath and o.casefold() == folded), None)


def _write(local: Path, listed: list[str]) -> None:
    """Write the manifest holding `listed`, sorted, under its explanatory header."""
    body = tomli_w.dumps({"paths": sorted(set(listed))})
    atomic.write_text(local / MANIFEST_NAME, f"{_HEADER}{body}")
