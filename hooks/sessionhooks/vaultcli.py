"""Locate the vault and run its CLI.

The vault owns everything that knows what a note is: paths, filenames, frontmatter,
and the rendering of the session-start block. This plugin owns what a session is.
That boundary is why nothing here parses or writes a note; every answer comes from
the CLI.

The CLI ships in this repository, so it is not discovered. `PLUGIN_ROOT` is the
plugin root and `bin/sessionmemory` beneath it is the entry point. Only the vault
directory is discovered, from `$SESSIONMEMORY_VAULT` and then the plugin config's
`[vault] root`, and it is confirmed by the marker `sessionmemory init` writes rather than
by the directory merely existing: a path that names an empty or wrong directory
would otherwise read as a usable vault and every command would fail one at a time.

There is no schema-version check. It existed to catch the plugin and the CLI
drifting apart across two repositories, and they now version together in one.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

ROOT_ENV = "SESSIONMEMORY_VAULT"

# hooks/sessionhooks/vaultcli.py -> hooks/sessionhooks -> hooks -> the plugin root.
PLUGIN_ROOT = Path(__file__).resolve().parents[2]

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


@dataclass(frozen=True, slots=True)
class VaultCLI:
    """A resolved vault directory and the shim this plugin runs against it."""

    root: Path

    @classmethod
    def discover(cls, *, env: Mapping[str, str], configured: str | None = None) -> VaultCLI | None:
        """Locate the vault, or None when there is not a usable one."""
        raw = env.get(ROOT_ENV) or configured
        if not raw:
            return None
        try:
            root = Path(raw).expanduser()
        except (OSError, RuntimeError):
            return None
        if not (root / MARKER).is_file():
            return None
        cli = cls(root=root)
        return cli if cli.cli.is_file() else None

    @property
    def cli(self) -> Path:
        """The shim every vault command runs through."""
        return PLUGIN_ROOT / "bin" / "sessionmemory"

    def _child_env(self, env: Mapping[str, str]) -> dict[str, str]:
        """The environment for the CLI, with the vault it must read pinned to ours.

        Discovery may have come from the config file rather than the environment,
        and the CLI reads its vault only from the environment.
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

        `--command` is the shim's own path rather than the bare name, because a
        session that reached this plugin without installing the CLI as a tool has
        no `sessionmemory` on PATH, and guidance naming a command the reader cannot run
        tells them nothing.
        """
        args = ["inject", "--cwd", str(cwd), "--command", str(self.cli)]
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
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        flat = {k: v for k, v in payload.items() if isinstance(k, str) and isinstance(v, str)}
        nested = payload.get("paths")
        if isinstance(nested, dict):
            flat.update(
                {k: v for k, v in nested.items() if isinstance(k, str) and isinstance(v, str)}
            )
        return flat

    def registered(self, *, cwd: Path, env: Mapping[str, str]) -> bool:
        """Report whether `cwd` belongs to a project the vault knows.

        `project` exits non-zero for an unregistered directory, which is what the
        session-start hint reads. Bounded by `RESOLVE_TIMEOUT` rather than the full
        `TIMEOUT`, since this call never touches the index or the embedding model.
        """
        return self.project_paths(cwd=cwd, env=env, timeout=RESOLVE_TIMEOUT) is not None

    def commit(self, *, env: Mapping[str, str]) -> str | None:
        """Commit the vault's outstanding changes; the short sha, or None."""
        # Imported at call time so a test can patch sessionhooks.commit.commit_vault.
        from sessionhooks.commit import commit_vault

        return commit_vault(self.root, env=env, timeout=COMMIT_GIT_TIMEOUT)
