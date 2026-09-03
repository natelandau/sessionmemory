"""The vault seam, exercised against a real shim on disk.

A mocked subprocess would pass while the real invocation was broken, and the whole
point of the seam is that it can actually run the CLI.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sessionhooks.vaultcli import (  # ty: ignore[unresolved-import]
    COMMIT_GIT_TIMEOUT,
    ROOT_ENV,
    VaultCLI,
    parse_version,
)

if TYPE_CHECKING:
    from collections.abc import Callable


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


def _path_without_cli() -> str:
    """The real PATH with every directory holding a `sessionmemory` removed.

    A developer with the tool installed would otherwise have discovery find it, and
    every case here needs to say which CLI is on PATH.
    """
    kept = [
        d for d in os.environ["PATH"].split(os.pathsep) if not (Path(d) / "sessionmemory").exists()
    ]
    return os.pathsep.join(kept)


def _env(**extra: str) -> dict[str, str]:
    return {"PATH": _path_without_cli(), **extra}


@pytest.fixture
def plugin_version(stub_cli: Path) -> str:
    """Declare the plugin's own version beside the stubbed shim."""
    (stub_cli.parent.parent / "pyproject.toml").write_text(
        '[project]\nname = "sessionmemory"\nversion = "1.4.0"\n', encoding="utf-8"
    )
    return "1.4.0"


@pytest.fixture
def path_cli(tmp_path: Path) -> Callable[[str], str]:
    """Put a `sessionmemory` reporting a chosen version on PATH; return that PATH."""

    def install(version: str) -> str:
        bindir = tmp_path / "pathbin"
        bindir.mkdir(exist_ok=True)
        script = bindir / "sessionmemory"
        body = "import sys\nprint('from-path')\n"
        if version == "broken":
            body = "import sys\nsys.exit(1)\n"
        elif version:
            body = f"import sys\nprint({version!r} if sys.argv[1:] == ['--version'] else 'from-path')\n"
        interpreter = "/nonexistent/interpreter" if version == "unrunnable" else sys.executable
        script.write_text(f"#!{interpreter}\n{body}", encoding="utf-8")
        script.chmod(0o755)
        return os.pathsep.join([str(bindir), _path_without_cli()])

    return install


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
    """Verify guidance names the shim's path when no tool on PATH is usable."""
    cli = VaultCLI.discover(env=_env(**{ROOT_ENV: str(marked_vault)}), configured=None)

    cli.inject(cwd=tmp_path, env=_env())

    recorded = (stub_cli.parent / "args.txt").read_text(encoding="utf-8").split()
    assert "--command" in recorded
    assert recorded[recorded.index("--command") + 1] == str(cli.cli)


def test_discovery_uses_the_cli_on_path_when_it_is_recent_enough(
    marked_vault, stub_cli, plugin_version, path_cli
):
    """Verify a tool on PATH at or past the plugin's version is the CLI the hooks run."""
    env = _env(**{ROOT_ENV: str(marked_vault)}, PATH=path_cli("1.4.0"))

    cli = VaultCLI.discover(env=env, configured=None)

    assert cli.cli != stub_cli
    assert cli.cli.name == "sessionmemory"
    assert cli.output(["anything"], cwd=marked_vault, env=env).strip() == "from-path"


def test_discovery_prefers_a_newer_cli_on_path(marked_vault, stub_cli, plugin_version, path_cli):
    """Verify a tool newer than the plugin still passes the handshake."""
    env = _env(**{ROOT_ENV: str(marked_vault)}, PATH=path_cli("2.0.0"))

    assert VaultCLI.discover(env=env, configured=None).cli != stub_cli


@pytest.mark.parametrize(
    "reported",
    [
        pytest.param("1.3.9", id="older"),
        pytest.param("", id="no-version-flag"),
        pytest.param("broken", id="version-exits-nonzero"),
        pytest.param("unrunnable", id="cannot-be-started"),
    ],
)
def test_discovery_falls_back_to_the_shim_when_the_path_cli_fails_the_handshake(
    marked_vault, stub_cli, plugin_version, path_cli, reported
):
    """Verify an older, silent, or failing tool on PATH is passed over for the shim."""
    env = _env(**{ROOT_ENV: str(marked_vault)}, PATH=path_cli(reported))

    assert VaultCLI.discover(env=env, configured=None).cli == stub_cli


