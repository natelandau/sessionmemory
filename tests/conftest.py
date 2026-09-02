"""Fixtures shared across the test suite."""

from __future__ import annotations

import pytest
import typer.rich_utils

from sessionmemory.lib.bootstrap import initialize


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch) -> None:
    """Keep Rich from wrapping prose a test asserts on.

    `pp` sizes its console from `COLUMNS`, and a temp path pytest builds can be long
    enough to wrap mid-value on a narrow one. A test asserting that a path appears in
    the output then fails on the wrapping rather than on the behavior, and only on the
    machines whose temp paths are long enough, which makes it look like flakiness.
    """
    monkeypatch.setenv("COLUMNS", "1000")


@pytest.fixture(autouse=True)
def _stub_embedder(monkeypatch) -> None:
    """Select the deterministic embedder for the whole suite.

    The real model costs seconds per load and downloads 520MB on a machine with no cached
    copy, and no test here depends on a real embedding.
    """
    monkeypatch.setenv("SESSIONMEMORY_EMBEDDER", "stub")


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """Build an initialized vault and point the environment at it."""
    root = tmp_path / "vault"
    initialize(root)
    monkeypatch.setenv("SESSIONMEMORY_VAULT", str(root))
    return root


@pytest.fixture(autouse=True)
def _git_identity(tmp_path_factory, monkeypatch) -> None:
    """Give git a committer identity that does not depend on the machine.

    Fixtures commit into throwaway repositories, and git refuses a commit with no
    identity on a host where it cannot derive one from the login and hostname, which a
    CI runner is. Pointing `GIT_CONFIG_GLOBAL` at a file of our own also keeps a
    developer's real global config, such as commit signing, out of the suite. The hooks
    strip `GIT_`-prefixed variables before running git, so their tests set a repo-local
    identity themselves.
    """
    config = tmp_path_factory.mktemp("git") / "config"
    config.write_text('[user]\n\tname = "Test"\n\temail = "test@example.com"\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture(autouse=True)
def _plain_typer_output(monkeypatch) -> None:
    """Keep Typer's error output free of escape codes, whatever the environment says.

    Typer decides at import time to force terminal mode under `GITHUB_ACTIONS`,
    `FORCE_COLOR`, or `PY_COLORS`, and Rich's option highlighter then styles
    `--max-distance` as three fragments, so a test asserting the option name appears in
    a usage error fails only on a machine that exports one of those. `CliRunner` is not
    a terminal, and the tests assert on what a caller redirecting output would see.
    """
    monkeypatch.setattr(typer.rich_utils, "FORCE_TERMINAL", False)
