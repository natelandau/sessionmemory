"""Tests for committing the vault from a hook."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from sessionhooks import commit as commit_module  # ty: ignore[unresolved-import]
from sessionhooks.commit import (  # ty: ignore[unresolved-import]
    CommitOutcome,
    commit_vault,
    is_repository,
    is_safe_to_commit,
    message,
)

from tests._env import clean_environ

if TYPE_CHECKING:
    from pathlib import Path


def _git(*args, cwd) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repo(tmp_path) -> Path:
    root = tmp_path / "vault"
    root.mkdir(parents=True)
    _git("init", "-q", ".", cwd=root)
    _git("config", "user.email", "t@example.org", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=root)
    return root


def test_commit_vault_commits_a_dirty_tree(tmp_path):
    """Verify a new file is committed and the short sha comes back."""
    root = _repo(tmp_path)
    (root / "page.md").write_text("x", encoding="utf-8")

    outcome = commit_vault(root, env=clean_environ())

    assert outcome.sha
    assert outcome.describe() == f"committed {outcome.sha}"
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=root, capture_output=True, text=True, check=True
    )
    assert "checkpoint" not in log.stdout.splitlines()[0]


def test_commit_vault_clean_tree_is_skipped(tmp_path):
    """Verify nothing is committed when nothing changed."""
    assert commit_vault(_repo(tmp_path), env=clean_environ()) == CommitOutcome(reason="clean")


def test_commit_vault_excludes_the_derived_index(tmp_path):
    """Verify a project's .sqlite3 index never enters git, even with an empty .gitignore."""
    # Given a repo with an empty .gitignore, a page, and a derived index sitting beside it
    root = _repo(tmp_path)
    (root / ".gitignore").write_text("", encoding="utf-8")
    learnings = root / "projects" / "demo" / "learnings"
    learnings.mkdir(parents=True)
    (learnings / "page.md").write_text("x", encoding="utf-8")
    (learnings / "stub.sqlite3").write_text("not real sqlite bytes", encoding="utf-8")

    # When committing the vault
    commit_vault(root, env=clean_environ())

    # Then the page is tracked and the index is not
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True
    ).stdout
    assert "projects/demo/learnings/page.md" in tracked
    assert "stub.sqlite3" not in tracked


def test_commit_vault_outside_a_repository_is_skipped(tmp_path):
    """Verify a plain directory is left alone."""
    assert commit_vault(tmp_path, env=clean_environ()) == CommitOutcome(reason="not a repository")
    assert is_repository(tmp_path, env=clean_environ()) is False


def test_commit_vault_ignores_an_ambient_git_dir(tmp_path):
    """Verify a leaked GIT_DIR cannot redirect the commit at another repository."""
    root = _repo(tmp_path)
    other = _repo(tmp_path / "other")
    (root / "page.md").write_text("x", encoding="utf-8")
    env = {**clean_environ(), "GIT_DIR": str(other / ".git")}

    commit_vault(root, env=env)

    other_log = subprocess.run(
        ["git", "log", "--oneline"], cwd=other, capture_output=True, text=True, check=True
    )
    assert "checkpoint" not in other_log.stdout


def test_commit_vault_skips_a_merge_in_progress(tmp_path):
    """Verify no commit lands during a merge conflict."""
    root = _repo(tmp_path)
    (root / "page.md").write_text("x", encoding="utf-8")
    (root / ".git" / "MERGE_HEAD").write_text("deadbeef\n", encoding="utf-8")

    assert commit_vault(root, env=clean_environ()) == CommitOutcome(reason="operation in progress")
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=root, capture_output=True, text=True, check=True
    )
    assert len(log.stdout.strip().split("\n")) == 1
    assert (root / "page.md").read_text(encoding="utf-8") == "x"


def test_commit_vault_skips_a_rebase_in_progress(tmp_path):
    """Verify no commit lands during a rebase."""
    root = _repo(tmp_path)
    (root / "page.md").write_text("x", encoding="utf-8")
    (root / ".git" / "rebase-merge").mkdir()

    assert commit_vault(root, env=clean_environ()) == CommitOutcome(reason="operation in progress")
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=root, capture_output=True, text=True, check=True
    )
    assert len(log.stdout.strip().split("\n")) == 1
    assert (root / "page.md").read_text(encoding="utf-8") == "x"


