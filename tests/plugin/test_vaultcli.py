"""The vault seam, exercised against a real shim on disk.

A mocked subprocess would pass while the real invocation was broken, and the whole
point of the seam is that it can actually run the CLI.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sessionhooks.commit import CommitOutcome  # ty: ignore[unresolved-import]
from sessionhooks.vaultcli import (  # ty: ignore[unresolved-import]
    COMMIT_GIT_TIMEOUT,
    ROOT_ENV,
    Lookup,
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


def test_locate_finds_the_vault_without_running_any_cli(marked_vault, stub_cli, path_cli):
    """Verify locating the vault alone starts no subprocess, unlike discovery's handshake."""
    env = _env(**{ROOT_ENV: str(marked_vault)}, PATH=path_cli("unrunnable"))

    assert VaultCLI.locate(env=env, configured=None) == marked_vault
    assert not (stub_cli.parent / "args.txt").exists()


def test_locate_refuses_a_directory_without_the_marker(tmp_path):
    """Verify locate applies the same marker check discovery does."""
    bare = tmp_path / "not-a-vault"
    bare.mkdir()

    assert VaultCLI.locate(env=_env(), configured=str(bare)) is None


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
    spy = mocker.patch(
        "sessionhooks.commit.commit_vault",
        autospec=True,
        return_value=CommitOutcome(sha="abc1234"),
    )

    assert cli.commit(env={}) == CommitOutcome(sha="abc1234")
    spy.assert_called_once_with(marked_vault, env={}, timeout=COMMIT_GIT_TIMEOUT, note="")


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


def test_discover_logs_the_handshake_at_debug(tmp_path, marked_vault, caplog):
    """Verify a person raising the level to debug can see which CLI answered and why."""
    caplog.set_level("DEBUG", logger="sessionmemory")
    empty = tmp_path / "empty"
    empty.mkdir()
    VaultCLI.discover(env={"SESSIONMEMORY_VAULT": str(marked_vault), "PATH": str(empty)})
    assert "handshake: no sessionmemory on PATH" in caplog.text


# ---------------------------------------------------------------------------
# VaultCLI.project_lookup
# ---------------------------------------------------------------------------


