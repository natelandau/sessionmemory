"""The map from a checkout on disk to a project slug in the vault.

Remotes are the primary key because they survive a repository being moved or cloned
again, which a filesystem path does not. Normalization strips everything that varies
between equivalent spellings of one URL: scheme, credentials, port, trailing `.git`,
and case. The port strip is anchored to the host segment (before the first `/`) so a
path that happens to contain a colon followed by digits is never mistaken for one.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import tomli_w

from sessionmemory.lib import atomic, ids
from sessionmemory.lib.paths import SYSTEM_DIR

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

REGISTRY_FILE = "registry.toml"

_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_SCP_LIKE = re.compile(r"^(?:[^@/]+@)?([^:/]+):(.+)$")
_CREDENTIALS = re.compile(r"^[^@/]+@")
_PORT = re.compile(r":\d+$")


class RegistryError(ValueError):
    """Raised when registry.toml cannot be read, whether from a syntax error or a malformed shape.

    A single exception for both failure modes lets callers report a hand-edited file's
    problems to the user without risking that a genuine bug elsewhere (which would
    raise some other exception type) gets misreported as "your file is broken".
    """


@dataclass(frozen=True)
class Project:
    """One registered project."""

    slug: str
    remotes: tuple[str, ...]
    root: str


def normalize_remote(url: str) -> str:
    """Reduce a git remote URL to a stable comparison key.

    Args:
        url (str): A remote URL in any of git's supported spellings.

    Returns:
        str: The normalized `host/path` key, lowercased and without a `.git` suffix.
    """
    candidate = url.strip()

    if _SCHEME.match(candidate):
        candidate = _SCHEME.sub("", candidate, count=1)
    else:
        scp = _SCP_LIKE.match(candidate)
        if scp:
            candidate = f"{scp.group(1)}/{scp.group(2)}"

    candidate = _CREDENTIALS.sub("", candidate, count=1)

    # Strip a port only from the host segment, so a path that happens to
    # contain ":digits" past the first slash is never mistaken for one.
    host, sep, rest = candidate.partition("/")
    host = _PORT.sub("", host, count=1)
    candidate = f"{host}{sep}{rest}"

    candidate = candidate.removesuffix(".git").strip("/")

    return candidate.lower()


def _string_tuple(entry: dict[str, object], key: str, slug: str) -> tuple[str, ...]:
    """Read an optional list-of-strings field from a raw registry entry.

    Args:
        entry (dict[str, object]): The raw table for one registry entry.
        key (str): The field name to read.
        slug (str): The entry's project slug, for error messages.

    Returns:
        tuple[str, ...]: The field's values, or an empty tuple when absent.

    Raises:
        RegistryError: If the field is present but is not a list of strings.
    """
    value = entry.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        msg = f"registry entry {slug!r} has a malformed {key!r} field"
        raise RegistryError(msg)
    return tuple(value)


def load(vault: Path) -> dict[str, Project]:
    """Read the registry.

    Args:
        vault (Path): The vault root.

    Returns:
        dict[str, Project]: Projects keyed by slug. Empty when the file is absent.

    Raises:
        RegistryError: If the file is not valid TOML, the top-level `projects` table
            is malformed, a registry entry is not a table, or one of its fields has
            the wrong type.
    """
    path = vault / SYSTEM_DIR / REGISTRY_FILE
    if not path.is_file():
        return {}

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        msg = f"registry is not valid TOML: {error}"
        raise RegistryError(msg) from error

    raw_projects = raw.get("projects", {})
    if not isinstance(raw_projects, dict):
        msg = f"registry 'projects' must be a table, got {type(raw_projects).__name__}"
        raise RegistryError(msg)

    projects: dict[str, Project] = {}
    for slug, entry in raw_projects.items():
        if not isinstance(entry, dict):
            msg = f"registry entry {slug!r} must be a table, got {type(entry).__name__}"
            raise RegistryError(msg)

        try:
            slug_shaped = slug == ids.slugify(slug)
        except ValueError:
            slug_shaped = False
        if not slug_shaped:
            # A slug names a directory under projects/, so one that survives no
            # slugification, or slugifies to something else, could otherwise walk
            # a project's files outside the vault.
            msg = f"registry entry {slug!r} is not a valid project slug"
            raise RegistryError(msg)

        root = entry.get("root", "")
        if not isinstance(root, str):
            msg = f"registry entry {slug!r} has a malformed 'root' field"
            raise RegistryError(msg)

        projects[slug] = Project(
            slug=slug,
            remotes=_string_tuple(entry, "remotes", slug),
            root=root,
        )
    return projects


def save(vault: Path, projects: Mapping[str, Project]) -> None:
    """Write the registry, replacing whatever was there.

    The write is atomic. A slug is permanent once notes carry it, and this file is the
    only record of which slug a project's existing notes were filed under, so a write
    interrupted partway through has to leave the previous mapping intact.

    Args:
        vault (Path): The vault root.
        projects (Mapping[str, Project]): Projects keyed by slug.
    """
    path = vault / SYSTEM_DIR / REGISTRY_FILE

    document = {
        "projects": {
            slug: {
                "remotes": list(project.remotes),
                "root": project.root,
            }
            for slug, project in sorted(projects.items())
        }
    }
    atomic.write_text(path, tomli_w.dumps(document))


def find_by_remote(projects: Mapping[str, Project], remotes: Iterable[str]) -> Project | None:
    """Return the project sharing any remote with `remotes`.

    Args:
        projects (Mapping[str, Project]): The loaded registry.
        remotes (Iterable[str]): Normalized remote keys from the current checkout.

    Returns:
        Project | None: The matching project, or None.
    """
    wanted = set(remotes)
    for project in projects.values():
        if wanted & set(project.remotes):
            return project
    return None


def find_by_root(projects: Mapping[str, Project], root: str) -> Project | None:
    """Return the project whose recorded repository root equals `root`.

    Both sides are resolved before comparison, so a symlinked path (`/tmp` on macOS,
    for instance) matches a registry entry regardless of which spelling either side
    happens to use. A stored root that is not absolute is skipped rather than resolved,
    since resolving it would measure it from wherever the process happens to be running,
    which would make the answer depend on the caller's working directory.

    Args:
        projects (Mapping[str, Project]): The loaded registry.
        root (str): An absolute repository root.

    Returns:
        Project | None: The matching project, or None.
    """
    if not root:
        return None

    target = Path(root).resolve()
    for project in projects.values():
        if not project.root or not Path(project.root).is_absolute():
            continue
        if Path(project.root).resolve() == target:
            return project
    return None


def find_by_path_prefix(projects: Mapping[str, Project], path: str) -> Project | None:
    """Return the registered project containing `path`, preferring the deepest root.

    This is what resolves a directory anywhere inside a project whose root is the only
    key it has, such as a project that is not a git repository. It is a broad key, and
    `resolve` documents the narrow conditions under which it applies one. Both sides are
    resolved before comparison for the same reason `find_by_root` does it, and a stored
    root that is not absolute is skipped rather than resolved, since resolving it would
    measure it from wherever the process happens to be running. The deepest matching root
    wins, so a project nested inside another resolves as the inner one. Ties, which only a
    hand-edited registry can produce, break on slug order to keep the answer stable across
    runs.

    Args:
        projects (Mapping[str, Project]): The loaded registry.
        path (str): An absolute directory.

    Returns:
        Project | None: The innermost project containing `path`, or None.
    """
    if not path:
        return None

    target = Path(path).resolve()
    best: Project | None = None
    best_depth = -1
    for project in sorted(projects.values(), key=lambda candidate: candidate.slug):
        if not project.root or not Path(project.root).is_absolute():
            continue
        root = Path(project.root).resolve()
        if not target.is_relative_to(root):
            continue
        depth = len(root.parts)
        if depth > best_depth:
            best = project
            best_depth = depth
    return best
