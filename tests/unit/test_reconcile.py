"""Tests for the log commit reconciler."""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING

import pytest

from sessionmemory.lib import field, paths, reconcile
from sessionmemory.lib.commits import CommitsError, Expansion
from sessionmemory.lib.reconcile import Context
from tests.unit.gitfixtures import commit, git, rebase_merge, squash_merge

if TYPE_CHECKING:
    from pathlib import Path

    from tests.unit.gitfixtures import Fixture

SLUG = "demo"


def _body(*lines: str, summary: str = "Did work.") -> str:
    changes = "\n".join(lines)
    return f"## Summary\n\n{summary}\n\n## Changes\n\n{changes}\n\n## Pages\n\n- none\n"


def _write_log(vault: Path, name: str, body: str) -> Path:
    path = paths.logs_dir(vault, SLUG) / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "title": name,
        "uuid": str(uuid.uuid5(uuid.NAMESPACE_URL, name)),
        "summary": "s",
        "created": "2026-01-01T00:00:00Z",
        "updated": "2026-01-01T00:00:00Z",
    }
    field.write_page(path, meta, body)
    return path


def _ctx(vault: Path, repo: Fixture) -> Context:
    return Context(vault=vault, slug=SLUG, root=repo.clone)


def _github(repo: Fixture) -> None:
    git(repo.clone, "config", "remote.origin.url", "https://github.com/nate/demo.git")
    origin = repo.clone.parent / "origin.git"
    git(repo.clone, "config", f"url.{origin}.insteadOf", "https://github.com/nate/demo.git")


def _squash(repo: Fixture) -> str:
    git(repo.clone, "checkout", "-q", "main")
    sha = commit(repo.clone, "squash")
    git(repo.clone, "push", "-q", "origin", "main")
    return sha


def _merged(sha: str) -> str:
    return json.dumps(
        [
            {
                "state": "closed",
                "merged_at": "2026-01-01T00:00:00Z",
                "merge_commit_sha": sha,
                "base": {"ref": "main"},
            }
        ]
    )


def _open() -> str:
    return json.dumps([{"state": "open", "merged_at": None, "base": {"ref": "main"}}])


def test_squash_merged_commits_become_the_merge_commit(tmp_path, repo, fake_gh):
    """Verify a squash-merged orphan is replaced by the trunk commit's line."""
    _github(repo)
    squash = _squash(repo)
    fake_gh(repo.gone, stdout=_merged(squash))
    path = _write_log(tmp_path, "one", _body(f"{repo.gone[:7]} gone work"))

    report = reconcile.log_commits(_ctx(tmp_path, repo), dry_run=False)

    assert report.name == "log-commits"
    assert report.replaced == 1
    assert report.changed == [path]
    assert f"{squash[:7]} squash" in field.read_page(path).body
    assert repo.gone[:7] not in field.read_page(path).body
    assert field.read_page(path).meta["updated"] == "2026-01-01T00:00:00Z"


def test_open_pr_and_live_branch_are_left_alone(tmp_path, repo, fake_gh):
    """Verify open PRs and commits on a live branch are not touched."""
    _github(repo)
    git(repo.clone, "checkout", "-q", "feat")
    feat2 = commit(repo.clone, "feat2")
    git(repo.clone, "checkout", "-q", "main")
    fake_gh(repo.feat, stdout=_open())
    fake_gh(feat2, stdout="[]")
    path = _write_log(tmp_path, "one", _body(f"{repo.feat[:7]} feat", f"{feat2[:7]} feat2"))
    before = path.read_bytes()

    report = reconcile.log_commits(_ctx(tmp_path, repo), dry_run=False)

    assert report.in_progress == 2
    assert report.changed == []
    assert path.read_bytes() == before


def test_orphan_unknown_to_github_is_dropped(tmp_path, repo, fake_gh):
    """Verify a commit GitHub never saw and no branch holds is dropped."""
    _github(repo)
    fake_gh(repo.gone, stderr="gh: No commit found for SHA (HTTP 422)", code=1)
    path = _write_log(tmp_path, "one", _body(f"{repo.gone[:7]} gone work"))

    report = reconcile.log_commits(_ctx(tmp_path, repo), dry_run=False)

    assert report.dropped == 1
    assert repo.gone[:7] not in field.read_page(path).body


