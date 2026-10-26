"""Tests for committing the vault from a hook."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from sessionhooks import commit as commit_module  # ty: ignore[unresolved-import]
from sessionhooks.commit import (  # ty: ignore[unresolved-import]
    CommitOutcome,
    commit_vault,
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


def _tracked(cwd) -> str:
    return subprocess.run(
        ["git", "ls-files"], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


def _write_plan_and_spec(vault: Path) -> None:
    for relative in ("projects/p/plans/x.md", "projects/p/specs/y.md"):
        page = vault / relative
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text("x", encoding="utf-8")


def test_commit_vault_excludes_plans_even_without_an_ignore_rule(tmp_path):
    """Verify a plan never enters git, even with an empty .gitignore."""
    root = _repo(tmp_path)
    (root / ".gitignore").write_text("", encoding="utf-8")
    _write_plan_and_spec(root)

    commit_vault(root, env=clean_environ())

    tracked = _tracked(root).splitlines()
    assert "projects/p/specs/y.md" in tracked
    assert "projects/p/plans/x.md" not in tracked


def test_commit_vault_excludes_plans_in_a_nested_vault(tmp_path):
    """Verify a plan is excluded when the vault sits inside a larger repository."""
    outer = _repo(tmp_path)
    vault = outer / "vault"
    _write_plan_and_spec(vault)

    commit_vault(vault, env=clean_environ())

    tracked = _tracked(outer).splitlines()
    assert "vault/projects/p/specs/y.md" in tracked
    assert "vault/projects/p/plans/x.md" not in tracked


def test_commit_vault_excludes_plans_in_a_vault_under_an_outer_projects_dir(tmp_path):
    """Verify a vault nested in an outer `projects/` folder still keeps its plans out."""
    outer = _repo(tmp_path)
    vault = outer / "projects" / "notes"
    _write_plan_and_spec(vault)

    commit_vault(vault, env=clean_environ())

    tracked = _tracked(outer).splitlines()
    assert "projects/notes/projects/p/specs/y.md" in tracked
    assert "projects/notes/projects/p/plans/x.md" not in tracked


def test_commit_vault_stages_the_outer_repository_plans(tmp_path):
    """Verify the exclusion is anchored to the vault, not to every `projects/*/plans`."""
    outer = _repo(tmp_path)
    vault = outer / "vault"
    _write_plan_and_spec(vault)
    app_plan = outer / "projects" / "app" / "plans" / "x.md"
    app_plan.parent.mkdir(parents=True)
    app_plan.write_text("x", encoding="utf-8")

    commit_vault(vault, env=clean_environ())

    tracked = _tracked(outer).splitlines()
    assert "projects/app/plans/x.md" in tracked
    assert "vault/projects/p/plans/x.md" not in tracked


def test_commit_vault_excludes_plans_under_a_prefix_with_glob_characters(tmp_path):
    """Verify a vault folder whose name holds glob characters still keeps its plans out."""
    outer = _repo(tmp_path)
    vault = outer / "my [vault]*"
    _write_plan_and_spec(vault)

    commit_vault(vault, env=clean_environ())

    tracked = _tracked(outer).splitlines()
    assert "my [vault]*/projects/p/specs/y.md" in tracked
    assert "my [vault]*/projects/p/plans/x.md" not in tracked


def test_plans_exclusion_falls_back_to_any_depth_without_a_prefix():
    """Verify an unknown prefix falls back to the unanchored plans glob."""
    assert commit_module._plans_exclusion(None) == ":(exclude,glob)**/projects/*/plans/**"
    assert commit_module._plans_exclusion("") == ":(top,exclude,glob)projects/*/plans/**"
    assert commit_module._plans_exclusion("a/") == ":(top,exclude,glob)a/projects/*/plans/**"


def test_commit_vault_leaves_an_already_tracked_plan_tracked(tmp_path):
    """Verify the exclusion does not untrack a plan that was committed earlier."""
    root = _repo(tmp_path)
    _write_plan_and_spec(root)
    _git("add", "-f", "projects/p/plans/x.md", cwd=root)
    _git("commit", "-q", "-m", "plan", cwd=root)
    (root / "projects/p/plans/x.md").write_text("edited", encoding="utf-8")

    commit_vault(root, env=clean_environ())

    assert "projects/p/plans/x.md" in _tracked(root).splitlines()
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=True
    ).stdout
    assert " M projects/p/plans/x.md" in status.splitlines()


def test_message_groups_by_the_segment_after_the_vault_prefix():
    """Verify a vault nested under an outer `projects/` folder groups by its own project."""
    changes = [("A", "projects/notes/projects/a/logs/x.md")]

    subject, body = message(changes, prefix="projects/notes/")

    assert subject == "a: add 1 log"
    assert "add: projects/notes/projects/a/logs/x.md" in body


def test_message_groups_the_vault_system_folder_under_a_prefix():
    """Verify `_system` under a nested vault prefix is grouped as the vault."""
    subject, _ = message([("M", "projects/notes/_system/registry.toml")], prefix="projects/notes/")

    assert subject == "vault: edit 1 file"


def test_message_with_an_empty_prefix_groups_a_top_level_vault():
    """Verify a vault at the top of its repository groups by project as before."""
    changes = [("A", "projects/crosstune/logs/a.md"), ("M", "_system/registry.toml")]

    subject, _ = message(changes, prefix="")

    assert subject == "crosstune: add 1 log"


def test_message_with_a_simple_prefix_groups_a_nested_vault():
    """Verify a vault nested under an ordinary folder groups by project."""
    subject, _ = message([("A", "notes/vault/projects/crosstune/logs/a.md")], prefix="notes/vault/")

    assert subject == "crosstune: add 1 log"


def test_message_reads_a_path_outside_the_vault_prefix_as_the_vault():
    """Verify an outer repository's own `projects/` path is not taken for a vault project."""
    changes = [("A", "notes/projects/a/logs/x.md"), ("M", "projects/app/readme.md")]

    subject, body = message(changes, prefix="notes/")

    assert subject == "a: add 1 log"
    assert "edit: projects/app/readme.md" in body


