"""The sweep prompt must ask for exactly the variables the sweep renders."""

from __future__ import annotations

import re
from pathlib import Path

PROMPTS = Path(__file__).resolve().parents[2] / "hooks" / "prompts"

RENDERED = {
    "capture_criteria",
    "vault_cli",
    "repo",
    "log_command",
    "nothing_sentinel",
    "changes",
    "existing_log",
    "existing_memory",
    "git_context",
    "transcript",
}


def test_every_placeholder_is_rendered():
    """Verify the set of `{{name}}` placeholders in sweep.md matches exactly what `_run_job` renders."""
    text = (PROMPTS / "sweep.md").read_text(encoding="utf-8")

    assert set(re.findall(r"\{\{(\w+)\}\}", text)) == RENDERED


def test_the_prompt_never_names_a_command_that_does_not_exist():
    """Verify neither prompt file names a flag, command, or concept the CLI no longer has."""
    for name in ("sweep.md", "_capture-criteria.md"):
        text = (PROMPTS / name).read_text(encoding="utf-8")
        for forbidden in (
            "--tag",
            "--read-when",
            "--project",
            " status ",
            "global learning",
            "[[",
            "new backlog",
            "--size",
            "--kind",
        ):
            assert forbidden not in text, f"{forbidden!r} in {name}"
