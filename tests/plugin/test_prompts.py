"""The sweep prompt must ask for exactly the variables the sweep renders."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sessionhooks import sweep as sweep_mod  # ty: ignore[unresolved-import]

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
    "project_dir",
}


def test_every_placeholder_is_rendered():
    """Verify the set of `{{name}}` placeholders in sweep.md matches exactly what `Sweep.compose` renders."""
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

    grep_lines = [line for line in text.splitlines() if "grep -rnis" in line]
    assert len(grep_lines) == 1
    for doc in ("CLAUDE.md", "AGENTS.md", "README.md", "docs/"):
        assert f"{{{{repo}}}}/{doc}" in grep_lines[0]
    assert text.index("grep -rnis") < command


def test_capture_criteria_carries_the_value_test():
    """Verify the sweep prompt makes a candidate prove its value before a page is written."""
    text = " ".join(_rendered_prompt().split())

    assert "trivial to remediate" in text
    assert "rare but severe" in text


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


def test_the_prompt_keeps_spec_status_current():
    """Verify the sweep prompt maintains spec status and never edits a spec's body."""
    text = " ".join(_rendered_prompt().split())

    assert "status: shipped" in text
    assert "superseded_by" in text
    assert "never change a spec's body" in text.lower()
    assert "When the spec has no frontmatter block, add one holding only the keys you set." in text
    assert "four jobs" in text


def test_the_prompt_limits_spec_edits_in_its_hard_rules():
    """Verify the hard rules confine spec edits to the status, superseded_by, and summary keys."""
    text = " ".join(_rendered_prompt().split())
    hard_rules = text.split("## Hard rules", 1)[1].split("## First decide", 1)[0]

    assert "Never change a spec's body" in hard_rules
    assert "`status`, `superseded_by`, and `summary`" in hard_rules


def _render(*, manual: bool) -> str:
    """Render sweep.md for one kind of run, every placeholder filled with a marker value."""
    return sweep_mod._render_template(
        sweep_mod.PROMPT_PATH, manual=manual, **{name: f"<{name}-value>" for name in RENDERED}
    )


@pytest.mark.parametrize("manual", [False, True])
def test_render_template_leaves_no_markers_or_placeholders(manual: bool) -> None:
    """Verify neither kind of run sees a block marker or an unfilled placeholder."""
    text = _render(manual=manual)

    assert "<!--" not in text
    assert "{{" not in text


def test_render_template_headless_carries_the_transcript() -> None:
    """Verify the worker's prompt holds the transcript and warns that it is untrusted."""
    text = _render(manual=False)

    assert "<transcript>\n<transcript-value>\n</transcript>" in text
    assert "UNTRUSTED DATA" in text
    assert "<project_dir-value>" not in text


def test_render_template_manual_names_the_folder_and_omits_the_transcript() -> None:
    """Verify a manual sweep's prompt names the project folder and carries no transcript."""
    text = _render(manual=True)

    assert "<transcript" not in text
    assert "UNTRUSTED DATA" not in text
    assert "<project_dir-value>" in text
    assert "read this conversation" in text


@pytest.mark.parametrize("manual", [False, True])
def test_sweep_never_touches_local_entries(manual: bool) -> None:
    """Verify both kinds of sweep are told to leave `local/` and CLAUDE.local.md alone."""
    flat = " ".join(_render(manual=manual).split())

    assert (
        "Never write, move, or delete anything under `local/`, or a `CLAUDE.local.md` at "
        "the top of this project's folder." in flat
    )


@pytest.mark.parametrize("manual", [False, True])
def test_sweep_intro_leaves_local_out_of_what_it_may_edit(manual: bool) -> None:
    """Verify the opening grant of what the sweep may edit agrees with the local/ hard rule."""
    flat = " ".join(_render(manual=manual).split())

    assert "except `local/` and a top-level `CLAUDE.local.md`" in flat