def test_commit_vault_groups_a_vault_under_an_outer_projects_dir(tmp_path):
    """Verify the commit subject names the vault's project, not the outer folder's name."""
    outer = _repo(tmp_path)
    vault = outer / "projects" / "notes"
    page = vault / "projects" / "a" / "logs" / "x.md"
    page.parent.mkdir(parents=True)
    page.write_text("x", encoding="utf-8")

    commit_vault(vault, env=clean_environ())

    log = subprocess.run(
        ["git", "log", "-1", "--format=%s"], cwd=outer, capture_output=True, text=True, check=True
    )
    assert log.stdout.strip() == "a: add 1 log"


def test_message_groups_paths_with_spaces_and_non_ascii_characters():
    """Verify a project or page name holding spaces or non-ASCII text groups and lists intact."""
    changes = [
        ("A", "projects/café app/learnings/naïve page.md"),
        ("A", "projects/café app/logs/a b.md"),
    ]

    subject, body = message(changes, prefix="")

    assert subject == "café app: add 1 log, 1 learning"
    assert "add: projects/café app/learnings/naïve page.md" in body
    assert "add: projects/café app/logs/a b.md" in body


def test_commit_vault_stages_paths_with_spaces_and_non_ascii_characters(tmp_path):
    """Verify such paths are staged, committed, and named in the message unquoted."""
    outer = _repo(tmp_path)
    vault = outer / "my vault ü"
    page = vault / "projects" / "café app" / "learnings" / "naïve page.md"
    page.parent.mkdir(parents=True)
    page.write_text("x", encoding="utf-8")
    plan = vault / "projects" / "café app" / "plans" / "plan ü.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("x", encoding="utf-8")

    outcome = commit_vault(vault, env=clean_environ())

    assert outcome.sha
    tracked = subprocess.run(
        ["git", "-c", "core.quotepath=false", "ls-files"],
        cwd=outer,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert "my vault ü/projects/café app/learnings/naïve page.md" in tracked
    assert not any("plans" in path for path in tracked)
    log = subprocess.run(
        ["git", "log", "-1", "--format=%s%n%b"],
        cwd=outer,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert log.splitlines()[0] == "café app: add 1 learning"
    assert "add: my vault ü/projects/café app/learnings/naïve page.md" in log


def _hide_local_md_globally(root: Path, tmp_path: Path) -> None:
    """Point the repo at an excludes file naming CLAUDE.local.md, as a global gitignore does."""
    excludes = tmp_path / "global-ignore"
    excludes.write_text("CLAUDE.local.md\n", encoding="utf-8")
    _git("config", "core.excludesFile", str(excludes), cwd=root)


def test_commit_stages_local_md_hidden_by_a_global_ignore(tmp_path):
    """Verify a CLAUDE.local.md a global ignore names is still committed."""
    # Given a project holding a learning and a CLAUDE.local.md the global ignore hides
    root = _repo(tmp_path)
    _hide_local_md_globally(root, tmp_path)
    project = root / "projects" / "demo"
    (project / "learnings").mkdir(parents=True)
    (project / "learnings" / "page.md").write_text("x", encoding="utf-8")
    (project / "CLAUDE.local.md").write_text("private", encoding="utf-8")

    # When committing the vault
    outcome = commit_vault(root, env=clean_environ())

    # Then the copy is tracked beside the learning
    assert outcome.sha
    tracked = _tracked(root)
    assert "projects/demo/CLAUDE.local.md" in tracked
    assert "projects/demo/learnings/page.md" in tracked


def test_commit_with_only_a_hidden_local_md_is_not_clean(tmp_path):
    """Verify a vault whose only change is a hidden CLAUDE.local.md still commits."""
    root = _repo(tmp_path)
    _hide_local_md_globally(root, tmp_path)
    project = root / "projects" / "demo"
    project.mkdir(parents=True)
    (project / "CLAUDE.local.md").write_text("private", encoding="utf-8")

    outcome = commit_vault(root, env=clean_environ())

    assert outcome.sha
    assert "projects/demo/CLAUDE.local.md" in _tracked(root)


def test_commit_skips_a_symlinked_local_md(tmp_path):
    """Verify a symlink at the CLAUDE.local.md path is never force-added."""
    root = _repo(tmp_path)
    _hide_local_md_globally(root, tmp_path)
    project = root / "projects" / "demo"
    project.mkdir(parents=True)
    target = tmp_path / "elsewhere.md"
    target.write_text("private", encoding="utf-8")
    (project / "CLAUDE.local.md").symlink_to(target)

    outcome = commit_vault(root, env=clean_environ())

    assert outcome == CommitOutcome(reason="clean")
    assert "CLAUDE.local.md" not in _tracked(root)


def test_commit_stages_a_hidden_local_md_in_a_nested_vault(tmp_path):
    """Verify the force-add reaches a CLAUDE.local.md in a vault inside an outer repository."""
    outer = _repo(tmp_path)
    _hide_local_md_globally(outer, tmp_path)
    project = outer / "notes" / "vault" / "projects" / "demo"
    project.mkdir(parents=True)
    (project / "CLAUDE.local.md").write_text("private", encoding="utf-8")

    outcome = commit_vault(outer / "notes" / "vault", env=clean_environ())

    assert outcome.sha
    assert "notes/vault/projects/demo/CLAUDE.local.md" in _tracked(outer).splitlines()


def test_commit_reports_a_failed_force_add(tmp_path, monkeypatch):
    """Verify a failed force-add of CLAUDE.local.md is not committed over."""
    root = _repo(tmp_path)
    _hide_local_md_globally(root, tmp_path)
    project = root / "projects" / "demo"
    project.mkdir(parents=True)
    (project / "CLAUDE.local.md").write_text("private", encoding="utf-8")
    (root / "page.md").write_text("x", encoding="utf-8")
    real_git = commit_module._git

    def _fake_git(root_, *args, env, timeout=10, **kwargs) -> subprocess.CompletedProcess | None:
        if args[:2] == ("add", "-f"):
            return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="boom")
        return real_git(root_, *args, env=env, timeout=timeout, **kwargs)

    monkeypatch.setattr(commit_module, "_git", _fake_git)

    assert commit_vault(root, env=clean_environ()) == CommitOutcome(reason="add failed")
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=root, capture_output=True, text=True, check=True
    )
    assert len(log.stdout.strip().splitlines()) == 1