def test_non_github_origin_never_calls_gh_and_drops_orphans(tmp_path, repo, fake_gh):
    """Verify a non-GitHub origin skips gh entirely."""
    git(repo.clone, "remote", "set-url", "origin", "https://example.org/nate/demo.git")
    origin = repo.clone.parent / "origin.git"
    git(repo.clone, "config", f"url.{origin}.insteadOf", "https://example.org/nate/demo.git")
    marker = tmp_path / "gh-called"
    gh = tmp_path / "bin" / "gh"
    gh.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n", encoding="utf-8")
    path = _write_log(tmp_path, "one", _body(f"{repo.gone[:7]} gone work"))

    report = reconcile.log_commits(_ctx(tmp_path, repo), dry_run=False)

    assert report.dropped == 1
    assert not marker.exists()
    assert repo.gone[:7] not in field.read_page(path).body


def test_gh_failure_leaves_lines_and_reports_unchecked(tmp_path, repo, fake_gh):
    """Verify a gh failure keeps the line and is reported as a note."""
    _github(repo)
    fake_gh(repo.gone, stderr="gh: Bad credentials (HTTP 401)", code=1)
    path = _write_log(tmp_path, "one", _body(f"{repo.gone[:7]} gone work"))
    before = path.read_bytes()

    report = reconcile.log_commits(_ctx(tmp_path, repo), dry_run=False)

    assert report.unchecked == 1
    assert len(report.notes) == 1
    assert "gh failed" in report.notes[0]
    assert path.read_bytes() == before


def test_dry_run_reports_without_writing(tmp_path, repo, fake_gh):
    """Verify dry run counts and lists the log but leaves it untouched."""
    _github(repo)
    squash = _squash(repo)
    fake_gh(repo.gone, stdout=_merged(squash))
    path = _write_log(tmp_path, "one", _body(f"{repo.gone[:7]} gone work"))
    before = path.read_bytes()

    report = reconcile.log_commits(_ctx(tmp_path, repo), dry_run=True)

    assert report.replaced == 1
    assert report.changed == [path]
    assert path.read_bytes() == before


def test_second_run_changes_nothing_and_skips_github(tmp_path, repo, fake_gh, monkeypatch):
    """Verify a reconciled log is all KEEP, so gh is not needed again."""
    _github(repo)
    squash = _squash(repo)
    fake_gh(repo.gone, stdout=_merged(squash))
    _write_log(tmp_path, "one", _body(f"{repo.gone[:7]} gone work"))
    context = _ctx(tmp_path, repo)
    reconcile.log_commits(context, dry_run=False)
    (tmp_path / "responses.json").unlink()
    git(repo.clone, "remote", "set-url", "origin", str(tmp_path / "missing.git"))

    def no_fetch(_self) -> bool:
        pytest.fail("fetched although every logged commit is on the local trunk")

    monkeypatch.setattr(reconcile.Repository, "fetch", no_fetch)

    report = reconcile.log_commits(context, dry_run=False)

    assert (report.replaced, report.dropped, report.in_progress, report.unchecked) == (0, 0, 0, 0)
    assert report.changed == []
    assert report.notes == []


def test_summary_prose_sha_is_kept(tmp_path, repo, fake_gh):
    """Verify a sha mentioned in the Summary survives the drop."""
    _github(repo)
    fake_gh(repo.gone, stderr="gh: No commit found for SHA (HTTP 422)", code=1)
    prose = f"Reverted {repo.gone[:7]} by hand."
    path = _write_log(tmp_path, "one", _body(f"{repo.gone[:7]} gone work", summary=prose))

    reconcile.log_commits(_ctx(tmp_path, repo), dry_run=False)

    body = field.read_page(path).body
    assert prose in body
    assert f"{repo.gone[:7]} gone work" not in body


def test_unknown_sha_is_dropped(tmp_path, repo):
    """Verify a sha the repository does not know is dropped."""
    path = _write_log(tmp_path, "one", _body("deadbee never existed"))

    report = reconcile.log_commits(_ctx(tmp_path, repo), dry_run=False)

    assert report.dropped == 1
    assert "deadbee" not in field.read_page(path).body


def test_merge_commit_that_cannot_be_described_is_unchecked(tmp_path, repo, fake_gh, monkeypatch):
    """Verify a describe() miss keeps the line rather than writing an empty one."""
    _github(repo)
    squash = _squash(repo)
    fake_gh(repo.gone, stdout=_merged(squash))
    monkeypatch.setattr(reconcile.Repository, "describe", lambda _self, _sha: None)
    path = _write_log(tmp_path, "one", _body(f"{repo.gone[:7]} gone work"))
    before = path.read_bytes()

    report = reconcile.log_commits(_ctx(tmp_path, repo), dry_run=False)

    assert report.unchecked == 1
    assert "could not be described" in report.notes[0]
    assert path.read_bytes() == before


