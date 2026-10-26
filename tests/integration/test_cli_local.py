"""Tests for the local command group."""

from __future__ import annotations

import json
import subprocess
import types
from typing import TYPE_CHECKING, NoReturn

import pytest
from typer.testing import CliRunner

from sessionmemory.cli import app
from sessionmemory.commands import local as local_commands
from sessionmemory.lib import localmd

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


def _git(*args, cwd) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def workspace(vault, tmp_path, monkeypatch):
    """Build a vault plus a registered repository, and chdir into the repository."""
    repo = tmp_path / "demo"
    repo.mkdir()
    _git("init", "-q", ".", cwd=repo)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
    _git("remote", "add", "origin", "git@example.org:nate/demo.git", cwd=repo)
    assert runner.invoke(app, ["project", "--register", "--cwd", str(repo)]).exit_code == 0
    monkeypatch.chdir(repo)
    monkeypatch.setattr(local_commands, "_interactive", lambda: False)
    return vault, repo


def _local(vault: Path) -> Path:
    return vault / "projects" / "demo" / "local"


def _vault_md(vault: Path, text: str = "notes\n") -> Path:
    target = _local(vault) / "CLAUDE.local.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    return target


def _vault_skill(vault: Path, name: str) -> Path:
    folder = _local(vault) / "claude" / "skills" / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"# {name}\n")
    return folder


def _checkout_skill(repo: Path, name: str) -> Path:
    folder = repo / ".claude" / "skills" / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"# {name}\n")
    return folder


def _checkout_rule(repo: Path, name: str, text: str = "be kind\n") -> Path:
    path = repo / ".claude" / "rules" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _fail_prompt(*_args: object, **_kwargs: object) -> NoReturn:
    msg = "the checklist must not be shown"
    raise AssertionError(msg)


def test_adopt_prints_only_the_vault_path(workspace):
    """Verify adopt by name prints the vault path alone on stdout and links the checkout."""
    vault, repo = workspace
    (repo / "CLAUDE.local.md").write_text("notes\n")

    result = runner.invoke(app, ["local", "adopt", "CLAUDE.local.md"])

    assert result.exit_code == 0
    assert result.stdout == f"{vault}/projects/demo/local/CLAUDE.local.md\n"
    assert (repo / "CLAUDE.local.md").is_symlink()


def test_adopt_warns_on_relative_import(workspace):
    """Verify adopt warns about an import that resolves from the vault copy's folder."""
    _, repo = workspace
    (repo / "CLAUDE.local.md").write_text("see @./sibling.md\n")

    result = runner.invoke(app, ["local", "adopt", "CLAUDE.local.md"])

    assert result.exit_code == 0
    assert "@./sibling.md" in result.stderr
    assert "commit the vault copy" in result.stderr


def test_adopt_when_already_linked_prints_the_path(workspace):
    """Verify a second adopt prints the vault path and exits 0."""
    vault, repo = workspace
    (repo / "CLAUDE.local.md").write_text("notes\n")
    runner.invoke(app, ["local", "adopt", "CLAUDE.local.md"])

    result = runner.invoke(app, ["local", "adopt", "CLAUDE.local.md"])

    assert result.exit_code == 0
    assert result.stdout == f"{vault}/projects/demo/local/CLAUDE.local.md\n"


def test_adopt_without_a_file_is_refused(workspace):
    """Verify adopt exits 1 when the named entry is not in the checkout."""
    result = runner.invoke(app, ["local", "adopt", "CLAUDE.local.md"])

    assert result.exit_code == 1
    assert "no CLAUDE.local.md" in result.stderr


def test_adopt_with_no_candidates_exits_0(workspace, monkeypatch):
    """Verify a bare adopt with nothing waiting exits 0 without prompting."""
    monkeypatch.setattr(local_commands, "_interactive", lambda: True)
    monkeypatch.setattr(local_commands.ask, "choose_multiple_from_list", _fail_prompt)

    result = runner.invoke(app, ["local", "adopt"])

    assert result.exit_code == 0
    assert "nothing" in result.stdout


