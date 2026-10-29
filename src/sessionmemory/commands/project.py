"""Commands that answer and record which project a directory belongs to."""

from __future__ import annotations

# Typer resolves annotations from the module's globals to build its CLI.
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING

import typer
from nclutils import pp

from sessionmemory.commands import _common
from sessionmemory.commands._common import (
    CWD_OPTION,
    JSON_OPTION,
    VersionedGroup,
    emit_json,
    fail,
    report_malformed_registry,
    require_vault,
    resolve_or_exit,
    target_dir,
)
from sessionmemory.lib import paths as paths_lib
from sessionmemory.lib import registration, registry
from sessionmemory.lib.config import today

if TYPE_CHECKING:
    from sessionmemory.lib.resolve import Resolution

app = typer.Typer(
    cls=VersionedGroup,
    invoke_without_command=True,
    help="Show this directory's project, or register it.",
)

SLUG_OPTION = typer.Option(
    None, "--slug", help="Name the project instead of deriving it from the remote or directory."
)
# The flag forms predate the subcommands. They stay, hidden, because a plugin older than
# the CLI on PATH still calls them.
DEPRECATED_SLUG_OPTION = typer.Option(None, "--slug", hidden=True)
DEPRECATED_REGISTER_OPTION = typer.Option(False, "--register", hidden=True)  # noqa: FBT003
DEPRECATED_MARK_REVIEWED_OPTION = typer.Option(False, "--mark-reviewed", hidden=True)  # noqa: FBT003


def _load(vault: Path) -> dict[str, registry.Project]:
    try:
        return registry.load(vault)
    except registry.RegistryError as error:
        report_malformed_registry(vault, error)


def _report(
    vault: Path,
    target: Path,
    action: str | None,
    *,
    as_json: bool,
    resolved: Resolution | None = None,
) -> None:
    """Print the entry as it now stands, and exit non-zero when there is none.

    This is the command's only stdout in either mode, so a write is reported by showing
    its result rather than by a line of its own.

    Args:
        vault (Path): The vault root.
        target (Path): The directory to report on.
        action (str | None): The verb naming a write that just happened, or None for a
            plain view.
        as_json (bool): Emit the payload instead of prose.
        resolved (Resolution | None): The directory's resolution when the caller already
            has one, or None to resolve it here.

    Raises:
        Exit: With code 1 when the directory is not registered.
    """
    result = resolved or resolve_or_exit(vault, target)
    project_paths = paths_lib.project_paths(vault, result.slug) if result.slug else {}

    if as_json:
        emit_json(
            {
                "slug": result.slug,
                "registered": result.registered,
                "repo_root": str(result.repo_root) if result.repo_root else None,
                "is_worktree": result.is_worktree,
                "project_dir": project_paths.get("project_dir"),
                "paths": project_paths,
            }
        )
    elif result.slug:
        headline = f"{action} {result.slug!r}" if action else result.slug
        # A directory outside git resolves by longest path prefix, so the directory asked
        # about is routinely below the root the registry records, which is the one shown.
        entry = _load(vault).get(result.slug)
        pp.success(headline, details=[f"root: {entry.root if entry else target}"])
    else:
        pp.error(
            f"{target} is not a registered project",
            details=[] if _common.interactive() else ["run: sessionmemory project register"],
        )

    if not result.registered:
        raise typer.Exit(1)


def _free_slug(vault: Path, found: registration.Candidate, slug: str | None) -> str | None:
    """Ask a person at a terminal for another slug while the one in hand is taken.

    The registry is read without its lock, since a person may take a while to answer;
    registration still checks the slug again under the lock.

    Returns:
        str | None: A slug no project holds, or `slug` unchanged when the directory is
            already registered and registration will refuse it anyway.

    Raises:
        Exit: When the person cancels the prompt.
    """
    projects = _load(vault)
    if registration.existing(projects, found):
        return slug
    wanted = slug if slug is not None else registration.derive_slug(found)
    problem = f"slug {wanted!r} is already in use"
    while wanted in projects:
        pp.warning(problem)
        answer = _common.ask_text("Another slug for this project")
        if answer is None:
            raise typer.Exit(1)
        if registration.as_slug(answer) != answer:
            problem = f"{answer!r} is not a slug: use lowercase letters, digits, and hyphens"
            continue
        wanted = answer
        problem = f"slug {wanted!r} is already in use"
    return wanted


