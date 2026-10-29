"""Helpers shared by every command module."""

from __future__ import annotations

import importlib.metadata
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import typer
import typer.core
from nclutils import ask, pp

# Typer vendors Click, so the `click` package's classes never match its errors.
from typer._click import core as click_core
from typer._click import exceptions as click_exceptions

from sessionmemory.lib import embed, registry
from sessionmemory.lib.bootstrap import InitResult, initialize, is_empty
from sessionmemory.lib.config import (
    CONFIG_FILE,
    VaultNotConfiguredError,
    is_initialized,
    vault_root,
)
from sessionmemory.lib.paths import SYSTEM_DIR
from sessionmemory.lib.resolve import resolve as resolve_project

if TYPE_CHECKING:
    from sessionmemory.lib.embed import Embedder
    from sessionmemory.lib.resolve import Resolution

EMBEDDER_ENV_VAR = "SESSIONMEMORY_EMBEDDER"

CWD_OPTION = typer.Option(None, "--cwd", "-C", help="Directory to resolve the project from.")
JSON_OPTION = typer.Option(False, "--json", help="Emit JSON instead of prose.")  # noqa: FBT003


# The two ways to name the vault, offered together whenever neither is set.
VAULT_FIX = [
    "export SESSIONMEMORY_VAULT=/path/to/your/vault",
    f"or set [vault] root in {CONFIG_FILE}",
]


class VersionedGroup(typer.core.TyperGroup):
    """A command group whose unknown-command error names the installed version."""

    def resolve_command(
        self, ctx: click_core.Context, args: list[str]
    ) -> tuple[str | None, click_core.Command | None, list[str]]:
        """Name the installed version and how to upgrade in an unknown command's error."""
        try:
            return super().resolve_command(ctx, args)
        except click_exceptions.UsageError as error:
            # Typer 0.27 raises a plain UsageError via ctx.fail with no typed signal for a miss.
            if args and self.get_command(ctx, args[0]) is None:
                error.message += f"\n{_unknown_command_hint(error.message)}"
            raise


def _unknown_command_hint(message: str) -> str:
    """Return the lines naming the installed version and how to upgrade.

    A miss Typer can correct is a typo, so a newer release is only offered as the cause
    when no close match was suggested.
    """
    try:
        version = importlib.metadata.version("sessionmemory")
    except importlib.metadata.PackageNotFoundError:
        version = ""
    suggested = "Did you mean" in message
    if version and suggested:
        cause = f"Installed sessionmemory is {version}.\n"
    elif version:
        cause = f"Installed sessionmemory is {version}; a newer release may have it.\n"
    elif suggested:
        cause = ""
    else:
        cause = "A newer release may have it.\n"
    return f"{cause}Upgrade with: uv tool upgrade sessionmemory"


def build_embedder() -> Embedder:
    """Build the embedder commands use: the stub under test, the real model otherwise.

    Read at call time so a test selects the stub with `monkeypatch.setenv`.
    """
    if os.environ.get(EMBEDDER_ENV_VAR) == "stub":
        return embed.StubEmbedder()
    return embed.default_embedder()


STDIN_SENTINEL = "-"


def resolve_body(body: str, body_file: Path | None) -> str:
    """Return a note's markdown body, read from a file or stdin when one was named.

    Shell-quoting a document of any real length into `--body` is impractical, so without
    this a caller creates the note empty and then writes the file itself, which is the
    one habit every skill in this project tells a caller not to form.

    Args:
        body (str): The `--body` value.
        body_file (Path | None): The `--body-file` value, where `-` means stdin.

    Returns:
        str: The body to write.

    Raises:
        Exit: When both options carry a value, or the named file cannot be read.
    """
    if body_file is None:
        return body

    if body:
        fail("--body and --body-file are mutually exclusive", ["pass one or the other"])

    if str(body_file) == STDIN_SENTINEL:
        return sys.stdin.read()

    try:
        return body_file.read_text(encoding="utf-8")
    except OSError as error:
        fail(f"cannot read {body_file}: {error.strerror or error}")
    except UnicodeDecodeError:
        fail(f"{body_file} is not valid UTF-8 text")


