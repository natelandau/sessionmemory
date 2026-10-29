"""The `init` command that creates a new vault."""

from __future__ import annotations

# Typer resolves annotations from the module's globals to build its CLI, so this
# cannot move into a TYPE_CHECKING block.
from pathlib import Path  # noqa: TC003

import typer
from nclutils import pp

from sessionmemory.commands import _common
from sessionmemory.commands._common import JSON_OPTION, configured_vault, emit_json, fail
from sessionmemory.lib.bootstrap import InitResult, NotAVaultError, initialize

DIRECTORY_ARGUMENT = typer.Argument(
    None, help="Where to create the vault. Defaults to the configured vault root."
)
FORCE_OPTION = typer.Option(
    False,  # noqa: FBT003
    "--force",
    help="Initialize a directory that already has contents. Nothing existing is overwritten.",
)


def _report(result: InitResult, *, as_json: bool) -> None:
    """Report what initialization did.

    Args:
        result (InitResult): What `bootstrap.initialize` created and found already there.
        as_json (bool): Emit a machine readable payload instead of prose.
    """
    if as_json:
        emit_json(
            {
                "vault": str(result.vault),
                "created": list(result.created),
                "existed": list(result.existed),
            }
        )
        return

    if result.created:
        pp.success(f"initialized {result.vault}", details=list(result.created))
    else:
        pp.info(f"{result.vault} is already initialized; nothing to create")

    if result.existed:
        pp.info(f"already present: {', '.join(result.existed)}")


def init(
    directory: Path | None = DIRECTORY_ARGUMENT,
    *,
    force: bool = FORCE_OPTION,
    as_json: bool = JSON_OPTION,
) -> None:
    """Create the files a new vault needs.

    Off a terminal, `require_vault` refuses an uninitialized directory, and this command is
    what fixes that. When no directory is given, the configured root is read directly
    instead. On a terminal, a directory holding unrelated files is initialized once the
    person agrees, as if `--force` were given.

    Raises:
        Exit: When no directory is given and no vault root is configured or the
            configured one names a missing directory, or when the target directory holds
            unrelated files and `--force` is not given.
    """
    if directory is not None:
        # Every other reader resolves the vault path, so a relative or symlinked path
        # recorded verbatim names a different directory than the one this created.
        vault = directory.expanduser().resolve()
        vault.mkdir(parents=True, exist_ok=True)
    else:
        vault = configured_vault()

    try:
        result = initialize(vault, force=force)
    except NotAVaultError as error:
        if not _common.interactive():
            fail(str(error))
        # Only a directory holding files raises this, so the offer is the forced one.
        offered = _common.offer_init(vault)
        if offered is None:
            fail(f"{vault} was not initialized")
        result = offered

    _report(result, as_json=as_json)
