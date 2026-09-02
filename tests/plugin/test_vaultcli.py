"""The vault seam, exercised against a real shim on disk.

A mocked subprocess would pass while the real invocation was broken, and the whole
point of the seam is that it can actually run the CLI.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from sessionhooks.vaultcli import (  # ty: ignore[unresolved-import]
    COMMIT_GIT_TIMEOUT,
    ROOT_ENV,
    VaultCLI,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def marked_vault(tmp_path: Path) -> Path:
    """A directory carrying the marker `sessionmemory init` writes."""
    root = tmp_path / "vault"
    (root / "_system").mkdir(parents=True)
    (root / "_system" / "vault.toml").write_text("", encoding="utf-8")
    return root


@pytest.fixture
def stub_cli(tmp_path: Path, monkeypatch) -> Path:
    """Replace the shim with a script that echoes a known answer per subcommand."""
    plugin_root = tmp_path / "plugin"
    (plugin_root / "bin").mkdir(parents=True)
    script = plugin_root / "bin" / "sessionmemory"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "a = sys.argv[1:]\n"
        # Recorded so a test can assert on what the seam actually sent, not on what
        # the seam reports having sent.
        "pathlib.Path(__file__).with_name('args.txt').write_text(' '.join(a))\n"
        "if a[0] == 'whichvault': print(os.environ.get('SESSIONMEMORY_VAULT', ''))\n"
        "elif a[0] == 'inject': print('## Guides\\nstuff')\n"
        "elif a[0] == 'project': print(json.dumps({\n"
        "    'slug': 'demo', 'registered': True, 'project_dir': '/v/projects/demo',\n"
        "    'paths': {'logs': '/v/projects/demo/logs', 'backlog': '/v/projects/demo/backlog'}}))\n"
        "elif a[0] == 'unregistered': sys.exit(1)\n"
        "else: sys.exit(1)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setattr("sessionhooks.vaultcli.PLUGIN_ROOT", plugin_root)
    return script


def _env(**extra: str) -> dict[str, str]:
    return {"PATH": os.environ["PATH"], **extra}


def test_discovery_prefers_the_environment(marked_vault, stub_cli, tmp_path):
    """Verify discovery reads the environment variable over the configured fallback."""
    cli = VaultCLI.discover(env=_env(**{ROOT_ENV: str(marked_vault)}), configured=str(tmp_path))

    assert cli is not None
    assert cli.root == marked_vault


def test_discovery_falls_back_to_the_configured_root(marked_vault, stub_cli):
    """Verify discovery falls back to the configured root when the environment names none."""
    cli = VaultCLI.discover(env=_env(), configured=str(marked_vault))

    assert cli is not None
    assert cli.root == marked_vault


def test_a_directory_without_the_marker_is_not_a_vault(tmp_path, stub_cli):
    """Verify a directory lacking the vault-init marker is not treated as a vault."""
    bare = tmp_path / "not-a-vault"
    bare.mkdir()

    assert VaultCLI.discover(env=_env(**{ROOT_ENV: str(bare)}), configured=None) is None


def test_no_root_anywhere_yields_nothing(stub_cli):
    """Verify discovery yields None when neither the environment nor config names a root."""
    assert VaultCLI.discover(env=_env(), configured=None) is None


def test_the_child_environment_pins_the_resolved_root(marked_vault, stub_cli, tmp_path):
    """Verify the resolved root is pinned into the child environment for the CLI to read."""
    # Discovery came from the config file and the passed environment names no vault,
    # so the child seeing one at all is the whole assertion.
    cli = VaultCLI.discover(env=_env(), configured=str(marked_vault))

    assert cli.output(["whichvault"], cwd=tmp_path, env=_env()).strip() == str(marked_vault)


def test_inject_returns_the_block(marked_vault, stub_cli, tmp_path):
    """Verify inject returns the session-start block the CLI prints."""
    cli = VaultCLI.discover(env=_env(**{ROOT_ENV: str(marked_vault)}), configured=None)

    assert cli.inject(cwd=tmp_path, env=_env()).startswith("## Guides")


def test_project_paths_flattens_the_nested_payload(marked_vault, stub_cli, tmp_path):
    """Verify project_paths flattens the nested paths payload into one mapping."""
    cli = VaultCLI.discover(env=_env(**{ROOT_ENV: str(marked_vault)}), configured=None)

    paths = cli.project_paths(cwd=tmp_path, env=_env())

    assert paths["project_dir"] == "/v/projects/demo"
    assert paths["logs"] == "/v/projects/demo/logs"
    assert paths["slug"] == "demo"


def test_a_failing_command_yields_none(marked_vault, stub_cli, tmp_path):
    """Verify a non-zero exit from the CLI yields None rather than raising."""
    cli = VaultCLI.discover(env=_env(**{ROOT_ENV: str(marked_vault)}), configured=None)

    assert cli.output(["unregistered"], cwd=tmp_path, env=_env()) is None


def test_inject_tells_the_cli_how_the_reader_must_invoke_it(marked_vault, stub_cli, tmp_path):
    """Verify guidance names the shim's path, since a plugin-only install has no vault on PATH."""
    cli = VaultCLI.discover(env=_env(**{ROOT_ENV: str(marked_vault)}), configured=None)

    cli.inject(cwd=tmp_path, env=_env())

    recorded = (stub_cli.parent / "args.txt").read_text(encoding="utf-8").split()
    assert "--command" in recorded
    assert recorded[recorded.index("--command") + 1] == str(cli.cli)