def test_commit_vault_skips_a_detached_head(tmp_path):
    """Verify no commit lands on a detached HEAD."""
    root = _repo(tmp_path)
    (root / "page.md").write_text("x", encoding="utf-8")
    _git("checkout", "-q", "--detach", cwd=root)

    assert commit_vault(root, env=clean_environ()) == CommitOutcome(reason="detached HEAD")
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=root, capture_output=True, text=True, check=True
    )
    assert len(log.stdout.strip().split("\n")) == 1
    assert (root / "page.md").read_text(encoding="utf-8") == "x"


def test_git_swallows_a_subprocess_failure(tmp_path, monkeypatch):
    """Verify a git invocation that cannot even run reads as None, not a raised error."""
    # Given a repo and subprocess.run replaced with one that always raises
    root = _repo(tmp_path)

    def _raise(*_args, **_kwargs) -> None:
        message = "git executable not found"
        raise OSError(message)

    monkeypatch.setattr(commit_module.subprocess, "run", _raise)

    # When running a git command through the low-level helper
    result = commit_module._git(root, "status", env=clean_environ(), timeout=5)

    # Then the failure reads as None rather than raising
    assert result is None


def test_is_safe_to_commit_outside_a_repository_is_false(tmp_path):
    """Verify a plain directory is never judged safe to commit."""
    # Given a plain directory that is not a git repository
    # When checking whether it is safe to commit
    # Then it is never judged safe
    assert is_safe_to_commit(tmp_path, env=clean_environ()) is False


def test_commit_vault_reports_a_failed_add(tmp_path, monkeypatch):
    """Verify a failed `git add` is not committed over, so a bad stage never lands silently."""
    # Given a dirty repo whose `git add` is rigged to fail
    root = _repo(tmp_path)
    (root / "page.md").write_text("x", encoding="utf-8")
    real_git = commit_module._git

    def _fake_git(root_, *args, env, timeout=10, **kwargs) -> subprocess.CompletedProcess | None:
        if args and args[0] == "add":
            return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="boom")
        return real_git(root_, *args, env=env, timeout=timeout, **kwargs)

    monkeypatch.setattr(commit_module, "_git", _fake_git)

    # When committing the vault
    result = commit_vault(root, env=clean_environ())

    # Then nothing is committed and the repo's log gains no new entry
    assert result == CommitOutcome(reason="add failed")
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=root, capture_output=True, text=True, check=True
    )
    assert len(log.stdout.strip().split("\n")) == 1


def test_commit_vault_reports_a_failed_commit(tmp_path, monkeypatch):
    """Verify a failed `git commit` reads as nothing committed, not a raised error."""
    # Given a dirty repo whose `git commit` is rigged to fail
    root = _repo(tmp_path)
    (root / "page.md").write_text("x", encoding="utf-8")
    real_git = commit_module._git

    def _fake_git(root_, *args, env, timeout=10, **kwargs) -> subprocess.CompletedProcess | None:
        if args and args[0] == "commit":
            return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="boom")
        return real_git(root_, *args, env=env, timeout=timeout, **kwargs)

    monkeypatch.setattr(commit_module, "_git", _fake_git)

    # When committing the vault
    result = commit_vault(root, env=clean_environ())

    # Then nothing is committed and the repo's log gains no new entry
    assert result == CommitOutcome(reason="commit failed")
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=root, capture_output=True, text=True, check=True
    )
    assert len(log.stdout.strip().split("\n")) == 1


def test_commit_vault_reports_git_unavailable_when_status_fails(tmp_path, monkeypatch):
    """Verify a `git status` that fails outright is reported as unavailable, not as clean."""
    # Given a dirty repo whose `git status` is rigged to fail
    root = _repo(tmp_path)
    (root / "page.md").write_text("x", encoding="utf-8")
    real_git = commit_module._git

    def _fake_git(root_, *args, env, timeout=10, **kwargs) -> subprocess.CompletedProcess | None:
        if args and args[0] == "status":
            return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="boom")
        return real_git(root_, *args, env=env, timeout=timeout, **kwargs)

    monkeypatch.setattr(commit_module, "_git", _fake_git)

    # When committing the vault
    result = commit_vault(root, env=clean_environ())

    # Then nothing is committed and the failure reads as unavailable, not clean
    assert result == CommitOutcome(reason="git unavailable")