def test_commit_force_adds_a_glob_named_project_literally(tmp_path):
    """Verify a project folder named like a glob never force-adds another project's link."""
    root = _repo(tmp_path)
    _hide_local_md_globally(root, tmp_path)
    globbed = root / "projects" / "[ab]"
    globbed.mkdir(parents=True)
    (globbed / "CLAUDE.local.md").write_text("private", encoding="utf-8")
    other = root / "projects" / "a"
    other.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere.md"
    elsewhere.write_text("not the vault's", encoding="utf-8")
    (other / "CLAUDE.local.md").symlink_to(elsewhere)

    outcome = commit_vault(root, env=clean_environ())

    assert outcome.sha
    tracked = _tracked(root).splitlines()
    assert "projects/[ab]/CLAUDE.local.md" in tracked
    assert "projects/a/CLAUDE.local.md" not in tracked


def test_commit_with_a_committed_hidden_local_md_is_clean(tmp_path):
    """Verify an unchanged CLAUDE.local.md, once committed, leaves the vault clean."""
    root = _repo(tmp_path)
    _hide_local_md_globally(root, tmp_path)
    project = root / "projects" / "demo"
    project.mkdir(parents=True)
    (project / "CLAUDE.local.md").write_text("private", encoding="utf-8")
    assert commit_vault(root, env=clean_environ()).sha

    outcome = commit_vault(root, env=clean_environ())

    assert outcome.reason == "clean"