def emit_value(text: str) -> None:
    """Print one machine readable value with nothing added to it.

    Machine readable output is read by a caller rather than by a person, so an ANSI escape
    in it is corruption rather than decoration: a styled path breaks the page path `vault
    search` prints for a caller to copy, and a styled version string breaks the comparison
    it exists for. Rich styles values by default and honors `FORCE_COLOR`, which real
    shells export, so styling does not depend on stdout being a terminal and a caller
    cannot opt out of it. Every value a caller consumes goes through here; prose for a
    person keeps its styling and goes through `pp.success`, `pp.warning`, and `pp.error`.

    `markup=False` keeps square brackets from being read as Rich tags, `emoji=False`
    keeps `:word:` from being replaced by the emoji of that name, `highlight=False`
    suppresses the automatic value coloring, and `soft_wrap=True` keeps a value longer
    than the console width on one line.

    Args:
        text (str): The value to print.
    """
    pp.console().print(text, markup=False, emoji=False, highlight=False, soft_wrap=True)


def emit_json(payload: object) -> None:
    """Print a machine readable payload as unstyled JSON.

    `--json` exists so a caller can read a value out of the output instead of scraping
    prose, which makes an escape sequence anywhere in the payload a parse error. Rich's
    JSON printer syntax-highlights what it writes, so the payload is serialized here and
    printed through `emit_value`, which documents why nothing may style it.

    Args:
        payload (object): Any JSON-serializable value.
    """
    emit_value(json.dumps(payload, indent=2))


def interactive() -> bool:
    """Report whether a person can answer a prompt, so an agent's call never blocks.

    Returns:
        bool: True when both stdin and stdout are terminals.
    """
    return sys.stdin.isatty() and sys.stdout.isatty()


def answer(question: str, yes: str, no: str = "No, leave it alone") -> bool | None:
    """Ask one yes-or-no question, telling a no apart from a cancelled question.

    Use it where a no is remembered, so walking away from the prompt is not taken as one.

    Args:
        question (str): The question.
        yes (str): The yes answer, naming what it does.
        no (str): The no answer, naming what it does.

    Returns:
        bool | None: True for yes, False for no, None when the person cancelled.
    """
    picked = ask.choose_one_from_list(choices=[(yes, "yes"), (no, "no")], message=question)
    return None if picked is None else picked == "yes"


def confirm(question: str, yes: str) -> bool:
    """Ask one yes-or-no question whose answers say what each choice does.

    Args:
        question (str): The question.
        yes (str): The yes answer, naming what it does.

    Returns:
        bool: True only when the person chose yes; cancelling counts as no.
    """
    return answer(question=question, yes=yes) is True


def init_question(vault: Path, *, force: bool) -> tuple[str, str]:
    """Word the question that offers to initialize a vault, and its yes answer.

    Args:
        vault (Path): The directory to initialize.
        force (bool): Whether the directory holds files but no vault marker.

    Returns:
        tuple[str, str]: The question and the yes answer.
    """
    if force:
        return (
            f"{vault} holds files but no vault marker. Initialize a vault there anyway?",
            "Yes, initialize it (its files are kept)",
        )
    return f"{vault} is not a vault yet. Initialize one there?", "Yes, initialize it"


def offer_init(vault: Path) -> InitResult | None:
    """Ask a person at a terminal whether to initialize `vault`, and do it on a yes.

    Args:
        vault (Path): The uninitialized directory.

    Returns:
        InitResult | None: What was created, or None when the person declined.
    """
    force = not is_empty(vault)
    question, yes = init_question(vault, force=force)
    if not confirm(question=question, yes=yes):
        return None
    return initialize(vault, force=force)


def ask_text(message: str) -> str | None:
    """Ask a person at a terminal to type an answer.

    Args:
        message (str): The prompt.

    Returns:
        str | None: The answer, or None when the person cancelled.
    """
    try:
        return str(typer.prompt(message)).strip() or None
    except typer.Abort:
        return None