def test_commit_vault_reports_the_sha_as_unavailable_when_rev_parse_fails(tmp_path, monkeypatch):
    """Verify a commit that landed but whose sha could not be read is still reported as committed."""
    # Given a dirty repo whose final `rev-parse --short` is rigged to fail
    root = _repo(tmp_path)
    (root / "page.md").write_text("x", encoding="utf-8")
    real_git = commit_module._git

    def _fake_git(root_, *args, env, timeout=10, **kwargs) -> subprocess.CompletedProcess | None:
        if args[:2] == ("rev-parse", "--short"):
            return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="boom")
        return real_git(root_, *args, env=env, timeout=timeout, **kwargs)

    monkeypatch.setattr(commit_module, "_git", _fake_git)

    # When committing the vault
    result = commit_vault(root, env=clean_environ())

    # Then the commit landed and is reported as such, sha unavailable rather than a failure
    assert result == CommitOutcome(sha=commit_module.SHA_UNAVAILABLE)
    assert result.describe() == "committed (sha unavailable)"
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=root, capture_output=True, text=True, check=True
    )
    assert "checkpoint" not in log.stdout.splitlines()[0]


def test_commit_vault_names_a_lost_index_lock_race(tmp_path, monkeypatch):
    """Verify a commit that lost git's own lock is reported as that, not as a generic failure."""
    root = _repo(tmp_path)
    (root / "page.md").write_text("x", encoding="utf-8")
    real_git = commit_module._git

    def _locked(root_, *args, env, timeout, **kwargs) -> subprocess.CompletedProcess:
        if args[0] == "commit":
            return subprocess.CompletedProcess(
                args=["git", *args],
                returncode=128,
                stdout="",
                stderr="fatal: Unable to create '.git/index.lock': File exists.",
            )
        return real_git(root_, *args, env=env, timeout=timeout, **kwargs)

    monkeypatch.setattr(commit_module, "_git", _locked)
    assert commit_vault(root, env=clean_environ()) == CommitOutcome(reason="index lock")


def test_message_names_the_project_and_what_changed():
    """Verify the subject names the project and counts each kind of change."""
    changes = [
        ("A", "projects/crosstune/logs/a.md"),
        ("A", "projects/crosstune/learnings/b.md"),
        ("A", "projects/crosstune/learnings/c.md"),
        ("M", "projects/crosstune/backlog.md"),
        ("M", "projects/crosstune/specs/d.md"),
    ]

    subject, _ = message(changes)

    assert subject == "crosstune: add 1 log, 2 learnings; edit backlog, 1 spec"


def test_message_for_the_registry_alone():
    """Verify a change under _system is grouped as the vault."""
    subject, _ = message([("M", "_system/registry.toml")])

    assert subject == "vault: edit 1 file"


def test_message_for_two_projects():
    """Verify several projects are named and the changes only counted."""
    changes = [
        ("A", "projects/crosstune/logs/a.md"),
        ("M", "projects/sessionmemory/backlog.md"),
        ("D", "projects/sessionmemory/learnings/x.md"),
    ]

    subject, _ = message(changes)

    assert subject == "crosstune, sessionmemory: update 3 files"


def test_message_subject_stays_within_70_characters():
    """Verify a long subject is cut at a separator and ends with an ellipsis."""
    base = "projects/crosstune"
    changes = [
        ("A", f"{base}/logs/a.md"),
        ("A", f"{base}/learnings/a.md"),
        ("A", f"{base}/specs/a.md"),
        ("A", f"{base}/plans/a.md"),
        ("A", f"{base}/runbooks/a.md"),
        ("A", f"{base}/reference/a.md"),
        ("M", f"{base}/logs/b.md"),
        ("M", f"{base}/learnings/b.md"),
        ("M", f"{base}/specs/b.md"),
        ("D", f"{base}/plans/b.md"),
        ("D", f"{base}/runbooks/b.md"),
        ("D", f"{base}/reference/b.md"),
    ]

    subject, _ = message(changes)

    assert len(subject) <= 70
    assert subject.endswith("...")
    assert subject.startswith("crosstune: add 1 log")


