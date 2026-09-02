"""Verify the vault-path.py facade resolves paths from both of its sources.

The vault-backed flags are proven against the real vault CLI rather than a fake
one: `VaultCLI.cli` is fixed to this repository's own `bin/sessionmemory` shim, not
something a discovered vault root can supply, so nothing short of the real CLI
can answer a subprocess call to the facade.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from sessionhooks.store import Store  # ty: ignore[unresolved-import]
from sessionhooks.vaultcli import VaultCLI  # ty: ignore[unresolved-import]

from sessionmemory.lib.bootstrap import initialize
from tests._env import clean_environ

RESOLVER = Path(__file__).resolve().parent.parent.parent / "hooks" / "vault-path.py"
SHIM = RESOLVER.parent.parent / "bin" / "sessionmemory"


def _run(
    flag: str, *, cwd: Path, env_overrides: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run the facade with an environment that cannot reach a developer's real vault.

    HOME is redirected by every caller because the facade reads
    ~/.claude/sessionmemory.toml for a vault root, and SESSIONMEMORY_VAULT is
    dropped for the same reason: without both, a developer who has a vault
    configured runs these cases against it.
    """
    env = {k: v for k, v in clean_environ().items() if k != "SESSIONMEMORY_VAULT"}
    return subprocess.run(
        [str(RESOLVER), flag],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env={**env, **env_overrides},
        check=False,
        timeout=30,
    )


def _isolated_home(tmp_path: Path) -> str:
    """A HOME with no sessionmemory.toml in it."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return str(home)


def _registered_vault(tmp_path: Path, proj: Path) -> Path:
    """A real, initialized vault with `proj` registered as a project."""
    root = tmp_path / "vault"
    initialize(root)
    cli = VaultCLI(root=root)
    env = {**clean_environ(), "SESSIONMEMORY_VAULT": str(root)}
    assert cli.output(["project", "--register", "--cwd", str(proj)], cwd=proj, env=env) is not None
    return root


def _expected_paths(vault_root: Path, proj: Path) -> dict[str, str]:
    """The flattened paths the facade's own VaultCLI resolves for `proj`."""
    cli = VaultCLI(root=vault_root)
    env = {**clean_environ(), "SESSIONMEMORY_VAULT": str(vault_root)}
    paths = cli.project_paths(cwd=proj, env=env)
    assert paths is not None
    return paths


@pytest.mark.parametrize(
    ("flag", "attr"),
    [("--handoff", "handoff_path"), ("--state-dir", "state_dir")],
)
def test_resolver_prints_a_machine_local_path(flag: str, attr: str, tmp_path: Path) -> None:
    """Verify the machine-local flags print what the engine would compute."""
    # Given an isolated non-git project
    proj = tmp_path / "proj"
    proj.mkdir()
    env = {
        "HOME": _isolated_home(tmp_path),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "CLAUDE_PROJECT_DIR": str(proj),
    }
    expected = getattr(Store.for_cwd(cwd=proj, env={**clean_environ(), **env}), attr)

    # When the resolver runs in that project
    proc = _run(flag, cwd=proj, env_overrides=env)

    # Then it prints the same path the engine would compute
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(expected)


@pytest.mark.parametrize(
    ("flag", "key"),
    [
        ("--project", "project_dir"),
        ("--learnings", "learnings"),
        ("--backlog", "backlog"),
        ("--specs", "specs"),
        ("--plans", "plans"),
        ("--logs", "logs"),
    ],
)
def test_resolver_prints_a_vault_path(flag: str, key: str, tmp_path: Path) -> None:
    """Verify each durable flag prints the vault's own answer for its resolve key."""
    # Given a reachable vault with the project registered
    proj = tmp_path / "proj"
    proj.mkdir()
    vault_root = _registered_vault(tmp_path, proj)
    expected = _expected_paths(vault_root, proj)[key]
    env = {
        "HOME": _isolated_home(tmp_path),
        "CLAUDE_PROJECT_DIR": str(proj),
        "SESSIONMEMORY_VAULT": str(vault_root),
    }

    # When the resolver runs in that project
    proc = _run(flag, cwd=proj, env_overrides=env)

    # Then the vault's answer is printed verbatim
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == expected