def _completed(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["sessionmemory"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _lookup(tmp_path: Path) -> Lookup:
    cli = VaultCLI(root=tmp_path / "vault", cli=tmp_path / "sessionmemory")
    return cli.project_lookup(cwd=tmp_path, env={}, timeout=120)


def test_project_lookup_returns_flattened_paths(tmp_path: Path, mocker) -> None:
    """Verify a registered project's payload comes back flattened with no reason."""
    mocker.patch(
        "sessionhooks.vaultcli.subprocess.run",
        autospec=True,
        return_value=_completed(
            0, '{"slug": "demo", "registered": true, "paths": {"learnings": "/v/learnings"}}'
        ),
    )
    assert _lookup(tmp_path) == Lookup({"slug": "demo", "learnings": "/v/learnings"})


def test_project_lookup_returns_paths_from_a_registered_payload_on_exit_1(
    tmp_path: Path, mocker
) -> None:
    """Verify exit 1 with a registered payload still yields the flattened paths."""
    # Given a CLI that exits 1 but reports the project as registered
    mocker.patch(
        "sessionhooks.vaultcli.subprocess.run",
        autospec=True,
        return_value=_completed(
            1, '{"slug": "demo", "registered": true, "paths": {"logs": "/v/logs"}}'
        ),
    )
    # When looking it up / Then the payload is the answer
    assert _lookup(tmp_path) == Lookup({"slug": "demo", "logs": "/v/logs"})


def test_project_lookup_and_run_share_one_spawn_helper(tmp_path: Path, mocker) -> None:
    """Verify both entry points launch the CLI through `_spawn`, so neither wires its own."""
    # Given the shared helper replaced with one returning a registered payload
    spawn = mocker.patch.object(
        VaultCLI,
        "_spawn",
        autospec=True,
        return_value=_completed(0, '{"slug": "demo", "registered": true}'),
    )
    cli = VaultCLI(root=tmp_path / "vault", cli=tmp_path / "sessionmemory")

    # When looking up the project and running a command
    lookup = cli.project_lookup(cwd=tmp_path, env={}, timeout=120)
    cli.run(["inject"], cwd=tmp_path, env={}, timeout=7)

    # Then both calls went through the helper with their own arguments
    assert lookup == Lookup({"slug": "demo"})
    assert spawn.call_args_list == [
        mocker.call(
            cli, ["project", "--cwd", str(tmp_path), "--json"], cwd=tmp_path, env={}, timeout=120
        ),
        mocker.call(cli, ["inject"], cwd=tmp_path, env={}, timeout=7),
    ]


def test_project_lookup_reports_a_timeout(tmp_path: Path, mocker) -> None:
    """Verify a CLI that outruns the timeout is reported with the seconds it was given."""
    mocker.patch(
        "sessionhooks.vaultcli.subprocess.run",
        autospec=True,
        side_effect=subprocess.TimeoutExpired(cmd="sessionmemory", timeout=120),
    )
    assert _lookup(tmp_path) == Lookup(None, "project timed out after 120s")


def test_project_lookup_reports_a_failure_to_start(tmp_path: Path, mocker) -> None:
    """Verify an OSError on launch is named in the reason."""
    mocker.patch(
        "sessionhooks.vaultcli.subprocess.run", autospec=True, side_effect=OSError("no exec")
    )
    assert _lookup(tmp_path) == Lookup(None, "project could not start: no exec")


def test_project_lookup_reports_an_unexpected_exit_with_the_stderr_tail(
    tmp_path: Path, mocker
) -> None:
    """Verify an exit past 1 carries the last 200 characters of stderr."""
    stderr = "x" * 300 + "boom"
    mocker.patch(
        "sessionhooks.vaultcli.subprocess.run",
        autospec=True,
        return_value=_completed(2, stderr=stderr),
    )
    assert _lookup(tmp_path) == Lookup(None, f"project exited 2: {stderr[-200:]}")


def test_project_lookup_keeps_a_multiline_stderr_tail_on_one_line(tmp_path: Path, mocker) -> None:
    """Verify a stderr tail spread over lines is collapsed so the log line stays whole."""
    mocker.patch(
        "sessionhooks.vaultcli.subprocess.run",
        autospec=True,
        return_value=_completed(2, stderr="Traceback:\n  File x\n\tboom\n"),
    )
    assert _lookup(tmp_path) == Lookup(None, "project exited 2: Traceback: File x boom")


def test_project_lookup_reports_an_unregistered_project(tmp_path: Path, mocker) -> None:
    """Verify exit 1 with registered false reads as not registered."""
    mocker.patch(
        "sessionhooks.vaultcli.subprocess.run",
        autospec=True,
        return_value=_completed(1, '{"registered": false}'),
    )
    assert _lookup(tmp_path) == Lookup(None, "project not registered")


def test_project_lookup_reports_output_that_is_not_json(tmp_path: Path, mocker) -> None:
    """Verify stdout that does not parse as an object is reported."""
    mocker.patch(
        "sessionhooks.vaultcli.subprocess.run",
        autospec=True,
        return_value=_completed(0, "not json"),
    )
    assert _lookup(tmp_path) == Lookup(None, "project printed no JSON")


def test_for_root_passes_the_version_timeout_to_the_handshake(tmp_path: Path, mocker) -> None:
    """Verify for_root hands its version_timeout to the --version call."""
    mocker.patch("sessionhooks.vaultcli.shutil.which", autospec=True, return_value="/bin/sm")
    mocker.patch("sessionhooks.vaultcli.plugin_version", autospec=True, return_value=(0, 0, 1))
    run = mocker.patch(
        "sessionhooks.vaultcli.subprocess.run",
        autospec=True,
        return_value=_completed(0, "sessionmemory 9.9.9"),
    )
    VaultCLI.for_root(tmp_path, env={}, version_timeout=120)
    assert run.call_args.kwargs["timeout"] == 120


_ENTRIES = {
    "entries": [
        {
            "kind": "local-md",
            "name": "CLAUDE.local.md",
            "status": "created",
            "link": "/p/CLAUDE.local.md",
            "target": "/v/t",
        },
        {
            "kind": "skill",
            "name": "skills/a",
            "status": "conflict",
            "link": "/p/.claude/skills/a",
            "target": "/v/a",
        },
    ]
}


def _link_stub(stub_cli: Path, body: str) -> None:
    stub_cli.write_text(
        "#!/usr/bin/env python3\nimport json, sys, pathlib\n"
        "pathlib.Path(__file__).with_name('args.txt').write_text(' '.join(sys.argv[1:]))\n"
        f"{body}\n",
        encoding="utf-8",
    )
    stub_cli.chmod(0o755)


@pytest.mark.parametrize("code", [0, 1], ids=["ok", "conflict"])
def test_sync_local_returns_the_whole_entries_payload(marked_vault, stub_cli, tmp_path, code):
    """Verify sync_local() sends `local sync --json --cwd` and returns every entry, on exit 1 too."""
    _link_stub(stub_cli, f"print(json.dumps({_ENTRIES!r}))\nsys.exit({code})")
    cli = VaultCLI.discover(env=_env(**{ROOT_ENV: str(marked_vault)}), configured=None)

    payload = cli.sync_local(cwd=tmp_path, env=_env())

    assert payload == _ENTRIES
    sent = (stub_cli.parent / "args.txt").read_text(encoding="utf-8").split()
    assert sent[:2] == ["local", "sync"]
    assert "--json" in sent
    assert sent[sent.index("--cwd") + 1] == str(tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("print(json.dumps({'status': 'created', 'target': '/v/t'}))", id="ok"),
        pytest.param("print(json.dumps({'status': 'conflict'}))\nsys.exit(1)", id="exit-1"),
        pytest.param("print(json.dumps({'entries': 'x'}))\nsys.exit(1)", id="not-a-list"),
        pytest.param("print('not json')\nsys.exit(1)", id="exit-1-not-json"),
    ],
)
def test_sync_local_none_without_an_entries_list(marked_vault, stub_cli, tmp_path, body):
    """Verify output lacking an `entries` list reads as unavailable, whatever the exit."""
    _link_stub(stub_cli, body)
    cli = VaultCLI.discover(env=_env(**{ROOT_ENV: str(marked_vault)}), configured=None)

    assert cli.sync_local(cwd=tmp_path, env=_env()) is None


def test_sync_local_reads_a_bare_refusal_as_refused(marked_vault, stub_cli, tmp_path):
    """Verify exit 1 with no payload, as a refusal gives, reads as refused."""
    _link_stub(stub_cli, "sys.stderr.write('tracked by git\\n')\nsys.exit(1)")
    cli = VaultCLI.discover(env=_env(**{ROOT_ENV: str(marked_vault)}), configured=None)

    assert cli.sync_local(cwd=tmp_path, env=_env()) == {"status": "refused"}


def test_sync_local_none_when_misconfigured(marked_vault, stub_cli, tmp_path):
    """Verify an exit other than 0 or 1 reads as None, whatever it printed."""
    _link_stub(stub_cli, "print(json.dumps({'status': 'conflict'}))\nsys.exit(2)")
    cli = VaultCLI.discover(env=_env(**{ROOT_ENV: str(marked_vault)}), configured=None)

    assert cli.sync_local(cwd=tmp_path, env=_env()) is None


def test_sync_local_none_on_a_timeout(marked_vault, stub_cli, tmp_path, mocker):
    """Verify a CLI that outruns its budget reads as None."""
    cli = VaultCLI.discover(env=_env(**{ROOT_ENV: str(marked_vault)}), configured=None)
    mocker.patch(
        "sessionhooks.vaultcli.subprocess.run",
        autospec=True,
        side_effect=subprocess.TimeoutExpired(cmd="sessionmemory", timeout=5),
    )

    assert cli.sync_local(cwd=tmp_path, env=_env()) is None
