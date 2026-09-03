"""Tests that machine readable output stays unstyled in a shell that forces color.

These run the CLI in a real subprocess rather than through `CliRunner`. Rich decides
whether it may emit color when the console is built, which happens at import, so an
in-process test that sets `FORCE_COLOR` afterwards is deciding nothing: it would pass
before the fix as well as after it. The environment has to be set before the process
starts, the way a shell sets it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from sessionmemory.lib.bootstrap import initialize

ESCAPE = b"\x1b["

# argv[0] would be the console script in real use, but running the entry point through
# the interpreter needs no assumption about where the script was installed.
CLI = [sys.executable, "-c", "from sessionmemory.cli import main; main()"]

# What the repository owner's shell exports. FORCE_COLOR alone is not enough: rich also
# needs a terminal type it can name a color system for.
COLORED = {"FORCE_COLOR": "3", "CLICOLOR": "1", "COLORTERM": "truecolor", "TERM": "xterm-256color"}


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[bytes]:
    """Invoke the CLI in a subprocess and return its raw bytes.

    Args:
        env (dict[str, str]): The environment to start the process with.
        *args: Arguments passed to the CLI.

    Returns:
        subprocess.CompletedProcess[bytes]: The finished process, output undecoded.
    """
    return subprocess.run([*CLI, *args], capture_output=True, env=env, check=False)


@pytest.fixture
def colored(tmp_path):
    """Build a vault and a registered project, and force color the way a shell does.

    The registration output is asserted to carry escapes, which is what proves the
    environment really is forcing color. Without that, every assertion below could pass
    for the trivial reason that nothing was ever styled.
    """
    vault = tmp_path / "vault"
    initialize(vault)
    project = tmp_path / "a-project-with-a-deliberately-long-directory-name"
    project.mkdir()

    env = {
        **os.environ,
        **COLORED,
        "SESSIONMEMORY_VAULT": str(vault),
        "SESSIONMEMORY_EMBEDDER": "stub",
    }
    registered = _run(env, "project", "--register", "--cwd", str(project))
    assert registered.returncode == 0
    assert ESCAPE in registered.stdout

    return env, project


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param(["project", "--json"], id="project"),
        pytest.param(
            ["new", "learning", "--title", "t", "--summary", "s", "--json"], id="new-learning"
        ),
        pytest.param(["search", "anything", "--json"], id="search"),
        pytest.param(["inject", "--json"], id="inject"),
        pytest.param(["reindex", "--json"], id="reindex"),
        pytest.param(["log", "--session-id", "s", "--title", "t", "--json"], id="log"),
    ],
)
def test_json_output_is_parseable_in_a_colored_shell(colored, arguments):
    """Emit JSON a caller can parse even where the environment demands color.

    `--json` exists so an agent can read the id and path of the note it just wrote rather
    than scrape the prose line, so one escape sequence in the payload defeats the flag.
    """
    env, project = colored
    result = _run(env, *arguments, "--cwd", str(project))

    assert result.returncode == 0
    assert ESCAPE not in result.stdout
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict | list)


def test_version_is_unstyled_in_a_colored_shell(colored):
    """Emit `--version` bare, since the plugin's hooks parse it to pick a CLI."""
    env, _project = colored
    result = _run(env, "--version")

    assert result.returncode == 0
    assert ESCAPE not in result.stdout
    assert result.stdout.strip().count(b".") == 2


def test_doctor_json_output_is_parseable_in_a_colored_shell(colored):
    """Emit `doctor --json` unstyled, even though it takes no `--cwd` to route through.

    `doctor` reports across the whole vault rather than one project, so it has no
    `--cwd` option to share with the other commands in the parametrized case above.
    """
    env, _project = colored
    result = _run(env, "doctor", "--json")

    assert result.returncode == 0
    assert ESCAPE not in result.stdout
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict | list)


def test_register_json_output_is_parseable_in_a_colored_shell(tmp_path):
    """Emit --register's own --json payload parseable, even where color is forced.

    `_register` takes `as_json` straight from `project_command`, and this write path has
    no other `--json` coverage.
    A fresh, unregistered directory is used rather than the `colored` fixture's project,
    since that one is already registered and a second `--register` would refuse instead
    of writing.
    """
    vault = tmp_path / "vault"
    initialize(vault)
    project = tmp_path / "a-project-not-yet-registered"
    project.mkdir()
    env = {**os.environ, **COLORED, "SESSIONMEMORY_VAULT": str(vault)}

    result = _run(env, "project", "--register", "--cwd", str(project), "--json")

    assert result.returncode == 0
    assert ESCAPE not in result.stdout
    payload = json.loads(result.stdout)
    assert payload["registered"] is True


def test_prose_for_a_person_keeps_its_styling(colored):
    """Leave human output styled, so the fix is scoped to what a caller parses."""
    env, project = colored
    result = _run(env, "project", "--register", "--cwd", str(project))

    assert result.returncode != 0
    assert ESCAPE in result.stdout + result.stderr


def test_inject_and_search_prose_stay_unstyled_and_unwrapped(colored):
    """Emit `inject` and `search` prose unstyled, with a long path kept on one line.

    Both go through `emit_value`, which a caller redirects or parses whole rather than
    reads on a screen, so an escape sequence or a soft-wrapped path defeats them the
    same way it would defeat `--json`. The `colored` fixture's project directory has a
    deliberately long name, which pushes the page path past a terminal's default width.
    """
    env, project = colored
    created = _run(
        env,
        "new",
        "learning",
        "--title",
        "wok maintenance",
        "--summary",
        "s",
        "--cwd",
        str(project),
        "--json",
    )
    assert created.returncode == 0
    page_path = json.loads(created.stdout)["path"]

    inject_result = _run(env, "inject", "--cwd", str(project))
    assert inject_result.returncode == 0
    assert ESCAPE not in inject_result.stdout

    search_result = _run(env, "search", "wok", "--max-distance", "2", "--cwd", str(project))
    assert search_result.returncode == 0
    assert ESCAPE not in search_result.stdout
    text = search_result.stdout.decode()
    assert page_path in text
    # A path this long is one Rich would soft-wrap unless emit_value disables that;
    # finding it whole on its own line proves the newline is not embedded mid-path.
    assert any(line.strip() == page_path for line in text.splitlines())