def test_discovery_falls_back_to_the_shim_when_nothing_is_on_path(
    marked_vault, stub_cli, plugin_version
):
    """Verify the shim is the CLI when PATH holds no `sessionmemory` at all."""
    assert (
        VaultCLI.discover(env=_env(**{ROOT_ENV: str(marked_vault)}), configured=None).cli
        == stub_cli
    )


def test_discovery_falls_back_to_the_shim_when_the_plugin_version_is_unreadable(
    marked_vault, stub_cli, path_cli
):
    """Verify a plugin that cannot read its own version trusts only its shim."""
    env = _env(**{ROOT_ENV: str(marked_vault)}, PATH=path_cli("9.9.9"))

    assert VaultCLI.discover(env=env, configured=None).cli == stub_cli


def test_discovery_falls_back_to_the_shim_when_the_plugin_manifest_names_no_version(
    marked_vault, stub_cli, path_cli
):
    """Verify a pyproject without a `[project]` table reads as no version at all."""
    (stub_cli.parent.parent / "pyproject.toml").write_text(
        "[tool.other]\nx = 1\n", encoding="utf-8"
    )
    env = _env(**{ROOT_ENV: str(marked_vault)}, PATH=path_cli("9.9.9"))

    assert VaultCLI.discover(env=env, configured=None).cli == stub_cli


def test_command_is_the_bare_name_for_a_cli_on_path(
    marked_vault, stub_cli, plugin_version, path_cli
):
    """Verify prompts spell a PATH tool by name, so a reader types what a person would."""
    env = _env(**{ROOT_ENV: str(marked_vault)}, PATH=path_cli("1.4.0"))

    assert VaultCLI.discover(env=env, configured=None).command == "sessionmemory"


def test_command_is_the_shim_path_otherwise(marked_vault, stub_cli):
    """Verify prompts spell the shim by absolute path, since nothing on PATH runs it."""
    cli = VaultCLI.discover(env=_env(**{ROOT_ENV: str(marked_vault)}), configured=None)

    assert cli.command == str(stub_cli)


def test_inject_names_the_bare_command_for_a_cli_on_path(
    marked_vault, stub_cli, plugin_version, path_cli, tmp_path
):
    """Verify the guidance block is told to spell a PATH tool by name."""
    env = _env(**{ROOT_ENV: str(marked_vault)}, PATH=path_cli("1.4.0"))
    recorder = tmp_path / "pathbin" / "sessionmemory"
    recorder.write_text(
        f"#!{sys.executable}\nimport pathlib, sys\n"
        "pathlib.Path(__file__).with_name('args.txt').write_text(' '.join(sys.argv[1:]))\n"
        "print('1.4.0' if sys.argv[1:] == ['--version'] else 'block')\n",
        encoding="utf-8",
    )

    VaultCLI.discover(env=env, configured=None).inject(cwd=tmp_path, env=env)

    recorded = (tmp_path / "pathbin" / "args.txt").read_text(encoding="utf-8").split()
    assert recorded[recorded.index("--command") + 1] == "sessionmemory"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("0.2.0\n", (0, 2, 0), id="bare"),
        pytest.param("sessionmemory 1.10.3", (1, 10, 3), id="prefixed"),
        pytest.param("2.0.0rc1", (2, 0, 0), id="prerelease"),
        pytest.param("", None, id="empty"),
        pytest.param("not a version", None, id="prose"),
    ],
)
def test_parse_version(text, expected):
    """Verify parse_version reads the first dotted triple and nothing else."""
    assert parse_version(text) == expected


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