def test_merge_commit_not_on_trunk_is_unchecked(tmp_path, repo, fake_gh):
    """Verify a merge sha missing from trunk keeps the line."""
    _github(repo)
    fake_gh(repo.gone, stdout=_merged(repo.feat))
    _write_log(tmp_path, "one", _body(f"{repo.gone[:7]} gone work"))

    report = reconcile.log_commits(_ctx(tmp_path, repo), dry_run=False)

    assert report.unchecked == 1
    assert report.notes == ["merge commit not on trunk"]


def test_git_failure_propagates(tmp_path, repo, monkeypatch):
    """Verify a CommitsError is not swallowed."""
    _write_log(tmp_path, "one", _body(f"{repo.gone[:7]} gone work"))

    def boom(_self) -> set[str]:
        msg = "git rev-list"
        raise CommitsError(msg, "bad")

    monkeypatch.setattr(reconcile.Repository, "trunk_commits", boom)

    with pytest.raises(CommitsError):
        reconcile.log_commits(_ctx(tmp_path, repo), dry_run=False)


def test_run_returns_one_report_per_reconciler(tmp_path, repo):
    """Verify run executes every registered reconciler."""
    reports = reconcile.run(_ctx(tmp_path, repo), dry_run=True)

    assert [r.name for r in reports] == ["log-commits"]


def test_two_commits_from_one_squashed_pr_collapse_to_one_line(tmp_path, repo, fake_gh):
    """Verify each logged line counts as replaced while one trunk line remains."""
    _github(repo)
    squash = _squash(repo)
    git(repo.clone, "checkout", "-q", "-b", "other", repo.main_head)
    second = commit(repo.clone, "second")
    git(repo.clone, "checkout", "-q", "main")
    git(repo.clone, "branch", "-q", "-D", "other")
    fake_gh(repo.gone, stdout=_merged(squash))
    fake_gh(second, stdout=_merged(squash))
    path = _write_log(tmp_path, "one", _body(f"{repo.gone[:7]} a", f"{second[:7]} b"))

    report = reconcile.log_commits(_ctx(tmp_path, repo), dry_run=False)

    assert report.replaced == 2
    changes_section = field.read_page(path).body.split("## Changes")[1].split("## Pages")[0]
    assert changes_section.count(squash[:7]) == 1
    assert repo.gone[:7] not in changes_section
    assert second[:7] not in changes_section


def test_ambiguous_short_sha_is_left_and_unchecked(tmp_path, repo, monkeypatch):
    """Verify an ambiguous prefix stays in the log rather than being dropped."""
    short = repo.main_head[:7]
    monkeypatch.setattr(
        reconcile.Repository,
        "expand",
        lambda _self, _shas: Expansion(full={}, ambiguous=frozenset({short})),
    )
    path = _write_log(tmp_path, "one", _body(f"{short} base"))
    before = path.read_bytes()

    report = reconcile.log_commits(_ctx(tmp_path, repo), dry_run=False)

    assert report.unchecked == 1
    assert report.notes == ["short sha is ambiguous"]
    assert path.read_bytes() == before


