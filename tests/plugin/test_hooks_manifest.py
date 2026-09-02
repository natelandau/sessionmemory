"""Manifest/wiring validation for the sessionmemory plugin's hooks."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PREFIX = "${CLAUDE_PLUGIN_ROOT}/"

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _parse_frontmatter(text: str) -> dict[str, str] | None:
    """Extract top-level key/value pairs from a YAML frontmatter block.

    Intentionally minimal: handles the flat `key: value` shape used by the skill
    in this plugin, plus indented continuation lines. Avoids a PyYAML dep so the
    test suite stays light.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None
    fields: dict[str, str] = {}
    current_key: str | None = None
    for line in match.group(1).splitlines():
        if not line.strip():
            continue
        # Indented continuation appends to the previous key's value.
        if line[:1].isspace() and current_key is not None:
            fields[current_key] = f"{fields[current_key]} {line.strip()}"
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            current_key = key.strip()
            fields[current_key] = value.strip()
    return fields


def _skill_files() -> list[Path]:
    return sorted((ROOT / "skills").glob("*/SKILL.md"))


def _agent_files() -> list[Path]:
    return sorted((ROOT / "agents").glob("*.md"))


def _hooks_commands() -> list[tuple[str, str]]:
    data = json.loads((ROOT / "hooks" / "hooks.json").read_text())
    return [
        (event, h["command"])
        for event, entries in data.get("hooks", {}).items()
        for entry in entries
        for h in entry.get("hooks", [])
        if h.get("type") == "command"
    ]


def test_skill_agent_and_hook_command_counts() -> None:
    """Verify the manifest still names every skill, agent, and hook command it should.

    Every other test in this module is a `@pytest.mark.parametrize` over one of
    these three lists, collected at import time. Deleting a directory the glob
    walks (`skills/`, say) would leave its parametrized tests with zero cases,
    which pytest reports as passing rather than as a loss of coverage.
    """
    assert len(_skill_files()) == 4
    assert len(_agent_files()) == 4
    assert len(_hooks_commands()) == 3


def test_plugin_manifest_fields() -> None:
    """Verify plugin.json declares name and description."""
    # Given the manifest
    data = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    # Then required fields exist
    assert data.get("name") == "sessionmemory"
    assert "description" in data


@pytest.mark.parametrize(
    ("event", "command"), _hooks_commands(), ids=lambda v: v if isinstance(v, str) else ""
)
def test_command_resolves_and_is_executable(event: str, command: str) -> None:
    """Verify every hooks.json command points at an executable script."""
    # Given a registered command
    assert command.startswith(PREFIX), command
    # When resolved
    resolved = ROOT / command[len(PREFIX) :]
    # Then it exists and is executable
    assert resolved.is_file(), resolved
    assert os.access(resolved, os.X_OK), resolved


@pytest.mark.parametrize("skill_path", _skill_files(), ids=lambda p: p.parent.name)
def test_skill_has_required_frontmatter(skill_path: Path) -> None:
    """Verify every SKILL.md declares name and description frontmatter."""
    # Given a skill entry file
    # When the frontmatter is parsed
    fields = _parse_frontmatter(skill_path.read_text())

    # Then frontmatter is present with name and description fields
    assert fields is not None, f"{skill_path}: missing YAML frontmatter"
    assert "name" in fields, f"{skill_path}: frontmatter missing 'name'"
    assert "description" in fields, f"{skill_path}: frontmatter missing 'description'"


@pytest.mark.parametrize("skill_path", _skill_files(), ids=lambda p: p.parent.name)
def test_skill_name_matches_directory(skill_path: Path) -> None:
    """Verify SKILL.md frontmatter 'name' equals the containing directory."""
    # Given a skill entry file
    fields = _parse_frontmatter(skill_path.read_text())

    # Then the declared name matches the directory it lives in
    assert fields is not None, f"{skill_path}: missing YAML frontmatter"
    assert fields.get("name") == skill_path.parent.name, (
        f"{skill_path}: frontmatter name {fields.get('name')!r} "
        f"!= directory {skill_path.parent.name!r}"
    )


@pytest.mark.parametrize("agent_path", _agent_files(), ids=lambda p: p.stem)
def test_agent_has_required_frontmatter(agent_path: Path) -> None:
    """Verify every agents/*.md declares name and description in frontmatter."""
    # Given a subagent definition
    fields = _parse_frontmatter(agent_path.read_text())

    # Then frontmatter parses with both required fields
    assert fields is not None, f"{agent_path}: missing YAML frontmatter"
    assert "name" in fields, f"{agent_path}: frontmatter missing 'name'"
    assert "description" in fields, f"{agent_path}: frontmatter missing 'description'"


@pytest.mark.parametrize("agent_path", _agent_files(), ids=lambda p: p.stem)
def test_agent_name_matches_filename(agent_path: Path) -> None:
    """Verify agents/*.md frontmatter 'name' equals the file stem used to dispatch it."""
    # Given a subagent definition
    fields = _parse_frontmatter(agent_path.read_text())
    assert fields is not None  # covered by the frontmatter test

    # Then frontmatter name aligns with the on-disk slug
    assert fields.get("name") == agent_path.stem, (
        f"{agent_path}: frontmatter name {fields.get('name')!r} != file stem {agent_path.stem!r}"
    )
