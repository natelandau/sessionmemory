"""Duty tasks for the project."""

from __future__ import annotations

import os
import re
import sys
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

from duty import duty
from rich.console import Console

if TYPE_CHECKING:
    from duty.context import Context


console = Console()
PYPROJECT = Path("pyproject.toml")
PY_SRC_PATHS = (
    Path(_) for _ in ("src/", "tests/", "duties.py", "scripts/", "hooks/") if Path(_).exists()
)
PY_SRC_LIST = tuple(str(_) for _ in PY_SRC_PATHS)
SH_SRC_LIST = tuple(str(_) for _ in sorted(Path("bin").glob("*")) if _.is_file())
CI = os.environ.get("CI", "0") in {"1", "true", "yes", ""}
PROJECT_ROOT = Path(__file__).parent
PREK_DUTY_HOOKS = ("ruff", "format", "ty", "typos", "yamllint", "shellcheck", "test")
VERSION = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
# Everything but prose and CI config, so a docs-only change skips the suite
TEST_PATHS = (
    ".",
    ":(exclude)docs/",
    ":(exclude)README.md",
    ":(exclude)CHANGELOG.md",
    ":(exclude).github/",
)


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from a string.

    Args:
        text (str): String to remove ANSI escape sequences from.

    Returns:
        str: String without ANSI escape sequences.
    """
    ansi_chars = re.compile(r"(\x9B|\x1B\[)[0-?]*[ -\/]*[@-~]")

    # Replace [ with \[ so rich doesn't interpret output as style tags
    return ansi_chars.sub("", text).replace("[", r"\[")


def gated(*command: str, name: str, paths: tuple[str, ...] = ()) -> list[str]:
    """Wrap a command in gate-cache so it skips when it already passed on the same content.

    gate-cache turns itself off when `CI` is set, so CI always runs the command. Run
    `gate-cache --skill` before adding or changing a gate.

    Args:
        *command (str): The command and its arguments.
        name (str): The gate name, free of spaces. Put an input git does not track after an
            `@`, as in `test@python3.14`.
        paths (tuple[str, ...]): Git pathspecs that scope the gate. Defaults to the whole tree.

    Returns:
        list[str]: The argv to pass to `ctx.run`.
    """
    scope = [arg for path in paths for arg in ("--path", path)]
    return ["gate-cache", "--name", name, *scope, "--", *command]


def pyprefix(title: str) -> str:
    """Add a prefix to the title if CI is true.

    Returns:
        str: Title with prefix if CI is true.
    """
    if CI:
        prefix = f"(python{sys.version_info.major}.{sys.version_info.minor})"
        return f"{prefix:14}{title}"
    return title


@duty(silent=True)
def clean(ctx: Context) -> None:
    """Clean the project."""
    ctx.run("rm -rf .coverage*")
    ctx.run("rm -rf .cache")
    ctx.run("rm -rf build")
    ctx.run("rm -rf dist")
    ctx.run("rm -rf pip-wheel-metadata")
    ctx.run("find . -type d -name __pycache__ | xargs rm -rf")
    ctx.run("find . -name '.DS_Store' -delete")
    ctx.run(["gate-cache", "clear", "--yes"])


@duty
def ruff(ctx: Context) -> None:
    """Check the code quality with ruff."""
    ctx.run(
        gated("ruff", "check", "--config", "pyproject.toml", "--no-fix", *PY_SRC_LIST, name="ruff"),
        title=pyprefix("code quality check"),
    )


@duty
def format(ctx: Context) -> None:  # noqa: A001
    """Check the code formatting with ruff."""
    ctx.run(
        gated(
            "ruff", "format", "--config", "pyproject.toml", "--check", *PY_SRC_LIST, name="format"
        ),
        title=pyprefix("code formatting"),
    )


@duty
def ty(ctx: Context) -> None:
    """Check the code with ty."""
    ctx.run(
        gated(
            "ty",
            "check",
            "src/",
            name="ty",
            paths=("src/", "pyproject.toml", "uv.lock", "duties.py"),
        ),
        title=pyprefix("ty check"),
    )


@duty
def typos(ctx: Context) -> None:
    """Check the code with typos."""
    ctx.run(gated("typos", "--config", ".typos.toml", name="typos"), title=pyprefix("typos check"))


@duty
def yamllint(ctx: Context) -> None:
    """Check YAML files with yamllint."""
    ctx.run(
        gated("yamllint", "--strict", "--config-file", ".yamllint.yml", ".", name="yamllint"),
        title=pyprefix("yamllint check"),
    )


@duty(skip_if=not SH_SRC_LIST, skip_reason="no shell scripts to check")
def shellcheck(ctx: Context) -> None:
    """Check shell scripts with shellcheck."""
    ctx.run(
        gated(
            "shellcheck",
            "--check-sourced",
            "--severity=warning",
            *SH_SRC_LIST,
            name="shellcheck",
            paths=("bin/", "duties.py"),
        ),
        title=pyprefix("shellcheck"),
    )


@duty(skip_if=CI, skip_reason="skip prek in CI environments")
def prek(ctx: Context) -> None:
    """Run prek hooks."""
    # Skip the hooks that call a lint duty, since `lint` runs those duties itself
    ctx.run(
        gated(
            "env",
            f"PREK_SKIP={','.join(PREK_DUTY_HOOKS)}",
            "prek",
            "run",
            "--all-files",
            name="prek",
        ),
        title=pyprefix("prek hooks"),
    )


@duty(pre=[ruff, format, ty, typos, yamllint, shellcheck, prek], capture=CI)
def lint(ctx: Context) -> None:
    """Run all linting duties."""


@duty(capture=CI)
def update(ctx: Context) -> None:
    """Update the project."""
    ctx.run(["uv", "lock", "--upgrade"], title="update uv lock")
    ctx.run(["uv", "sync"], title="update uv sync")
    ctx.run(["prek", "autoupdate"], title="prek autoupdate")
    ctx.run(["uvx", "uv-upx", "upgrade", "run"], title="uv-upx upgrade")


@duty()
def test(ctx: Context, *cli_args: str) -> None:
    """Test package and generate coverage reports."""
    command = (
        "pytest",
        "tests/",
        "-c",
        "pyproject.toml",
        "--cov",
        "--cov-config=pyproject.toml",
        "--cov-report=xml",
        "--cov-report=term",
        *cli_args,
    )
    # A filtered run must never skip the full suite, so only the bare suite is gated. The
    # interpreter is not in the tree, so the key keeps one version's pass from skipping another's.
    python = f"python{sys.version_info.major}.{sys.version_info.minor}"
    ctx.run(
        list(command) if cli_args else gated(*command, name=f"test@{python}", paths=TEST_PATHS),
        title=pyprefix("Running tests - this may take a while"),
        capture=CI,
    )