def test_failed_fetch_leaves_unknown_and_orphaned_lines_unchecked(tmp_path, repo):
    """Verify a failed fetch never drops a commit origin's trunk may now hold."""
    git(repo.clone, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    path = _write_log(tmp_path, "one", _body("deadbee never existed", f"{repo.gone[:7]} gone"))
    before = path.read_bytes()

    report = reconcile.log_commits(_ctx(tmp_path, repo), dry_run=False)

    assert report.unchecked == 2
    assert report.dropped == 0
    assert report.notes == [reconcile.FETCH_FAILED_NOTE]
    assert report.changed == []
    assert path.read_bytes() == before


def test_no_origin_skips_the_fetch_and_its_note(tmp_path, repo, monkeypatch):
    """Verify a project without origin is not reported as a failed fetch."""
    git(repo.clone, "remote", "remove", "origin")

    def no_fetch(_self) -> bool:
        pytest.fail("fetched without an origin")

    monkeypatch.setattr(reconcile.Repository, "fetch", no_fetch)
    path = _write_log(tmp_path, "one", _body("deadbee never existed"))

    report = reconcile.log_commits(_ctx(tmp_path, repo), dry_run=False)

    assert report.dropped == 1
    assert report.notes == []
    assert "deadbee" not in field.read_page(path).body


def test_shallow_clone_leaves_unknown_shas_unchecked(tmp_path, repo):
    """Verify a sha a shallow clone does not know is left, since it may sit below the cut."""
    commit(repo.clone, "second")
    git(repo.clone, "push", "-q", "origin", "main")
    shallow = tmp_path / "shallow"
    git(tmp_path, "clone", "-q", "--depth", "1", f"file://{tmp_path / 'origin.git'}", str(shallow))
    path = _write_log(tmp_path, "one", _body(f"{repo.main_head[:7]} base"))
    before = path.read_bytes()

    report = reconcile.log_commits(Context(vault=tmp_path, slug=SLUG, root=shallow), dry_run=False)

    assert report.unchecked == 1
    assert report.notes == [reconcile.SHALLOW_NOTE]
    assert path.read_bytes() == before


def test_shallow_clone_leaves_known_orphans_unchecked(tmp_path, repo):
    """Verify a shallow clone never drops a commit, even one it knows that no ref holds."""
    shallow = tmp_path / "shallow"
    git(tmp_path, "clone", "-q", "--depth", "1", f"file://{tmp_path / 'origin.git'}", str(shallow))
    git(shallow, "checkout", "-q", "-b", "scratch")
    orphan = commit(shallow, "orphan")
    git(shallow, "checkout", "-q", "main")
    git(shallow, "branch", "-q", "-D", "scratch")
    path = _write_log(tmp_path, "one", _body(f"{orphan[:7]} orphan"))
    before = path.read_bytes()

    report = reconcile.log_commits(Context(vault=tmp_path, slug=SLUG, root=shallow), dry_run=False)

    assert report.dropped == 0
    assert report.unchecked == 1
    assert path.read_bytes() == before


def test_git_failure_on_a_later_log_writes_nothing(tmp_path, repo, monkeypatch):
    """Verify every decision is made before any log is written."""
    first = _write_log(tmp_path, "a", _body("deadbee never existed"))
    _write_log(tmp_path, "b", _body("cafe123 also never existed"))
    before = first.read_bytes()
    decide = reconcile._Judge.decide

    def flaky(self, sha: str, expansion: Expansion) -> reconcile.Decision:
        if sha == "cafe123":
            msg = "git for-each-ref"
            raise CommitsError(msg, "bad")
        return decide(self, sha, expansion)

    monkeypatch.setattr(reconcile._Judge, "decide", flaky)

    with pytest.raises(CommitsError):
        reconcile.log_commits(_ctx(tmp_path, repo), dry_run=False)

    assert first.read_bytes() == before


def test_replacement_matches_a_merge_commit_logged_with_a_longer_sha(tmp_path, repo, fake_gh):
    """Verify a merge commit already listed under a longer sha is not listed twice."""
    _github(repo)
    squash = _squash(repo)
    fake_gh(repo.gone, stdout=_merged(squash))
    path = _write_log(tmp_path, "one", _body(f"{repo.gone[:7]} gone work", f"{squash[:12]} squash"))

    report = reconcile.log_commits(_ctx(tmp_path, repo), dry_run=False)

    assert report.replaced == 1
    section = field.read_page(path).body.split("## Changes")[1].split("## Pages")[0]
    assert [line for line in section.splitlines() if line.strip()] == [f"{squash[:12]} squash"]


def test_log_rewritten_during_the_run_keeps_the_new_text(tmp_path, repo, monkeypatch):
    """Verify a write that lands while git is asked survives the reconcile write."""
    path = _write_log(tmp_path, "one", _body("deadbee never existed"))
    page = field.read_page(path)
    newer = page.body.replace("Did work.", "Did more work.")

    def concurrent_fetch(_self) -> bool:
        field.write_page(path, page.meta, newer)
        return True

    monkeypatch.setattr(reconcile.Repository, "fetch", concurrent_fetch)

    report = reconcile.log_commits(_ctx(tmp_path, repo), dry_run=False)

    body = field.read_page(path).body
    assert report.dropped == 1
    assert "Did more work." in body
    assert "deadbee" not in body


def test_log_without_frontmatter_is_not_rewritten(tmp_path, repo):
    """Verify a log whose frontmatter does not parse is left byte for byte."""
    path = paths.logs_dir(tmp_path, SLUG) / "broken.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\ntitle: [unclosed\n---\n" + _body("deadbee never existed"))
    before = path.read_bytes()

    report = reconcile.log_commits(_ctx(tmp_path, repo), dry_run=False)

    assert report.changed == []
    assert path.read_bytes() == before


