"""Tests that the agent guide and prompt state only what the CLI and plugin really do.

An agent runs what the guide names verbatim, so each fact the guide copies from code is
checked against that code here.
"""

from __future__ import annotations

import re
import tomllib
import typing
from pathlib import Path
from typing import Literal

import pytest
import typer
from typer.core import TyperGroup, TyperOption

from sessionmemory.cli import app
from sessionmemory.lib import agentguide, doctor, field

COMMAND_SPAN = re.compile(r"`sessionmemory ([^`]+)`")
SETTINGS_ROW = re.compile(r"^\| `([a-z_]+)\.([a-z_]+)` +\| `?([^`|]+?)`? +\|", re.MULTILINE)
SETTINGS_EXAMPLE = Path(__file__).parents[2] / "hooks" / "sessionmemory.toml.example"
ROOT_COMMAND = typer.main.get_command(app)


def _choices(param: TyperOption) -> list[str] | None:
    """Return the values an option accepts when they are a fixed `Literal`, else None."""
    annotation = param.type_descriptor.annotation
    if typing.get_origin(annotation) is not Literal:
        annotation = next(
            (arg for arg in typing.get_args(annotation) if typing.get_origin(arg) is Literal),
            None,
        )
    return None if annotation is None else [str(value) for value in typing.get_args(annotation)]


def _unknown_words(invocation: str) -> list[str]:
    """Walk an invocation down the command tree and return each word the CLI does not know.

    A placeholder such as `<dir>` or `"<words>"` is skipped, and a placeholder where a
    subcommand belongs ends the walk. A subcommand of a group, a word that starts with
    `--`, and each `|`-separated value given to an option with fixed choices are checked.
    """
    command = ROOT_COMMAND
    unknown: list[str] = []
    choices: list[str] | None = None
    for word in invocation.split():
        if choices is not None:
            if not word.startswith("<"):
                unknown.extend(value for value in word.split("|") if value not in choices)
            choices = None
            continue
        if isinstance(command, TyperGroup) and word.startswith("<"):
            break
        if word.startswith("--"):
            param = next((p for p in command.params if word in p.opts), None)
            if param is None:
                unknown.append(word)
            else:
                choices = _choices(param) if isinstance(param, TyperOption) else None
        elif isinstance(command, TyperGroup) and word[0].isalpha():
            if word not in command.commands:
                unknown.append(word)
                break
            command = command.commands[word]
    return unknown


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(agentguide.skill(), id="skill"),
        pytest.param(agentguide.PROMPT, id="prompt"),
    ],
)
def test_agent_text_names_only_real_commands_and_options(text):
    """Keep the guide in step with the CLI, since an agent runs what it names verbatim."""
    invocations = COMMAND_SPAN.findall(text)
    assert invocations

    unknown = {inv: words for inv in invocations if (words := _unknown_words(inv))}

    assert unknown == {}


def test_agent_skill_covers_every_doctor_check_and_the_page_limit():
    """Name every check doctor reports and the real page limit, so no finding is unexplained."""
    guide = agentguide.skill()

    missing = [name for name in doctor.CHECK_NAMES if f"| `{name}`" not in guide]

    assert missing == []
    assert str(field.PAGE_LIMIT) in guide


def test_agent_skill_names_only_real_settings():
    """Name only settings keys the plugin reads, with the defaults its example file lists."""
    example = tomllib.loads(SETTINGS_EXAMPLE.read_text(encoding="utf-8"))
    rows = SETTINGS_ROW.findall(agentguide.skill())
    assert rows

    unknown = [(s, k) for s, k, _ in rows if k not in example.get(s, {})]
    wrong_default = [
        (s, k, default)
        for s, k, default in rows
        if (s, k) not in unknown and default != "unset" and str(example[s][k]).lower() != default
    ]

    assert unknown == []
    assert wrong_default == []