def configured_vault() -> Path:
    """Return the configured vault root, initialized or not, or exit naming how to set it.

    Returns:
        Path: The resolved vault directory.

    Raises:
        Exit: When no vault root is configured or the configured one names a missing
            directory.
    """
    try:
        return vault_root()
    except VaultNotConfiguredError as error:
        pp.error(str(error), details=VAULT_FIX)
        raise typer.Exit(1) from error


def require_vault() -> Path:
    """Return an initialized vault root, or exit with the command that fixes it.

    Every command needs the vault location before it can do anything else, so this is
    the one place that turns an unset, missing, or uninitialized `SESSIONMEMORY_VAULT`
    into a clean message instead of a traceback or a note scattered into the wrong place.

    Returns:
        Path: The resolved, initialized vault directory.

    Raises:
        Exit: When the variable is unset, names a missing directory, or names a
            directory that `sessionmemory init` has never touched.
    """
    vault = configured_vault()
    if is_initialized(vault):
        return vault

    if interactive():
        if offer_init(vault) is None:
            fail(f"{vault} is not a vault")
        pp.success(f"initialized {vault}")
        return vault

    # An empty directory is the ordinary bootstrap case, and `sessionmemory init` is safe
    # for a caller, human or agent, to run there. A non-empty directory holding no
    # marker is different: initializing it is a one-time human decision about a
    # specific path, never something to hand an agent as its next action, so that
    # case is described rather than phrased as a `run:` instruction to follow.
    if is_empty(vault):
        fail(f"{vault} is not a vault", ["run: sessionmemory init"])

    pp.error(
        f"{vault} is not an initialized vault",
        details=[
            "it holds files but no _system/vault.toml marker",
            (
                "if this is an existing vault, a human can initialize it once by "
                "hand with: sessionmemory init --force"
            ),
        ],
    )
    raise typer.Exit(1)


def fail(message: str, details: list[str] | None = None) -> NoReturn:
    """Report a failure and exit non-zero.

    Args:
        message (str): The error line.
        details (list[str] | None): Follow-up lines.

    Raises:
        Exit: Always, with code 1.
    """
    pp.error(message, details=details or [])
    raise typer.Exit(1)


def report_malformed_registry(vault: Path, error: registry.RegistryError) -> NoReturn:
    """Report a malformed registry.toml and exit non-zero.

    Args:
        vault (Path): The vault root.
        error (registry.RegistryError): The error raised while loading the registry.

    Raises:
        Exit: Always, with code 1.
    """
    path = vault / SYSTEM_DIR / registry.REGISTRY_FILE
    pp.error(f"{path} is malformed: {error}")
    raise typer.Exit(1) from error


def target_dir(cwd: Path | None) -> Path:
    """Return the directory a `--cwd` option names, or the shell's, resolved.

    Args:
        cwd (Path | None): The `--cwd` value.

    Returns:
        Path: The resolved directory.
    """
    return (cwd or Path.cwd()).resolve()


def resolve_or_exit(vault: Path, target: Path) -> Resolution:
    """Resolve which project `target` belongs to, exiting on a malformed registry.

    Args:
        vault (Path): The vault root.
        target (Path): The directory to resolve.

    Returns:
        Resolution: The match, or an unregistered result.
    """
    try:
        return resolve_project(vault, target)
    except registry.RegistryError as error:
        report_malformed_registry(vault, error)


def require_project(vault: Path, cwd: Path | None = None) -> str:
    """Return the current project's slug, or exit with instructions.

    Args:
        vault (Path): The vault root.
        cwd (Path | None): The directory to resolve, or None for the shell's.

    Returns:
        str: The resolved project slug.

    Raises:
        Exit: When the working directory is not a registered project.
    """
    target = target_dir(cwd)
    slug = resolve_or_exit(vault, target).slug
    if slug is not None:
        return slug
    if not interactive():
        fail("this directory is not a registered project", ["run: sessionmemory project register"])
    # Imported here, since the project command imports this module.
    from sessionmemory.commands.project import offer_registration

    if offer_registration(vault, target):
        return require_project(vault, target)
    fail("this directory is not a registered project")