def test_adopt_checklist_adopts_selected(workspace, monkeypatch):
    """Verify the checklist offers every candidate and adopts the ticked ones."""
    vault, repo = workspace
    (repo / "CLAUDE.local.md").write_text("notes\n")
    _checkout_skill(repo, "a")
    _checkout_skill(repo, "b")
    offered: list[list[str]] = []

    def choose(choices: list[tuple[str, object]], message: str) -> list[object]:
        offered.append([title for title, _ in choices])
        assert message
        return [value for title, value in choices if title in {"CLAUDE.local.md", "skills/b"}]

    monkeypatch.setattr(local_commands, "_interactive", lambda: True)
    monkeypatch.setattr(local_commands.ask, "choose_multiple_from_list", choose)

    result = runner.invoke(app, ["local", "adopt"])

    assert result.exit_code == 0
    assert offered == [["CLAUDE.local.md", "skills/a", "skills/b"]]
    assert result.stdout.splitlines() == [
        str(_local(vault) / "CLAUDE.local.md"),
        str(_local(vault) / "claude" / "skills" / "b"),
    ]
    assert (repo / "CLAUDE.local.md").is_symlink()
    assert (repo / ".claude" / "skills" / "b").is_symlink()
    assert not (repo / ".claude" / "skills" / "a").is_symlink()


def test_adopt_checklist_empty_selection_exits_0(workspace, monkeypatch):
    """Verify ticking nothing, or cancelling, adopts nothing and exits 0."""
    _, repo = workspace
    (repo / "CLAUDE.local.md").write_text("notes\n")
    monkeypatch.setattr(local_commands, "_interactive", lambda: True)
    monkeypatch.setattr(local_commands.ask, "choose_multiple_from_list", lambda *_a, **_k: None)

    result = runner.invoke(app, ["local", "adopt"])

    assert result.exit_code == 0
    assert result.stdout == ""
    assert not (repo / "CLAUDE.local.md").is_symlink()


