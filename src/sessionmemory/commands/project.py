"""Commands that answer and record which project a directory belongs to."""

from __future__ import annotations

from pathlib import Path

import typer
from nclutils import pp

from sessionmemory.commands._common import (
    emit_json,
    fail,
    report_malformed_registry,
    require_vault,
)
from sessionmemory.lib import paths as paths_lib
from sessionmemory.lib import registry
from sessionmemory.lib.gitinfo import git_context
from sessionmemory.lib.ids import slugify
from sessionmemory.lib.resolve import resolve as resolve_project

CWD_OPTION = typer.Option(None, "--cwd", help="Directory to resolve. Defaults to the shell's.")
JSON_OPTION = typer.Option(False, "--json", help="Emit JSON instead of prose.")  # noqa: FBT003
SLUG_OPTION = typer.Option(None, "--slug", help="Override the derived slug.")
REGISTER_OPTION = typer.Option(
    False,  # noqa: FBT003
    "--register",
    help="Create this directory's registry entry.",
)


def _target(cwd: Path | None) -> Path:
    """Return the directory to operate on.

    Args:
        cwd (Path | None): An explicit directory, or None for the current one.

    Returns:
        Path: The resolved directory.
    """
    return (cwd or Path.cwd()).resolve()


def _slug_from_remotes(remotes: tuple[str, ...]) -> str | None:
    """Return the repository name from the first normalized remote.

    The remote is preferred over the directory name because a checkout can be renamed
    or cloned into a differently named directory while the remote stays put.

    Args:
        remotes (tuple[str, ...]): Normalized remote keys, as `host/owner/name`.

    Returns:
        str | None: The repository name, or None when there are no remotes.
    """
    for remote in remotes:
        name = remote.rstrip("/").rsplit("/", 1)[-1]
        if name:
            return name
    return None


def _derived_slug(remotes: tuple[str, ...], root: Path) -> str:
    """Slugify the best name available for a project the caller did not name.

    A derived slug has to satisfy the same rule an explicit `--slug` must already meet,
    since either becomes a directory name under `projects/`. Explicit input is refused
    rather than corrected because a caller must not silently get a slug it did not ask
    for, but a remote or directory name has no caller to surprise, so it is slugified:
    `My.Repo` becomes `my-repo`. A remote is preferred over the directory name because a
    checkout can be renamed while the remote stays put, and the directory name is the
    only source a project outside git has.

    Args:
        remotes (tuple[str, ...]): Normalized remote keys, as `host/owner/name`.
        root (Path): The project root being registered.

    Returns:
        str: The slug to register under.

    Raises:
        Exit: When neither the remote nor the directory name yields a slug.
    """
    for candidate in (_slug_from_remotes(remotes), root.name):
        if not candidate:
            continue
        try:
            return slugify(candidate)
        except ValueError:
            continue

    pp.error(
        f"cannot derive a slug from {root.name or str(root)!r}",
        details=["name it explicitly, for example: --slug my-project"],
    )
    raise typer.Exit(1)


def _require_slug_form(slug: str) -> None:
    """Refuse a `--slug` that is not already the form the vault files projects under.

    An explicit value is rejected rather than quietly corrected: a caller that asked
    for one slug must not silently end up with another, and the slug becomes a
    directory name under `projects/`, so anything outside the slug alphabet is either a
    path escape or a name that will not match its own notes.

    Args:
        slug (str): The value passed to `--slug`.

    Raises:
        Exit: When the value is blank or is not its own slugification.
    """
    if not slug.strip():
        pp.error("--slug must not be empty")
        raise typer.Exit(1)

    try:
        normalized: str | None = slugify(slug)
    except ValueError:
        normalized = None

    if normalized != slug:
        pp.error(
            "--slug must be lowercase letters, digits, and hyphens",
            details=[f"try: --slug {normalized}"] if normalized else [],
        )
        raise typer.Exit(1)


def _entry_root(vault: Path, slug: str, fallback: Path) -> str:
    """Return the root the registry records for `slug`.

    A directory outside git resolves by longest path prefix, so the directory asked
    about is routinely several levels below the one the entry was registered at, and
    reporting the former would name a root the registry does not hold.

    Args:
        vault (Path): The vault root.
        slug (str): The project the entry belongs to.
        fallback (Path): The directory asked about, for an entry that has since gone.

    Returns:
        str: The recorded root.
    """
    entry = registry.load(vault).get(slug)
    return entry.root if entry else str(fallback)


