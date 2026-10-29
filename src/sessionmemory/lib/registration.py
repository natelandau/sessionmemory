"""Record a directory as a project in the vault registry.

Registration runs in two steps so a caller can choose the slug in between: `candidate`
asks git what the directory is and refuses anything that cannot be registered, and
`record` files it under a slug while holding the registry lock. A slug is permanent once
notes carry it, so every refusal here exists to keep a wrong one from being recorded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sessionmemory.lib import paths, registry
from sessionmemory.lib.gitinfo import git_context
from sessionmemory.lib.ids import slugify

if TYPE_CHECKING:
    from pathlib import Path


class RegistrationError(Exception):
    """A directory or slug that registration refuses, with the lines that explain it."""

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
class Candidate:
    """A directory git has vouched for as registrable, keyed as it would be resolved."""

    root: Path
    remotes: tuple[str, ...]


@dataclass(frozen=True)
class Registered:
    """The slug a directory was filed under, and the project it sits inside, if any."""

    slug: str
    enclosing: registry.Project | None


def candidate(target: Path) -> Candidate:
    """Ask git what `target` is, refusing a directory that cannot be registered.

    Args:
        target (Path): The directory to register.

    Returns:
        Candidate: The root and remotes the directory would be filed under.

    Raises:
        RegistrationError: When the target is not an existing directory, git could not
            tell whether it is a repository, or it is a bare repository.
    """
    # A mistyped --cwd is the one accident registration must not accept.
    if not target.is_dir():
        msg = f"{target} is not a directory"
        raise RegistrationError(msg, ["--cwd takes a directory that exists"])

    context = git_context(target)

    # An entry recorded while git was unavailable stays wrong once git works again: a
    # repository would be filed as a plain directory, with no remote and the wrong root.
    if not context.git_answered:
        msg = f"cannot tell whether {target} is a git repository"
        raise RegistrationError(
            msg,
            [
                "git did not answer: it may be missing, or it may refuse this directory",
                "fix that first, since a slug is permanent once notes carry it",
            ],
        )

    # A bare repository has no working tree, so no working directory belongs to it and
    # nothing filed under it could ever be resolved back.
    if context.is_bare:
        msg = f"{target} is a bare repository"
        raise RegistrationError(msg, ["register a working checkout instead"])

    # A directory outside git has no remote and no repository root, so the directory
    # itself is the only key it can be registered and resolved under.
    return Candidate(root=context.repo_root or target, remotes=context.remotes)


def as_slug(text: str) -> str | None:
    """Return `text` slugified, or None when nothing in it survives.

    Args:
        text (str): Any name.

    Returns:
        str | None: The slug, which equals `text` only when `text` is already one.
    """
    try:
        return slugify(text)
    except ValueError:
        return None


def check_slug(slug: str) -> None:
    """Refuse an explicit slug that is not already the form the vault files projects under.

    An explicit value is rejected rather than quietly corrected: a caller that asked
    for one slug must not silently end up with another, and the slug becomes a
    directory name under `projects/`, so anything outside the slug alphabet is either a
    path escape or a name that will not match its own notes.

    Args:
        slug (str): The slug the caller asked for.

    Raises:
        RegistrationError: When the value is blank or is not its own slugification.
    """
    if not slug.strip():
        msg = "--slug must not be empty"
        raise RegistrationError(msg)
    normalized = as_slug(slug)
    if normalized != slug:
        msg = "--slug must be lowercase letters, digits, and hyphens"
        raise RegistrationError(
            msg,
            [f"try: --slug {normalized}"] if normalized else [],
        )


def derive_slug(found: Candidate) -> str:
    """Slugify the best name available for a project the caller did not name.

    A remote or directory name has no caller to surprise, so unlike an explicit slug it
    is slugified: `My.Repo` becomes `my-repo`. A remote is preferred over the directory
    name because a checkout can be renamed or cloned into a differently named directory
    while the remote stays put, and the directory name is the only source a project
    outside git has.

    Args:
        found (Candidate): The directory being registered.

    Returns:
        str: The slug to register under.

    Raises:
        RegistrationError: When neither the remote nor the directory name yields a slug.
    """
    remote_names = (remote.rstrip("/").rsplit("/", 1)[-1] for remote in found.remotes)
    for name in (next(filter(None, remote_names), None), found.root.name):
        slug = as_slug(name) if name else None
        if slug is not None:
            return slug
    msg = f"cannot derive a slug from {found.root.name or str(found.root)!r}"
    raise RegistrationError(
        msg,
        ["name it explicitly, for example: --slug my-project"],
    )


def existing(projects: dict[str, registry.Project], found: Candidate) -> registry.Project | None:
    """Return the project `found` is already registered as, if any.

    Args:
        projects (dict[str, registry.Project]): The loaded registry.
        found (Candidate): The directory being registered.

    Returns:
        registry.Project | None: The matching entry, or None.
    """
    return registry.find_by_remote(projects, found.remotes) or registry.find_by_root(
        projects, str(found.root)
    )


def record(vault: Path, found: Candidate, slug: str | None) -> Registered:
    """File `found` in the registry and create the project's folders.

    Args:
        vault (Path): The vault root.
        found (Candidate): The directory to register.
        slug (str | None): The slug to use, or None to derive one.

    Returns:
        Registered: The slug used and any project the directory sits inside.

    Raises:
        RegistrationError: When the directory is already registered or the slug is taken.
    """
    # Held from load to save, so a registration racing this one cannot be lost.
    with registry.locked(vault):
        projects = registry.load(vault)
        already = existing(projects, found)
        if already is not None:
            msg = f"already registered as {already.slug!r}"
            raise RegistrationError(msg)

        chosen = slug if slug is not None else derive_slug(found)
        if chosen in projects:
            msg = f"slug {chosen!r} is already in use; pass --slug"
            raise RegistrationError(msg)

        # Read before the new entry is inserted, or it would match itself.
        enclosing = registry.find_by_path_prefix(projects, str(found.root))
        projects[chosen] = registry.Project(
            slug=chosen, remotes=found.remotes, root=str(found.root)
        )
        registry.save(vault, projects)

    # The folder is created here rather than on the first page write because the
    # plugin's sweep runs inside it, and a project's first sweep is what writes its
    # first page.
    for name in paths.FIELD_DIRS:
        (paths.project_dir(vault, chosen) / name).mkdir(parents=True, exist_ok=True)
    return Registered(slug=chosen, enclosing=enclosing)