def test_a_root_that_cannot_be_expanded_is_not_a_vault(stub_cli):
    """Verify an unresolvable home reference fails closed rather than raising at session start."""
    assert (
        VaultCLI.discover(env=_env(**{ROOT_ENV: "~nosuchuser_xyz/vault"}), configured=None) is None
    )


def test_a_command_that_cannot_be_run_at_all_yields_nothing(marked_vault, stub_cli, monkeypatch):
    """Verify a CLI that cannot be spawned reads as no answer, never as an exception."""
    cli = VaultCLI.discover(env=_env(**{ROOT_ENV: str(marked_vault)}), configured=None)

    def _explode(*_args: object, **_kwargs: object) -> None:
        message = "cannot spawn"
        raise OSError(message)

    monkeypatch.setattr("sessionhooks.vaultcli.subprocess.run", _explode)

    assert cli.run(["inject"], cwd=marked_vault, env=_env()) is None


@pytest.mark.parametrize(
    "emitted", ["not json at all", '["a", "b"]'], ids=["malformed", "non-object"]
)
def test_project_paths_refuses_output_it_cannot_trust(marked_vault, stub_cli, tmp_path, emitted):
    """Verify a project payload that is not a JSON object yields nothing to act on."""
    stub_cli.write_text(
        f"#!/usr/bin/env python3\nimport sys\nprint({emitted!r})\n", encoding="utf-8"
    )
    stub_cli.chmod(0o755)
    cli = VaultCLI.discover(env=_env(**{ROOT_ENV: str(marked_vault)}), configured=None)

    assert cli.project_paths(cwd=tmp_path, env=_env()) is None


def test_commit_delegates_to_commit_vault(marked_vault, stub_cli, mocker):
    """Verify VaultCLI.commit commits the discovered root."""
    cli = VaultCLI.discover(env=_env(**{ROOT_ENV: str(marked_vault)}), configured=None)
    spy = mocker.patch("sessionhooks.commit.commit_vault", autospec=True, return_value="abc1234")

    assert cli.commit(env={}) == "abc1234"
    spy.assert_called_once_with(marked_vault, env={}, timeout=COMMIT_GIT_TIMEOUT)


def test_project_paths_yields_none_when_the_command_fails(marked_vault, stub_cli, tmp_path):
    """Verify project_paths returns None outright when the underlying command fails."""
    # Given a CLI that exits non-zero for every command
    stub_cli.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n", encoding="utf-8")
    stub_cli.chmod(0o755)
    cli = VaultCLI.discover(env=_env(**{ROOT_ENV: str(marked_vault)}), configured=None)

    # When resolving project paths / Then nothing is resolved
    assert cli.project_paths(cwd=tmp_path, env=_env()) is None


def test_project_paths_without_a_nested_paths_key(marked_vault, stub_cli, tmp_path):
    """Verify a flat payload with no nested 'paths' table still returns the flat fields."""
    # Given a CLI whose payload carries no nested "paths" table
    stub_cli.write_text(
        "#!/usr/bin/env python3\nimport json\nprint(json.dumps({'slug': 'demo'}))\n",
        encoding="utf-8",
    )
    stub_cli.chmod(0o755)
    cli = VaultCLI.discover(env=_env(**{ROOT_ENV: str(marked_vault)}), configured=None)

    # When resolving project paths / Then the flat fields still come through
    assert cli.project_paths(cwd=tmp_path, env=_env()) == {"slug": "demo"}


def test_registered_reports_whether_project_paths_resolved(marked_vault, stub_cli, tmp_path):
    """Verify registered() reflects whether project_paths resolved something."""
    # Given a discovered vault whose stub CLI resolves a project
    cli = VaultCLI.discover(env=_env(**{ROOT_ENV: str(marked_vault)}), configured=None)

    # When asking whether the project is registered / Then it reports True
    assert cli.registered(cwd=tmp_path, env=_env()) is True
