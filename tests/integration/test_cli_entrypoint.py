"""Tests for the console-script entry point.

Every other integration test invokes the Typer `app` directly through `CliRunner`, which
never runs `cli.main`. That leaves the entry point itself, `def main(): app()`, unmeasured
even though `bin/sessionmemory` and the installed console script both call nothing else.
"""

from __future__ import annotations

import importlib.metadata
import sys

import pytest
from typer.testing import CliRunner

from sessionmemory.cli import app, main


def test_main_runs_the_typer_app(monkeypatch, capsys):
    """Verify main() invokes the Typer app with the process's own argv."""
    monkeypatch.setattr(sys, "argv", ["vault", "--help"])

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 0
    assert "Usage:" in capsys.readouterr().out


def test_version_flag_prints_the_installed_version():
    """Verify `--version` prints the package version alone, for a caller to compare."""
    result = CliRunner().invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.output.strip() == importlib.metadata.version("sessionmemory")


@pytest.mark.parametrize("argv", [["frobnicate"], ["new", "frobnicate"]])
def test_unknown_command_names_the_version_and_the_upgrade(argv):
    """Verify an unknown command or subcommand says which version is installed and how to upgrade."""
    result = CliRunner().invoke(app, argv)

    assert result.exit_code == 2
    assert importlib.metadata.version("sessionmemory") in result.output
    assert "uv tool upgrade sessionmemory" in result.output


def test_unknown_command_without_installed_metadata_still_exits_with_usage_error(mocker):
    """Verify a missing package version leaves the usage error intact."""
    mocker.patch(
        "importlib.metadata.version",
        autospec=True,
        side_effect=importlib.metadata.PackageNotFoundError,
    )

    result = CliRunner().invoke(app, ["frobnicate"])

    assert result.exit_code == 2
    assert "No such command" in result.output
    assert "uv tool upgrade sessionmemory" in result.output


def _error_lines(output: str) -> list[str]:
    """Return the error panel's text lines with the box drawing stripped."""
    return [line.strip("│ ").rstrip() for line in output.splitlines() if line.startswith("│")]


def test_unknown_command_without_a_suggestion_says_a_newer_release_may_have_it():
    """Verify a miss with no close match points at a newer release."""
    version = importlib.metadata.version("sessionmemory")

    result = CliRunner().invoke(app, ["frobnicate"])

    assert _error_lines(result.output) == [
        "No such command 'frobnicate'.",
        f"Installed sessionmemory is {version}; a newer release may have it.",
        "Upgrade with: uv tool upgrade sessionmemory",
    ]


def test_unknown_command_with_a_suggestion_does_not_blame_the_release():
    """Verify a typo Typer can correct is not attributed to an old release."""
    version = importlib.metadata.version("sessionmemory")

    result = CliRunner().invoke(app, ["projects"])

    assert _error_lines(result.output) == [
        "No such command 'projects'. Did you mean 'project'?",
        f"Installed sessionmemory is {version}.",
        "Upgrade with: uv tool upgrade sessionmemory",
    ]


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        pytest.param(
            ["frobnicate"],
            ["No such command 'frobnicate'.", "A newer release may have it."],
            id="no-suggestion",
        ),
        pytest.param(
            ["projects"], ["No such command 'projects'. Did you mean 'project'?"], id="suggestion"
        ),
    ],
)
def test_unknown_command_without_a_version_starts_its_own_sentence(mocker, argv, expected):
    """Verify the hint reads as whole sentences when the installed version is unknown."""
    mocker.patch(
        "importlib.metadata.version",
        autospec=True,
        side_effect=importlib.metadata.PackageNotFoundError,
    )

    result = CliRunner().invoke(app, argv)

    assert _error_lines(result.output) == [*expected, "Upgrade with: uv tool upgrade sessionmemory"]


@pytest.mark.parametrize(
    "argv", [["-h"], ["new", "-h"], ["search", "-h"], ["project", "register", "-h"]]
)
def test_short_help_flag_works_at_every_level(argv):
    """Verify -h is accepted wherever --help is, since subcommands inherit it."""
    result = CliRunner().invoke(app, argv)

    assert result.exit_code == 0, result.output
    assert "Usage:" in result.stdout


def test_help_lists_everyday_commands_before_plugin_internals():
    """Verify the top-level help groups commands so a newcomer meets search first."""
    out = CliRunner().invoke(app, ["--help"]).stdout

    assert out.index("Everyday") < out.index("search") < out.index("Plugin internals")
    assert out.index("Plugin internals") < out.index("inject")