def test_adopt_off_terminal_lists_candidates_and_exits_1(workspace, monkeypatch):
    """Verify a bare adopt off a terminal never prompts and names the command to run."""
    _, repo = workspace
    _checkout_skill(repo, "a")
    _checkout_rule(repo, "style.md")
    monkeypatch.setattr(local_commands.ask, "choose_multiple_from_list", _fail_prompt)

    result = runner.invoke(app, ["local", "adopt"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "skills/a" in result.stderr
    assert "sessionmemory local adopt skills/a rules/style.md --cwd ." in result.stderr
    assert not (repo / ".claude" / "skills" / "a").is_symlink()


def test_adopt_by_name_continues_after_a_failure(workspace):
    """Verify a failing name is reported and the next name is still adopted."""
    vault, repo = workspace
    _checkout_skill(repo, "good")

    result = runner.invoke(app, ["local", "adopt", "skills/missing", "bogus", "skills/good"])

    assert result.exit_code == 1
    assert "no skills/missing" in result.stderr
    assert "'bogus' is not an entry name" in result.stderr
    assert result.stdout == f"{_local(vault) / 'claude' / 'skills' / 'good'}\n"
    assert (repo / ".claude" / "skills" / "good").is_symlink()


def test_adopt_rule_prints_approval_note(workspace):
    """Verify adopting a rule says Claude Code asks once to allow it as an import."""
    vault, repo = workspace
    _checkout_rule(repo, "style.md")

    result = runner.invoke(app, ["local", "adopt", "rules/style.md"])

    assert result.exit_code == 0
    assert result.stdout == f"{_local(vault) / 'claude' / 'rules' / 'style.md'}\n"
    assert "external import" in result.stderr


def test_adopt_skill_prints_no_approval_note(workspace):
    """Verify a skill alone needs no import approval note."""
    _, repo = workspace
    _checkout_skill(repo, "a")

    result = runner.invoke(app, ["local", "adopt", "skills/a"])

    assert result.exit_code == 0
    assert "external import" not in result.stdout + result.stderr


def test_adopt_reports_a_legacy_conflict(workspace):
    """Verify adopt names both vault copies when the old and new ones differ."""
    vault, repo = workspace
    _vault_md(vault, "new\n")
    legacy = vault / "projects" / "demo" / "CLAUDE.local.md"
    legacy.write_text("old\n")
    _checkout_skill(repo, "a")

    result = runner.invoke(app, ["local", "adopt", "skills/a"])

    assert result.exit_code == 0
    assert str(legacy) in result.stderr
    assert str(_local(vault) / "CLAUDE.local.md") in result.stderr


def test_adopt_moves_a_legacy_copy_first(workspace):
    """Verify adopt moves an old-path vault copy before it looks at the entry."""
    vault, repo = workspace
    legacy = vault / "projects" / "demo" / "CLAUDE.local.md"
    legacy.write_text("notes\n")
    (repo / "CLAUDE.local.md").write_text("notes\n")

    result = runner.invoke(app, ["local", "adopt", "CLAUDE.local.md"])

    assert result.exit_code == 0
    assert not legacy.exists()
    assert (repo / "CLAUDE.local.md").resolve() == (_local(vault) / "CLAUDE.local.md").resolve()


def test_link_json_lists_entries(workspace):
    """Verify link --json lists every vault entry, created then present."""
    vault, repo = workspace
    target = _vault_md(vault)
    skill = _vault_skill(vault, "a")

    first = runner.invoke(app, ["local", "link", "--json"])
    second = runner.invoke(app, ["local", "link", "--json"])

    assert first.exit_code == 0
    assert json.loads(first.stdout) == {
        "entries": [
            {
                "kind": "local-md",
                "name": "CLAUDE.local.md",
                "status": "created",
                "link": str(repo.resolve() / "CLAUDE.local.md"),
                "target": str(target),
            },
            {
                "kind": "skill",
                "name": "skills/a",
                "status": "created",
                "link": str(repo.resolve() / ".claude" / "skills" / "a"),
                "target": str(skill),
            },
        ]
    }
    assert second.exit_code == 0
    assert [e["status"] for e in json.loads(second.stdout)["entries"]] == ["present", "present"]
    assert (repo / "CLAUDE.local.md").is_symlink()
    assert (repo / ".claude" / "skills" / "a").is_symlink()


def test_link_prose_prints_the_link_paths(workspace):
    """Verify link without --json prints each created link path alone."""
    vault, repo = workspace
    _vault_md(vault)
    _vault_skill(vault, "a")

    result = runner.invoke(app, ["local", "link"])

    assert result.exit_code == 0
    assert result.stdout.splitlines() == [
        str(repo.resolve() / "CLAUDE.local.md"),
        str(repo.resolve() / ".claude" / "skills" / "a"),
    ]


def test_link_conflict_exits_1(workspace):
    """Verify a regular file beside a vault copy exits 1, and the other entries still link."""
    vault, repo = workspace
    _vault_md(vault, "vault\n")
    _vault_skill(vault, "a")
    (repo / "CLAUDE.local.md").write_text("local\n")

    result = runner.invoke(app, ["local", "link", "--json"])

    assert result.exit_code == 1
    statuses = {e["name"]: e["status"] for e in json.loads(result.stdout)["entries"]}
    assert statuses == {"CLAUDE.local.md": "conflict", "skills/a": "created"}


def test_link_conflict_in_prose_names_the_reason_and_remedy(workspace):
    """Verify the prose conflict warning says why and how to resolve a differing copy."""
    vault, repo = workspace
    _vault_md(vault, "vault\n")
    (repo / "CLAUDE.local.md").write_text("local\n")

    result = runner.invoke(app, ["local", "link"])

    assert result.exit_code == 1
    assert result.stdout == ""
    flat = " ".join(result.stderr.split())
    assert "differs from the vault copy" in flat
    assert (
        "Copy the newer one over the vault copy, delete the checkout copy, and run local link."
        in flat
    )


def test_link_conflict_for_a_wrong_shape_vault_entry_says_fix_the_vault(workspace):
    """Verify a file among vault skills is reported as the vault's problem, not adopt's."""
    vault, _ = workspace
    skills = _local(vault) / "claude" / "skills"
    skills.mkdir(parents=True)
    (skills / "README.md").write_text("stray\n")

    result = runner.invoke(app, ["local", "link"])

    assert result.exit_code == 1
    flat = " ".join(result.stderr.split())
    assert "is not a folder" in flat
    assert "(none)" not in flat
    assert "local adopt" not in flat


def test_legacy_conflict_wording_matches_in_adopt_and_link(workspace):
    """Verify adopt and link describe differing old and new vault copies the same way."""
    vault, repo = workspace
    _vault_md(vault, "new\n")
    (vault / "projects" / "demo" / "CLAUDE.local.md").write_text("old\n")
    _checkout_skill(repo, "a")

    adopted = runner.invoke(app, ["local", "adopt", "skills/a"])
    linked = runner.invoke(app, ["local", "link"])

    def warning(stderr: str) -> str:
        flat = " ".join(stderr.split())
        start = flat.index(str(vault / "projects" / "demo" / "CLAUDE.local.md"))
        return flat[start : flat.index("run local link.", start)]

    assert warning(adopted.stderr) == warning(linked.stderr)


def test_link_tracked_is_not_a_conflict(workspace):
    """Verify a tracked CLAUDE.local.md is reported as tracked and leaves the exit at 0."""
    vault, repo = workspace
    _vault_md(vault)
    (repo / "CLAUDE.local.md").write_text("committed\n")
    _git("add", "-f", "CLAUDE.local.md", cwd=repo)

    result = runner.invoke(app, ["local", "link", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["entries"][0]["status"] == "tracked"
    assert not (repo / "CLAUDE.local.md").is_symlink()


def test_link_migrates_old_copy_and_repairs_link(workspace):
    """Verify link moves an old-path copy into local/ and replaces the dangling link."""
    vault, repo = workspace
    legacy = vault / "projects" / "demo" / "CLAUDE.local.md"
    legacy.write_text("notes\n")
    (repo / "CLAUDE.local.md").symlink_to(legacy)

    result = runner.invoke(app, ["local", "link", "--json"])

    assert result.exit_code == 0
    entries = json.loads(result.stdout)["entries"]
    assert [(e["name"], e["status"]) for e in entries] == [("CLAUDE.local.md", "created")]
    assert not legacy.exists()
    assert (repo / "CLAUDE.local.md").resolve() == (_local(vault) / "CLAUDE.local.md").resolve()


def test_link_legacy_conflict_names_both_paths(workspace):
    """Verify differing old and new vault copies are a conflict naming both paths."""
    vault, _ = workspace
    target = _vault_md(vault, "new\n")
    legacy = vault / "projects" / "demo" / "CLAUDE.local.md"
    legacy.write_text("old\n")

    result = runner.invoke(app, ["local", "link"])

    assert result.exit_code == 1
    assert str(legacy) in result.stderr
    assert str(target) in result.stderr


def test_link_reports_a_removed_link(workspace):
    """Verify link removes a dangling link into local/ and says so off stdout."""
    vault, repo = workspace
    gone = _local(vault) / "claude" / "skills" / "gone"
    (repo / ".claude" / "skills").mkdir(parents=True)
    (repo / ".claude" / "skills" / "gone").symlink_to(gone)

    result = runner.invoke(app, ["local", "link", "--json"])

    assert result.exit_code == 0
    assert [e["status"] for e in json.loads(result.stdout)["entries"]] == ["removed"]
    assert not (repo / ".claude" / "skills" / "gone").is_symlink()


def test_link_prose_keeps_the_removed_note_off_stdout(workspace):
    """Verify link prose prints only created link paths on stdout beside a removal note."""
    vault, repo = workspace
    _vault_skill(vault, "a")
    (repo / ".claude" / "skills").mkdir(parents=True)
    (repo / ".claude" / "skills" / "gone").symlink_to(_local(vault) / "claude" / "skills" / "gone")

    result = runner.invoke(app, ["local", "link"])

    assert result.exit_code == 0
    assert result.stdout == f"{repo.resolve() / '.claude' / 'skills' / 'a'}\n"
    assert "removed" in result.stderr


def test_link_with_nothing_in_the_vault_exits_0(workspace):
    """Verify link exits 0 with an empty list when the vault holds nothing."""
    result = runner.invoke(app, ["local", "link", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"entries": []}


def test_link_with_nothing_in_prose_prints_nothing_on_stdout(workspace):
    """Verify link prints an info line, not a value, when the vault holds nothing."""
    result = runner.invoke(app, ["local", "link"])

    assert result.exit_code == 0
    assert "holds nothing" in result.stdout
    assert str(workspace[1]) not in result.stdout


def test_status_json_shape(workspace):
    """Verify status --json reports the local folder and every entry from both sides."""
    vault, repo = workspace
    target = _vault_md(vault, "same\n")
    (repo / "CLAUDE.local.md").write_text("same\n")
    _checkout_skill(repo, "a")

    result = runner.invoke(app, ["local", "status", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "local": str(_local(vault)),
        "entries": [
            {
                "kind": "local-md",
                "name": "CLAUDE.local.md",
                "state": "diverged",
                "link": str(repo.resolve() / "CLAUDE.local.md"),
                "target": str(target),
                "ignored": False,
                "tracked": False,
                "match": True,
            },
            {
                "kind": "skill",
                "name": "skills/a",
                "state": "unadopted",
                "link": str(repo.resolve() / ".claude" / "skills" / "a"),
                "target": str(_local(vault) / "claude" / "skills" / "a"),
                "ignored": False,
                "tracked": False,
                "match": None,
            },
        ],
    }


def test_status_json_blocked_entry_has_unknown_ignore(workspace, tmp_path):
    """Verify an entry behind a symlinked .claude folder reports ignored as null."""
    vault, repo = workspace
    _vault_skill(vault, "a")
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "skills").mkdir(parents=True)
    (repo / ".claude").symlink_to(elsewhere)

    result = runner.invoke(app, ["local", "status", "--json"])

    assert result.exit_code == 0
    (only,) = json.loads(result.stdout)["entries"]
    assert only["name"] == "skills/a"
    assert only["ignored"] is None


def test_status_prose_lists_state_and_contents(workspace):
    """Verify status prose gives one line per entry, the contents verdict, and the folder."""
    vault, repo = workspace
    _vault_md(vault, "a\n")
    (repo / "CLAUDE.local.md").write_text("b\n")
    _checkout_skill(repo, "a")

    result = runner.invoke(app, ["local", "status"])

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert "diverged" in lines[0]
    assert "CLAUDE.local.md" in lines[0]
    assert "contents different" in lines[0]
    assert "unadopted" in lines[1]
    assert "skills/a" in lines[1]
    assert lines[-1] == f"local: {_local(vault)}"


def test_status_prose_flags_an_unignored_link(workspace):
    """Verify status flags a linked entry git does not ignore."""
    vault, repo = workspace
    target = _vault_md(vault)
    (repo / "CLAUDE.local.md").symlink_to(target)

    result = runner.invoke(app, ["local", "status"])

    assert result.exit_code == 0
    assert "not ignored" in result.stdout.splitlines()[0]


def test_unregistered_directory_is_refused(vault, tmp_path):
    """Verify a directory no project owns is refused."""
    other = tmp_path / "other"
    other.mkdir()

    result = runner.invoke(app, ["local", "status", "--cwd", str(other)])

    assert result.exit_code == 1


def test_outside_git_is_refused(workspace, monkeypatch):
    """Verify a registered directory outside a git work tree is refused by the checkout."""

    def not_a_work_tree(
        _cwd: object, *_args: str, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=128, stdout="", stderr="fatal")

    monkeypatch.setattr(localmd, "gitinfo", types.SimpleNamespace(run_git=not_a_work_tree))

    result = runner.invoke(app, ["local", "status"])

    assert result.exit_code == 1
    assert "is not inside a git work tree" in result.stderr


def test_status_reports_a_git_failure(workspace, monkeypatch):
    """Verify status exits 1 with a message when git cannot run."""
    _, repo = workspace
    (repo / "CLAUDE.local.md").write_text("notes\n")

    def broken(cwd, *_args) -> None:
        msg = f"git could not run in {cwd}"
        raise localmd.LocalMdError(msg)

    monkeypatch.setattr(localmd, "_git", broken)

    result = runner.invoke(app, ["local", "status"])

    assert result.exit_code == 1
    assert "git could not run" in result.stderr


def test_status_prose_reports_a_tracked_file(workspace):
    """Verify status prose says when git tracks the checkout's file."""
    _, repo = workspace
    (repo / "CLAUDE.local.md").write_text("committed\n")
    _git("add", "-f", "CLAUDE.local.md", cwd=repo)

    result = runner.invoke(app, ["local", "status"])

    assert result.exit_code == 0
    assert "tracked" in result.stdout.splitlines()[0]


def test_adopt_on_a_linked_checkout_restores_the_ignore_entry(workspace):
    """Verify adopt on an already linked checkout prints only the path and ignores the link."""
    vault, repo = workspace
    copy = _vault_md(vault)
    (repo / "CLAUDE.local.md").symlink_to(copy)

    result = runner.invoke(app, ["local", "adopt", "CLAUDE.local.md"])

    assert result.exit_code == 0
    assert result.stdout == f"{copy}\n"
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "CLAUDE.local.md"], cwd=repo, check=False
    )
    assert ignored.returncode == 0


def test_adopt_off_terminal_quotes_names_in_the_command(workspace, monkeypatch):
    """Verify the suggested command shell-quotes an entry name holding a space."""
    _, repo = workspace
    _checkout_rule(repo, "my style.md")
    monkeypatch.setattr(local_commands.ask, "choose_multiple_from_list", _fail_prompt)

    result = runner.invoke(app, ["local", "adopt"])

    assert result.exit_code == 1
    assert "sessionmemory local adopt 'rules/my style.md' --cwd ." in result.stderr


def test_adopt_off_terminal_with_no_candidates_exits_0(workspace, monkeypatch):
    """Verify a bare adopt off a terminal with nothing waiting exits 0 without prompting."""
    monkeypatch.setattr(local_commands.ask, "choose_multiple_from_list", _fail_prompt)

    result = runner.invoke(app, ["local", "adopt"])

    assert result.exit_code == 0
    assert "nothing" in result.stdout


def test_link_prose_warns_about_a_tracked_entry(workspace):
    """Verify link prose warns on stderr that a tracked entry is left alone."""
    vault, repo = workspace
    _vault_md(vault)
    (repo / "CLAUDE.local.md").write_text("committed\n")
    _git("add", "-f", "CLAUDE.local.md", cwd=repo)

    result = runner.invoke(app, ["local", "link"])

    assert result.exit_code == 0
    assert result.stdout == ""
    assert "is tracked by git" in result.stderr


def test_adopt_fails_when_the_legacy_copy_cannot_be_moved(workspace, monkeypatch):
    """Verify an OS error while moving the old vault copy fails the command with a message."""

    def broken(**_kwargs: object) -> None:
        msg = "denied"
        raise PermissionError(msg)

    monkeypatch.setattr(localmd, "migrate_legacy", broken)

    result = runner.invoke(app, ["local", "adopt", "CLAUDE.local.md"])

    assert result.exit_code == 1
    assert "could not move the vault's old CLAUDE.local.md" in result.stderr


def test_status_fails_when_an_entry_cannot_be_read(workspace, monkeypatch):
    """Verify an OS error while reading an entry fails status with a message."""
    _, repo = workspace
    (repo / "CLAUDE.local.md").write_text("notes\n")

    def broken(_item: object) -> None:
        msg = "denied"
        raise PermissionError(msg)

    monkeypatch.setattr(localmd, "state", broken)

    result = runner.invoke(app, ["local", "status"])

    assert result.exit_code == 1
    assert "could not read the entries" in result.stderr


def test_link_reports_an_unreadable_vault_without_a_traceback(workspace, monkeypatch):
    """Verify a filesystem failure while linking exits 1 with a message."""

    def broken(_co: localmd.Checkout) -> list[localmd.LinkResult]:
        msg = "permission denied"
        raise PermissionError(msg)

    monkeypatch.setattr(localmd, "link_all", broken)

    result = runner.invoke(app, ["local", "link"])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "permission denied" in result.stderr