def test_resolver_reports_a_missing_vault_rather_than_printing_nothing(tmp_path: Path) -> None:
    """Verify an unreachable vault is distinguishable from an empty path."""
    # Given no vault anywhere
    proj = tmp_path / "proj"
    proj.mkdir()
    env = {"HOME": _isolated_home(tmp_path), "CLAUDE_PROJECT_DIR": str(proj)}

    # When a vault-backed path is requested
    proc = _run("--learnings", cwd=proj, env_overrides=env)

    # Then it fails loudly with nothing on stdout
    assert proc.returncode == 2
    assert proc.stdout.strip() == ""
    assert "vault" in proc.stderr


def test_resolver_exits_2_when_the_directory_lacks_the_vault_marker(tmp_path: Path) -> None:
    """Verify a directory without `_system/vault.toml` is refused, not treated as a vault."""
    # Given a directory that merely exists, never initialized by `sessionmemory init`
    proj = tmp_path / "proj"
    proj.mkdir()
    bare = tmp_path / "not-a-vault"
    bare.mkdir()
    env = {
        "HOME": _isolated_home(tmp_path),
        "CLAUDE_PROJECT_DIR": str(proj),
        "SESSIONMEMORY_VAULT": str(bare),
    }

    # When a vault-backed path is requested
    proc = _run("--learnings", cwd=proj, env_overrides=env)

    # Then it is refused the same way an unreachable vault is
    assert proc.returncode == 2
    assert proc.stdout.strip() == ""
    assert "vault" in proc.stderr


def test_resolver_reports_an_unregistered_project(tmp_path: Path) -> None:
    """Verify a reachable vault that has never seen this project fails the same as no vault."""
    # Given a reachable vault that has never registered this project
    proj = tmp_path / "proj"
    proj.mkdir()
    root = tmp_path / "vault"
    initialize(root)
    env = {
        "HOME": _isolated_home(tmp_path),
        "CLAUDE_PROJECT_DIR": str(proj),
        "SESSIONMEMORY_VAULT": str(root),
    }

    # When a vault-backed path is requested
    proc = _run("--learnings", cwd=proj, env_overrides=env)

    # Then it exits 2 with nothing on stdout rather than crashing
    assert proc.returncode == 2
    assert proc.stdout.strip() == ""
    assert "vault" in proc.stderr


def test_resolver_prints_the_cli_path(tmp_path: Path) -> None:
    """Verify the CLI itself is resolvable, so a skill never re-derives where it lives."""
    # Given a vault reachable only through the config file
    proj = tmp_path / "proj"
    proj.mkdir()
    home = Path(_isolated_home(tmp_path))
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    root = tmp_path / "vault"
    initialize(root)
    (home / ".claude" / "sessionmemory.toml").write_text(
        f'[vault]\nroot = "{root}"\n', encoding="utf-8"
    )
    env = {"HOME": str(home), "CLAUDE_PROJECT_DIR": str(proj)}

    # When the CLI path is requested
    proc = _run("--cli", cwd=proj, env_overrides=env)

    # Then this repository's own shim is printed, found via the config fallback
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(SHIM)


def test_resolver_no_flag_is_usage_error(tmp_path: Path) -> None:
    """Verify invoking with no target flag exits non-zero (a usage error)."""
    # Given an isolated project
    proj = tmp_path / "proj"
    proj.mkdir()
    # When the resolver runs with no flag
    proc = subprocess.run(
        [str(RESOLVER)],
        cwd=str(proj),
        capture_output=True,
        text=True,
        env={**clean_environ(), "HOME": _isolated_home(tmp_path), "CLAUDE_PROJECT_DIR": str(proj)},
        check=False,
        timeout=30,
    )
    # Then it is a usage error
    assert proc.returncode != 0
    assert proc.stdout.strip() == ""


def test_resolver_unknown_flag_is_usage_error(tmp_path: Path) -> None:
    """Verify an unknown flag exits non-zero rather than printing a path."""
    # Given an isolated project
    proj = tmp_path / "proj"
    proj.mkdir()
    # When the resolver runs with an unknown flag
    proc = _run(
        "--nope",
        cwd=proj,
        env_overrides={"HOME": _isolated_home(tmp_path), "CLAUDE_PROJECT_DIR": str(proj)},
    )
    # Then it is a usage error
    assert proc.returncode != 0
    assert proc.stdout.strip() == ""
