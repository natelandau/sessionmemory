"""Locate the vault and the CLI, and run the CLI against the vault.

The vault owns everything that knows what a note is: paths, filenames, frontmatter,
and the rendering of the session-start block. This plugin owns what a session is.
That boundary is why nothing here parses or writes a note; every answer comes from
the CLI.

Two copies of the CLI can exist: the `sessionmemory` a person installed on PATH, and
`bin/sessionmemory` under `PLUGIN_ROOT`, which runs the copy shipped with this plugin.
The one on PATH is preferred when it passes a version handshake, since it is what the
person and their agent type, and its environment is already built. The handshake is
that its `--version` is at or past this plugin's own version, read from the
`pyproject.toml` beside the shim: the hooks hard-code the flags and payloads of the CLI
they ship with, and every hook fails open, so an older CLI would mean a session with no
memory and no error. Anything short of a passing handshake falls back to the shim.

Only the vault directory is discovered, from `$SESSIONMEMORY_VAULT` and then the plugin
config's `[vault] root`, and it is confirmed by the marker `sessionmemory init` writes
rather than by the directory merely existing: a path that names an empty or wrong
directory would otherwise read as a usable vault and every command would fail one at a
time.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

ROOT_ENV = "SESSIONMEMORY_VAULT"

CLI_NAME = "sessionmemory"

# hooks/sessionhooks/vaultcli.py -> hooks/sessionhooks -> hooks -> the plugin root.
PLUGIN_ROOT = Path(__file__).resolve().parents[2]

# `--version` imports no model and touches no index, so it never needs the full budget.
VERSION_TIMEOUT = 5

# What `sessionmemory init` writes, and the only proof a directory is a vault rather than
# a path someone mistyped.
MARKER = Path("_system") / "vault.toml"

# Long enough for a cold `uv run` to resolve the project and for the CLI to load
# its embedding model on a search, short enough that a wedged CLI cannot hold a hook open.
TIMEOUT = 25

# `project` reads registry.toml and opens neither the index nor the embedding
# model, so it never needs the full budget above; a caller checking registration
# alone should not risk doubling a session-start hook's total wait.
RESOLVE_TIMEOUT = 5

# commit_vault runs up to seven git calls in sequence; five seconds each keeps its
# worst case at 35s, inside both hook budgets.
COMMIT_GIT_TIMEOUT = 5

# The CLI's own contract: 0 success, 1 refused, 2 misconfigured.
EXIT_OK = 0
EXIT_REFUSED = 1


def _shim() -> Path:
    """The CLI shipped with this plugin, run through `bin/sessionmemory`."""
    return PLUGIN_ROOT / "bin" / CLI_NAME


def parse_version(text: str | None) -> tuple[int, int, int] | None:
    """Read the first dotted triple in `text` as a version, or None when there is none.

    A pre-release suffix is dropped rather than compared, because the handshake only
    asks whether a CLI is at least as new as the plugin, and no release of this project
    has ever changed the CLI between a pre-release and its final version.
    """
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return (major, minor, patch)


def plugin_version() -> tuple[int, int, int] | None:
    """The version this plugin shipped as, from the `pyproject.toml` beside the shim.

    The plugin and the CLI release from one repository under one version, so the
    package version is the floor a CLI on PATH has to meet. None when the file is
    missing or unreadable, which the caller treats as "trust only the shim".
    """
    try:
        data = tomllib.loads((PLUGIN_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None
    project = data.get("project")
    if not isinstance(project, dict):
        return None
    raw = project.get("version")
    return parse_version(raw) if isinstance(raw, str) else None


def _installed_version(cli: Path, env: Mapping[str, str]) -> tuple[int, int, int] | None:
    """Ask a CLI for its version, or None when it cannot say."""
    try:
        proc = subprocess.run(  # noqa: S603
            [str(cli), "--version"],
            capture_output=True,
            text=True,
            timeout=VERSION_TIMEOUT,
            check=False,
            env=dict(env),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != EXIT_OK:
        return None
    return parse_version(proc.stdout)


def _cli_on_path(env: Mapping[str, str]) -> Path | None:
    """The `sessionmemory` on PATH, when it passes the version handshake."""
    found = shutil.which(CLI_NAME, path=env.get("PATH"))
    if found is None:
        return None
    required = plugin_version()
    if required is None:
        return None
    installed = _installed_version(Path(found), env)
    if installed is None or installed < required:
        return None
    return Path(found)


def _json_object(raw: str | None) -> dict[str, Any] | None:
    """Parse `raw` as a JSON object, or None for anything the hook cannot trust."""
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


@dataclass(frozen=True, slots=True)
class VaultCLI:
    """A resolved vault directory and the CLI this plugin runs against it."""

    root: Path
    cli: Path = field(default_factory=_shim)
    # True when `cli` was found on PATH, so a prompt can spell it by name.
    on_path: bool = False

    @classmethod
    def discover(cls, *, env: Mapping[str, str], configured: str | None = None) -> VaultCLI | None:
        """Locate the vault and a CLI to run against it, or None when there is not one."""
        raw = env.get(ROOT_ENV) or configured
        if not raw:
            return None
        try:
            root = Path(raw).expanduser()
        except (OSError, RuntimeError):
            return None
        if not (root / MARKER).is_file():
            return None
        found = _cli_on_path(env)
        if found is not None:
            return cls(root=root, cli=found, on_path=True)
        shim = _shim()
        return cls(root=root, cli=shim) if shim.is_file() else None

    @property
    def command(self) -> str:
        """How a prompt spells the CLI: by name when it is on PATH, else the shim's path.

        A reader given a name it cannot run learns nothing, and a reader given an
        absolute cache path for a tool it has on PATH types something no person would.
        """
        return self.cli.name if self.on_path else str(self.cli)

    def _child_env(self, env: Mapping[str, str]) -> dict[str, str]:
        """The environment for the CLI, with the vault it must read pinned to ours.

        Discovery may have come from the config file rather than the environment. The
        CLI reads the same file, but pinning the root keeps a hook and the CLI it runs
        from ever resolving two different vaults.
        """
        return {**env, ROOT_ENV: str(self.root)}

    def run(
        self, args: list[str], *, cwd: Path, env: Mapping[str, str], timeout: int = TIMEOUT
    ) -> subprocess.CompletedProcess[str] | None:
        """Run one vault command, or None when it could not be run at all."""
        try:
            return subprocess.run(  # noqa: S603
                [str(self.cli), *args],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=self._child_env(env),
            )
        except (OSError, subprocess.SubprocessError):
            return None

    def output(
        self, args: list[str], *, cwd: Path, env: Mapping[str, str], timeout: int = TIMEOUT
    ) -> str | None:
        """Run a command and return its stdout, or None on any non-success."""
        proc = self.run(args, cwd=cwd, env=env, timeout=timeout)
        if proc is None or proc.returncode != EXIT_OK:
            return None
        return proc.stdout

    def inject(self, *, cwd: Path, env: Mapping[str, str]) -> str:
        """The session-start memory block for `cwd`, or "" when there is none.

        `--command` is `command`, so the guidance spells the CLI the way the reader
        can run it.
        """
        args = ["inject", "--cwd", str(cwd), "--command", self.command]
        return (self.output(args, cwd=cwd, env=env) or "").strip()

    def project_paths(
        self, *, cwd: Path, env: Mapping[str, str], timeout: int = TIMEOUT
    ) -> dict[str, str] | None:
        """Every vault path for the project owning `cwd`, or None when unresolvable.

        The payload is flattened: `project` nests the type directories under
        `paths` and reports `slug` and `project_dir` beside it, and every caller
        here wants one mapping of name to absolute path.
        """
        raw = self.output(
            ["project", "--cwd", str(cwd), "--json"], cwd=cwd, env=env, timeout=timeout
        )
        payload = _json_object(raw)
        if payload is None:
            return None
        flat = {k: v for k, v in payload.items() if isinstance(k, str) and isinstance(v, str)}
        nested = payload.get("paths")
        if isinstance(nested, dict):
            flat.update(
                {k: v for k, v in nested.items() if isinstance(k, str) and isinstance(v, str)}
            )
        return flat

    def resolve(self, *, cwd: Path, env: Mapping[str, str]) -> dict[str, Any] | None:
        """The `project --json` payload for `cwd`, or None when the CLI could not answer.

        `project` exits 1 for an unregistered directory but still prints its
        payload, and that payload is the answer here: `registered` says whether
        the directory has an entry, and `repo_root` says whether it is a git
        working tree, which is what decides whether the session-start hook may
        register it. Bounded by `RESOLVE_TIMEOUT`, since the call never touches
        the index or the embedding model.
        """
        proc = self.run(
            ["project", "--cwd", str(cwd), "--json"], cwd=cwd, env=env, timeout=RESOLVE_TIMEOUT
        )
        if proc is None or proc.returncode not in (EXIT_OK, EXIT_REFUSED):
            return None
        return _json_object(proc.stdout)

    def register(self, *, cwd: Path, env: Mapping[str, str]) -> str | None:
        """Register the project owning `cwd`; the slug, or None when the CLI refused.

        The CLI owns every rule here: which root and remote to record, how the
        slug is derived, and what to refuse (a bare repository, a slug already
        in use, a malformed registry). A refusal is reported as None rather than
        interpreted, since the hook's only fallback is to name the command.
        """
        raw = self.output(
            ["project", "--register", "--cwd", str(cwd), "--json"],
            cwd=cwd,
            env=env,
            timeout=RESOLVE_TIMEOUT,
        )
        payload = _json_object(raw)
        if payload is None:
            return None
        slug = payload.get("slug")
        return slug if isinstance(slug, str) and slug else None

    def commit(self, *, env: Mapping[str, str]) -> str | None:
        """Commit the vault's outstanding changes; the short sha, or None."""
        # Imported at call time so a test can patch sessionhooks.commit.commit_vault.
        from sessionhooks.commit import commit_vault

        return commit_vault(self.root, env=env, timeout=COMMIT_GIT_TIMEOUT)