def _register(vault: Path, found: registration.Candidate, slug: str | None) -> None:
    """Record a directory in the vault registry, on a terminal swapping a taken slug.

    Raises:
        Exit: When the slug is malformed or taken, the directory is already registered,
            or no slug can be derived from its remote or its directory name.
    """
    try:
        if slug is not None:
            registration.check_slug(slug)
        if _common.interactive():
            slug = _free_slug(vault, found, slug)
        registered = registration.record(vault, found, slug)
    except registration.RegistrationError as error:
        fail(error.message, error.details)
    except registry.RegistryError as error:
        report_malformed_registry(vault, error)

    # Registering inside another project is legitimate, but an accidental one looks
    # exactly like a deliberate one, so the nesting is reported rather than assumed.
    if registered.enclosing is not None:
        enclosing = registered.enclosing.slug
        pp.warning(
            f"{found.root} sits inside project {enclosing!r}",
            details=[
                f"{registered.slug!r} now wins for paths under {found.root}",
                f"remove it from the registry if you meant to use {enclosing!r}",
            ],
        )


def _candidate(target: Path) -> registration.Candidate:
    try:
        return registration.candidate(target)
    except registration.RegistrationError as error:
        fail(error.message, error.details)


def offer_registration(vault: Path, target: Path) -> bool:
    """Ask a person at a terminal whether to register an unregistered directory.

    Args:
        vault (Path): The vault root.
        target (Path): The unregistered directory.

    Returns:
        bool: True when the person agreed and the directory is now registered.
    """
    found = _candidate(target)
    try:
        slug = registration.derive_slug(found)
    except registration.RegistrationError as error:
        fail(error.message, error.details)
    question = f"{target} is not a registered project. Register it as {slug!r}?"
    if not _common.confirm(question=question, yes="Yes, register it"):
        return False
    _register(vault, found, None)
    return True


@app.callback()
def project_command(
    ctx: typer.Context,
    cwd: Path | None = CWD_OPTION,
    *,
    as_json: bool = JSON_OPTION,
    slug: str | None = DEPRECATED_SLUG_OPTION,
    register: bool = DEPRECATED_REGISTER_OPTION,
    mark_reviewed: bool = DEPRECATED_MARK_REVIEWED_OPTION,
) -> None:
    """Show which project this directory belongs to, and exit 1 when it has none.

    Raises:
        Exit: When the deprecated flags are combined in a way their subcommands do not
            allow, or with code 1 when the directory has no project.
    """
    if ctx.invoked_subcommand is not None:
        # The subcommand never sees these, and registering the shell's directory in
        # place of a misplaced --cwd would file notes under a permanent wrong slug.
        if cwd is not None or as_json or slug is not None or register or mark_reviewed:
            fail(
                f"options for {ctx.invoked_subcommand!r} go after it",
                [f"run: sessionmemory project {ctx.invoked_subcommand} --cwd <dir> ..."],
            )
        return
    if mark_reviewed and (register or slug is not None):
        fail("--mark-reviewed cannot be combined with --register or --slug")
    if slug is not None and not register:
        fail("--slug applies only with --register", ["a slug is permanent once notes carry it"])
    if register:
        pp.warning("--register is deprecated; run: sessionmemory project register")
        register_command(cwd=cwd, slug=slug, as_json=as_json)
    elif mark_reviewed:
        pp.warning("--mark-reviewed is deprecated; run: sessionmemory project mark-reviewed")
        mark_reviewed_command(cwd=cwd, as_json=as_json)
    else:
        vault, target = require_vault(), target_dir(cwd)
        action: str | None = None
        resolved: Resolution | None = None
        if not as_json and _common.interactive():
            resolved = resolve_or_exit(vault, target)
            if not resolved.registered and offer_registration(vault, target):
                action, resolved = "registered", None
        _report(vault, target, action, as_json=as_json, resolved=resolved)


@app.command("register")
def register_command(
    cwd: Path | None = CWD_OPTION,
    slug: str | None = SLUG_OPTION,
    *,
    as_json: bool = JSON_OPTION,
) -> None:
    """Register this directory as a project, so it can hold pages.

    Raises:
        Exit: When the directory does not exist, is a bare repository, is already
            registered, or its slug is taken or cannot be derived.
    """
    vault = require_vault()
    target = target_dir(cwd)
    _register(vault, _candidate(target), slug)
    _report(vault, target, "registered", as_json=as_json)


@app.command("mark-reviewed")
def mark_reviewed_command(cwd: Path | None = CWD_OPTION, *, as_json: bool = JSON_OPTION) -> None:
    """Record today as the day this project's learnings were reviewed.

    Raises:
        Exit: With code 1 when the directory has no project.
    """
    vault = require_vault()
    target = target_dir(cwd)
    action: str | None = None
    resolved = resolve_or_exit(vault, target)
    if resolved.slug:
        registry.mark_reviewed(vault, resolved.slug, today())
        action = "reviewed"
    _report(vault, target, action, as_json=as_json, resolved=resolved)