def _report(vault: Path, target: Path, action: str | None, *, as_json: bool) -> None:
    """Print the entry as it now stands, and exit non-zero when there is none.

    This is the command's only stdout in either mode, so a write is reported by showing
    its result rather than by a line of its own.

    Args:
        vault (Path): The vault root.
        target (Path): The directory to report on.
        action (str | None): The verb naming a write that just happened, or None for a
            plain view.
        as_json (bool): Emit the payload instead of prose.

    Raises:
        Exit: With code 1 when the directory is not registered.
    """
    try:
        result = resolve_project(vault, target)
    except registry.RegistryError as error:
        report_malformed_registry(vault, error)

    project_paths = (
        paths_lib.project_paths(vault, result.slug) if result.registered and result.slug else {}
    )

    payload = {
        "slug": result.slug,
        "registered": result.registered,
        "repo_root": str(result.repo_root) if result.repo_root else None,
        "is_worktree": result.is_worktree,
        "project_dir": (
            str(paths_lib.project_dir(vault, result.slug))
            if result.registered and result.slug
            else None
        ),
        "paths": project_paths,
    }

    if as_json:
        emit_json(payload)
    elif result.registered:
        headline = f"{action} {result.slug!r}" if action else result.slug or ""
        root = _entry_root(vault, result.slug, target) if result.slug else str(target)
        pp.success(headline, details=[f"root: {root}"])
    else:
        pp.error(
            f"{target} is not a registered project",
            details=["run: sessionmemory project --register"],
        )

    if not result.registered:
        raise typer.Exit(1)


def _register(
    vault: Path,
    target: Path,
    slug: str | None,
) -> None:
    """Record this directory in the vault registry.

    Args:
        vault (Path): The vault root.
        target (Path): The directory to register.
        slug (str | None): An explicit slug, or None to derive one.

    Raises:
        Exit: When the target is not an existing directory, when git could not determine
            whether it is a repository, when it is a bare repository, when it is already
            registered, when its slug is already in use, or when no slug can be derived
            from its remote or its directory name.
    """
    # A slug is permanent and its notes have to be moved by hand, so a mistyped --cwd is
    # the one accident registration must not accept.
    if not target.is_dir():
        pp.error(
            f"{target} is not a directory",
            details=["--cwd takes a directory that exists"],
        )
        raise typer.Exit(1)

    context = git_context(target)

    # An entry recorded while git was unavailable stays wrong once git works again: a
    # repository would be filed as a plain directory, with no remote and the wrong root.
    if not context.git_answered:
        pp.error(
            f"cannot tell whether {target} is a git repository",
            details=[
                "git did not answer: it may be missing, or it may refuse this directory",
                "fix that first, since a slug is permanent once notes carry it",
            ],
        )
        raise typer.Exit(1)

    # A bare repository has no working tree, so no working directory belongs to it and
    # nothing filed under it could ever be resolved back.
    if context.is_bare:
        pp.error(
            f"{target} is a bare repository",
            details=["register a working checkout instead"],
        )
        raise typer.Exit(1)

    # A directory outside git has no remote and no repository root, so the directory
    # itself is the only key it can be registered and resolved under.
    root = context.repo_root or target

    if slug is not None:
        _require_slug_form(slug)

    try:
        projects = registry.load(vault)
    except registry.RegistryError as error:
        report_malformed_registry(vault, error)

    existing = registry.find_by_remote(projects, context.remotes) or registry.find_by_root(
        projects, str(root)
    )
    if existing:
        pp.error(f"already registered as {existing.slug!r}")
        raise typer.Exit(1)

    resolved_slug = slug if slug is not None else _derived_slug(context.remotes, root)
    if resolved_slug in projects:
        pp.error(f"slug {resolved_slug!r} is already in use; pass --slug")
        raise typer.Exit(1)

    # Read before the new entry is inserted, or it would match itself.
    enclosing = registry.find_by_path_prefix(projects, str(root))

    projects[resolved_slug] = registry.Project(
        slug=resolved_slug,
        remotes=context.remotes,
        root=str(root),
    )
    registry.save(vault, projects)

    # The folder is created here rather than on the first page write because the
    # plugin's sweep runs inside it, and a project's first sweep is what writes its
    # first page.
    for field in paths_lib.FIELD_DIRS:
        (paths_lib.project_dir(vault, resolved_slug) / field).mkdir(parents=True, exist_ok=True)

    # Registering inside another project is legitimate, but an accidental one looks
    # exactly like a deliberate one, so the nesting is reported rather than assumed.
    if enclosing is not None:
        pp.warning(
            f"{root} sits inside project {enclosing.slug!r}",
            details=[
                f"{resolved_slug!r} now wins for paths under {root}",
                f"remove it from the registry if you meant to use {enclosing.slug!r}",
            ],
        )


def project_command(
    cwd: Path | None = CWD_OPTION,
    slug: str | None = SLUG_OPTION,
    *,
    register: bool = REGISTER_OPTION,
    as_json: bool = JSON_OPTION,
) -> None:
    """Report this directory's project, or create its registry entry.

    Raises:
        Exit: When `--slug` is given without `--register`, when `--register` names a
            directory that does not exist, is a bare repository, or is already
            registered, or with code 1 when the directory has no project.
    """
    if slug is not None and not register:
        fail("--slug applies only with --register", ["a slug is permanent once notes carry it"])
    vault = require_vault()
    target = _target(cwd)
    action: str | None = None
    if register:
        _register(vault, target, slug)
        action = "registered"
    _report(vault, target, action, as_json=as_json)