def test_ambiguous_short_sha_does_not_fetch(tmp_path, repo, monkeypatch):
    """Verify an ambiguous prefix alone never sends a repeat run to the network."""
    short = repo.main_head[:7]
    monkeypatch.setattr(
        reconcile.Repository,
        "expand",
        lambda _self, _shas: Expansion(full={}, ambiguous=frozenset({short})),
    )

    def no_fetch(_self) -> bool:
        pytest.fail("fetched for an ambiguous sha")

    monkeypatch.setattr(reconcile.Repository, "fetch", no_fetch)
    _write_log(tmp_path, "one", _body(f"{short} base"))

    report = reconcile.log_commits(_ctx(tmp_path, repo), dry_run=False)

    assert report.unchecked == 1


def test_single_branch_clone_leaves_unknown_shas_unchecked(tmp_path, repo):
    """Verify a commit on a branch a single-branch clone never fetches is not dropped."""
    single = tmp_path / "single"
    origin = f"file://{tmp_path / 'origin.git'}"
    git(tmp_path, "clone", "-q", "--single-branch", "-b", "main", origin, str(single))
    path = _write_log(tmp_path, "one", _body(f"{repo.feat[:7]} feat"))
    before = path.read_bytes()

    report = reconcile.log_commits(Context(vault=tmp_path, slug=SLUG, root=single), dry_run=False)

    assert report.dropped == 0
    assert report.unchecked == 1
    assert report.notes == [reconcile.NARROWED_NOTE]
    assert path.read_bytes() == before


def test_rebase_merged_commits_become_their_own_copies(tmp_path, repo, fake_gh):
    """Verify each rebase-merged commit is replaced by its copy, not by the last one."""
    _github(repo)
    originals, copies = rebase_merge(repo.clone)
    for sha in originals:
        fake_gh(sha, stdout=_merged(copies[-1]))
    lines = [f"{sha[:7]} {name}" for sha, name in zip(originals, ("ra", "rb", "rc"), strict=True)]
    path = _write_log(tmp_path, "one", _body(*lines))

    report = reconcile.log_commits(_ctx(tmp_path, repo), dry_run=False)

    assert report.replaced == 3
    section = field.read_page(path).body.split("## Changes")[1].split("## Pages")[0]
    assert [line for line in section.splitlines() if line.strip()] == [
        f"{copies[0][:7]} ra",
        f"{copies[1][:7]} rb",
        f"{copies[2][:7]} rc",
    ]


def test_squash_merged_branch_collapses_to_the_squash_commit(tmp_path, repo, fake_gh):
    """Verify commits squashed into one trunk commit are replaced by that commit."""
    _github(repo)
    originals, squash = squash_merge(repo.clone)
    for sha in originals:
        fake_gh(sha, stdout=_merged(squash))
    path = _write_log(tmp_path, "one", _body(*(f"{sha[:7]} work" for sha in originals)))

    report = reconcile.log_commits(_ctx(tmp_path, repo), dry_run=False)

    assert report.replaced == 2
    section = field.read_page(path).body.split("## Changes")[1].split("## Pages")[0]
    assert [line for line in section.splitlines() if line.strip()] == [f"{squash[:7]} squashed pr"]


def test_rebased_copy_failure_falls_back_to_the_merge_commit(tmp_path, repo, fake_gh, monkeypatch):
    """Verify a git failure in the rebase search keeps the merge commit, not the run."""
    _github(repo)
    squash = _squash(repo)
    fake_gh(repo.gone, stdout=_merged(squash))

    def boom(_self, _sha: str, _merge_sha: str) -> str | None:
        msg = "git patch-id"
        raise CommitsError(msg, "bad")

    monkeypatch.setattr(reconcile.Repository, "rebased_copy", boom)
    path = _write_log(tmp_path, "one", _body(f"{repo.gone[:7]} gone work"))

    report = reconcile.log_commits(_ctx(tmp_path, repo), dry_run=False)

    assert report.replaced == 1
    assert f"{squash[:7]} squash" in field.read_page(path).body