def test_message_body_lists_every_path_and_opens_with_the_note():
    """Verify the body opens with the note and lists one line per path."""
    changes = [
        ("A", "projects/crosstune/logs/a.md"),
        ("R", "projects/crosstune/specs/d.md"),
        ("D", "projects/crosstune/learnings/b.md"),
    ]

    _, body = message(changes, note="Made the handler idempotent")

    assert body.splitlines() == [
        "Made the handler idempotent",
        "",
        "add: projects/crosstune/logs/a.md",
        "edit: projects/crosstune/specs/d.md",
        "delete: projects/crosstune/learnings/b.md",
    ]


def test_commit_vault_writes_the_built_message(tmp_path):
    """Verify the commit subject names the project and the body lists the paths."""
    root = _repo(tmp_path)
    page = root / "projects" / "crosstune" / "learnings" / "a.md"
    page.parent.mkdir(parents=True)
    page.write_text("x", encoding="utf-8")

    commit_vault(root, env=clean_environ(), note="Did a thing")

    log = subprocess.run(
        ["git", "log", "-1", "--format=%s%n%b"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    assert log.stdout.splitlines()[0] == "crosstune: add 1 learning"
    assert "checkpoint" not in log.stdout
    assert "Did a thing" in log.stdout
    assert "add: projects/crosstune/learnings/a.md" in log.stdout


def test_commit_vault_works_in_a_nested_vault(tmp_path):
    """Verify a vault inside a larger repository still groups by project."""
    repo = _repo(tmp_path)
    vault = repo / "notes" / "vault"
    page = vault / "projects" / "crosstune" / "logs" / "a.md"
    page.parent.mkdir(parents=True)
    page.write_text("x", encoding="utf-8")

    outcome = commit_vault(vault, env=clean_environ())

    assert outcome.sha
    log = subprocess.run(
        ["git", "log", "-1", "--format=%s%n%b"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert log.stdout.splitlines()[0] == "crosstune: add 1 log"
    assert "add: notes/vault/projects/crosstune/logs/a.md" in log.stdout


def test_message_orders_nouns_whatever_order_git_lists_paths():
    """Verify git's path-sorted input still reads log, learning, backlog, spec."""
    changes = [
        ("M", "projects/crosstune/backlog.md"),
        ("A", "projects/crosstune/learnings/b.md"),
        ("A", "projects/crosstune/learnings/c.md"),
        ("A", "projects/crosstune/logs/a.md"),
        ("M", "projects/crosstune/specs/d.md"),
    ]

    subject, _ = message(changes)

    assert subject == "crosstune: add 1 log, 2 learnings; edit backlog, 1 spec"


def test_message_for_a_project_beside_the_vault():
    """Verify _system paths stay out of the subject but appear in the body."""
    changes = [
        ("A", "projects/crosstune/logs/a.md"),
        ("M", "_system/registry.toml"),
    ]

    subject, body = message(changes)

    assert subject == "crosstune: add 1 log"
    assert "edit: _system/registry.toml" in body


def test_message_for_two_projects_beside_the_vault_does_not_list_it():
    """Verify the vault is not named among several projects but is counted."""
    changes = [
        ("A", "projects/a/logs/x.md"),
        ("A", "projects/b/logs/x.md"),
        ("M", "_system/registry.toml"),
    ]

    subject, body = message(changes)

    assert subject == "a, b: update 3 files"
    assert len(body.splitlines()) == 3


def test_commit_vault_skips_when_only_the_derived_index_changed(tmp_path):
    """Verify an untracked .sqlite3 alone is reported as clean rather than raising."""
    root = _repo(tmp_path)
    (root / "index.sqlite3").write_text("x", encoding="utf-8")

    assert commit_vault(root, env=clean_environ()) == CommitOutcome(reason="clean")