def test_resolve_reads_the_payload_of_a_registered_project(marked_vault, stub_cli, tmp_path):
    """Verify resolve() returns the CLI's payload for a directory with an entry."""
    # Given a discovered vault whose stub CLI resolves a project
    cli = VaultCLI.discover(env=_env(**{ROOT_ENV: str(marked_vault)}), configured=None)

    # When resolving / Then the payload reports the entry
    resolved = cli.resolve(cwd=tmp_path, env=_env())
    assert resolved is not None
    assert resolved["registered"] is True
    assert resolved["slug"] == "demo"


def test_resolve_reads_the_payload_of_an_unregistered_project(marked_vault, stub_cli, tmp_path):
    """Verify resolve() still parses `project --json` when the CLI exits 1 for no entry."""
    # Given a CLI that reports the directory unregistered, and exits 1 saying so
    stub_cli.write_text(
        "#!/usr/bin/env python3\nimport json, sys\n"
        "print(json.dumps({'slug': None, 'registered': False, 'repo_root': '/r/proj'}))\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    stub_cli.chmod(0o755)
    cli = VaultCLI.discover(env=_env(**{ROOT_ENV: str(marked_vault)}), configured=None)

    # When resolving / Then the payload comes through, exit code notwithstanding
    resolved = cli.resolve(cwd=tmp_path, env=_env())
    assert resolved == {"slug": None, "registered": False, "repo_root": "/r/proj"}


@pytest.mark.parametrize(
    "emitted", ["not json at all", '["a", "b"]'], ids=["malformed", "non-object"]
)
def test_resolve_refuses_output_it_cannot_trust(marked_vault, stub_cli, tmp_path, emitted):
    """Verify a `project` payload that is not a JSON object resolves to nothing."""
    stub_cli.write_text(
        f"#!/usr/bin/env python3\nimport sys\nprint({emitted!r})\n", encoding="utf-8"
    )
    stub_cli.chmod(0o755)
    cli = VaultCLI.discover(env=_env(**{ROOT_ENV: str(marked_vault)}), configured=None)

    assert cli.resolve(cwd=tmp_path, env=_env()) is None


def test_register_returns_the_slug_the_cli_filed_the_project_under(
    marked_vault, stub_cli, tmp_path
):
    """Verify register() sends `project --register` for the directory and returns the slug."""
    cli = VaultCLI.discover(env=_env(**{ROOT_ENV: str(marked_vault)}), configured=None)

    slug = cli.register(cwd=tmp_path, env=_env())

    assert slug == "demo"
    sent = (stub_cli.parent / "args.txt").read_text(encoding="utf-8").split()
    assert sent[:2] == ["project", "--register"]
    assert sent[sent.index("--cwd") + 1] == str(tmp_path)
    assert "--json" in sent


def test_register_yields_none_when_the_cli_refuses(marked_vault, stub_cli, tmp_path):
    """Verify a refused registration is reported as None rather than a partial answer."""
    # Given a CLI that refuses the registration, as it does for a slug already in use
    stub_cli.write_text(
        "#!/usr/bin/env python3\nimport json, sys\n"
        "print(json.dumps({'slug': None, 'registered': False}))\nsys.exit(1)\n",
        encoding="utf-8",
    )
    stub_cli.chmod(0o755)
    cli = VaultCLI.discover(env=_env(**{ROOT_ENV: str(marked_vault)}), configured=None)

    assert cli.register(cwd=tmp_path, env=_env()) is None


def test_resolve_yields_none_when_the_cli_is_misconfigured(marked_vault, stub_cli, tmp_path):
    """Verify an exit code outside the CLI's answered set is never read as a payload."""
    # Given a CLI that exits 2, its code for a missing or uninitialized vault
    stub_cli.write_text(
        "#!/usr/bin/env python3\nimport json, sys\n"
        "print(json.dumps({'registered': False, 'repo_root': '/r'}))\nsys.exit(2)\n",
        encoding="utf-8",
    )
    stub_cli.chmod(0o755)
    cli = VaultCLI.discover(env=_env(**{ROOT_ENV: str(marked_vault)}), configured=None)

    assert cli.resolve(cwd=tmp_path, env=_env()) is None