def test_commit_ignores_other_ignored_files_when_clean(tmp_path):
    """Verify an ignored file other than CLAUDE.local.md does not make the vault dirty."""
    root = _repo(tmp_path)
    (root / ".gitignore").write_text("*.sqlite3\n", encoding="utf-8")
    _git("add", ".gitignore", cwd=root)
    _git("commit", "-q", "-m", "ignore", cwd=root)
    (root / "index.sqlite3").write_text("x", encoding="utf-8")

    assert commit_vault(root, env=clean_environ()).reason == "clean"


def test_commit_force_stages_the_local_folder_copy_hidden_by_a_global_ignore(tmp_path):
    """Verify local/CLAUDE.local.md is committed when a global ignore names it."""
    # Given a vault whose only change is a local/ copy the global ignore hides
    root = _repo(tmp_path)
    _hide_local_md_globally(root, tmp_path)
    local = root / "projects" / "p" / "local"
    local.mkdir(parents=True)
    (local / "CLAUDE.local.md").write_text("private", encoding="utf-8")

    # When committing the vault
    outcome = commit_vault(root, env=clean_environ())

    # Then the copy is tracked
    assert outcome.sha
    assert "projects/p/local/CLAUDE.local.md" in _tracked(root).splitlines()


def test_commit_stages_skill_files_a_global_ignore_names_through_the_vault_gitignore(tmp_path):
    """Verify the vault .gitignore re-include lets `add -A` stage every skill file."""
    # Given a global ignore naming what a skill may hold, and the vault's re-include lines
    root = _repo(tmp_path)
    excludes = tmp_path / "global-ignore"
    excludes.write_text("env/\n*.log\nsuperpowers/\n", encoding="utf-8")
    _git("config", "core.excludesFile", str(excludes), cwd=root)
    (root / ".gitignore").write_text(
        "!projects/*/local/**\nprojects/*/local/**/.DS_Store\nprojects/*/local/**/__pycache__/\n",
        encoding="utf-8",
    )
    skill = root / "projects" / "p" / "local" / "claude" / "skills" / "superpowers"
    (skill / "env").mkdir(parents=True)
    (skill / "SKILL.md").write_text("skill", encoding="utf-8")
    (skill / "env" / "run.log").write_text("log", encoding="utf-8")
    (skill / ".DS_Store").write_text("debris", encoding="utf-8")

    # When committing the vault
    outcome = commit_vault(root, env=clean_environ())

    # Then every skill file is tracked except debris
    assert outcome.sha
    tracked = _tracked(root).splitlines()
    prefix = "projects/p/local/claude/skills/superpowers"
    assert f"{prefix}/SKILL.md" in tracked
    assert f"{prefix}/env/run.log" in tracked
    assert f"{prefix}/.DS_Store" not in tracked


def test_commit_force_stages_local_md_when_a_global_ignore_names_the_local_folder(tmp_path):
    """Verify local/CLAUDE.local.md commits when a global ignore hides the whole folder."""
    # Given a vault with no re-include lines and a global ignore naming `local/`
    root = _repo(tmp_path)
    excludes = tmp_path / "global-ignore"
    excludes.write_text("local/\n", encoding="utf-8")
    _git("config", "core.excludesFile", str(excludes), cwd=root)
    local = root / "projects" / "p" / "local"
    local.mkdir(parents=True)
    (local / "CLAUDE.local.md").write_text("private", encoding="utf-8")

    # When committing the vault
    outcome = commit_vault(root, env=clean_environ())

    # Then the force-staged copy is committed rather than the vault reading as clean
    assert outcome.sha
    assert "projects/p/local/CLAUDE.local.md" in _tracked(root).splitlines()


def test_commit_stages_skills_when_a_global_ignore_names_the_local_folder(tmp_path):
    """Verify the vault's folder re-include line lets `add -A` reach skills under `local/`."""
    root = _repo(tmp_path)
    excludes = tmp_path / "global-ignore"
    excludes.write_text("local/\nnode_modules/\n", encoding="utf-8")
    _git("config", "core.excludesFile", str(excludes), cwd=root)
    (root / ".gitignore").write_text(
        "!projects/*/local/\n!projects/*/local/**\nprojects/*/local/**/.DS_Store\n"
        "projects/*/local/**/__pycache__/\nprojects/*/local/**/node_modules/\n",
        encoding="utf-8",
    )
    skill = root / "projects" / "p" / "local" / "claude" / "skills" / "s"
    (skill / "node_modules").mkdir(parents=True)
    (skill / "SKILL.md").write_text("skill", encoding="utf-8")
    (skill / "node_modules" / "x.js").write_text("x", encoding="utf-8")

    outcome = commit_vault(root, env=clean_environ())

    assert outcome.sha
    tracked = _tracked(root).splitlines()
    assert "projects/p/local/claude/skills/s/SKILL.md" in tracked
    assert "projects/p/local/claude/skills/s/node_modules/x.js" not in tracked
