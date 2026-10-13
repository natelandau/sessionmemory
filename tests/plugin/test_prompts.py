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
        ):
            assert forbidden not in text, f"{forbidden!r} in {name}"


def test_the_prompt_adds_a_backlog_item_through_the_cli():
    """Verify deferred work is written by the command that owns the line format, not by hand."""
    text = (PROMPTS / "sweep.md").read_text(encoding="utf-8")

    assert "{{vault_cli}} new backlog --kind" in text
    assert "create it with a `# Backlog` heading" not in text


def _rendered_prompt() -> str:
    """Return sweep.md with the capture criteria interpolated."""
    text = (PROMPTS / "sweep.md").read_text(encoding="utf-8")
    criteria = (PROMPTS / "_capture-criteria.md").read_text(encoding="utf-8")
    return text.replace("{{capture_criteria}}", criteria)


def test_the_prompt_searches_the_repo_docs_before_a_learning():
    """Verify the prompt names the repo docs and tells the sweep to search them before creating a learning."""
    text = _rendered_prompt()
    command = text.index("new learning")

    for doc in ("CLAUDE.md", "AGENTS.md", "README.md", "docs/"):
        assert doc in text
    assert text.index("grep -rnis") < command
    assert text.rindex("AGENTS.md", 0, command) > 0


def test_the_prompt_does_not_count_a_commit_message_as_a_record():
    """Verify a commit message is not listed among the things that already state a fact."""
    text = " ".join(_rendered_prompt().split())

    assert "or a commit message already states" not in text
    assert "or a commit message that states it" not in text
    assert "only in a commit message is still unrecorded" in text


def test_sweep_prompt_routes_open_questions_to_decide():
    """Verify the deferred-work section names the decide kind and sends user questions under it."""
    text = " ".join(_rendered_prompt().split())

    assert "## decide" in text
    assert "ci, decide" in text
