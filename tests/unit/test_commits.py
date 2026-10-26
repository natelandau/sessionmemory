"""Tests for the git and GitHub commit queries."""

from __future__ import annotations

import json
import shutil

import pytest

from sessionmemory.lib import commits
from sessionmemory.lib.commits import CommitsError, PullAnswer, PullState, Repository
from tests.unit.gitfixtures import Fixture, commit, rebase_merge, squash_merge
from tests.unit.gitfixtures import git as _git


def _github(fixture: Fixture) -> Repository:
    _git(fixture.clone, "remote", "set-url", "origin", "git@github.com:Nate/Demo.git")
    return Repository(fixture.clone)


def _pr(*, state="closed", merged_at="2026-01-01T00:00:00Z", base="main", sha="abc") -> str:
    return json.dumps(
        [{"state": state, "merged_at": merged_at, "merge_commit_sha": sha, "base": {"ref": base}}]
    )


def _ambiguous_prefix(fixture: Fixture) -> str:
    """Commit until two objects share a 4-character prefix, and return that prefix."""
    seen: dict[str, str] = {}
    for sha in _git(fixture.clone, "rev-list", "--all").split():
        seen[sha[:4]] = sha
    for index in range(2000):
        sha = commit(fixture.clone, f"amb{index}")
        if sha[:4] in seen and seen[sha[:4]] != sha:
            return sha[:4]
        seen[sha[:4]] = sha
    pytest.fail("no ambiguous prefix found")


def test_trunk_follows_origin_head(repo):
    """Verify the trunk is the branch origin/HEAD points at."""
    assert Repository(repo.clone).trunk == "main"


def test_expand_returns_none_for_unknown_shas(repo):
    """Verify a short sha expands and an unknown one maps to None."""
    expansion = Repository(repo.clone).expand([repo.feat[:7], "deadbee"])

    assert expansion.full == {repo.feat[:7]: repo.feat}
    assert expansion.ambiguous == frozenset()


def test_expand_reports_an_ambiguous_prefix_apart_from_an_unknown_one(repo):
    """Verify a prefix shared by two objects is ambiguous, not missing."""
    prefix = _ambiguous_prefix(repo)

    expansion = Repository(repo.clone).expand([prefix, "deadbee"])

    assert expansion.ambiguous == frozenset({prefix})
    assert prefix not in expansion.full
    assert "deadbee" not in expansion.full


def test_trunk_commits_excludes_branch_commits(repo):
    """Verify trunk history holds trunk's commit and not a feature branch's."""
    commits = Repository(repo.clone).trunk_commits()

    assert repo.main_head in commits
    assert repo.feat not in commits


def test_in_branch_sees_live_branches_but_not_trunk_or_deleted_ones(repo):
    """Verify only a commit held by a non-trunk ref counts as in a branch."""
    repository = Repository(repo.clone)

    assert repository.in_branch(repo.feat) is True
    assert repository.in_branch(repo.main_head) is False
    assert repository.in_branch(repo.gone) is False


def test_ref_commits_excludes_a_commit_no_ref_holds(repo):
    """Verify a commit whose refs were deleted is absent from the ref set."""
    commits = Repository(repo.clone).ref_commits()

    assert repo.feat in commits
    assert repo.gone not in commits


def test_describe_gives_short_sha_and_subject(repo):
    """Verify describe returns the abbreviated sha and subject, or None if unknown."""
    repository = Repository(repo.clone)

    assert repository.describe(repo.feat) == f"{repo.feat[:7]} feat"
    assert repository.describe("deadbee") is None


def test_github_slug_parses_github_remotes_only(repo):
    """Verify only a github.com origin yields a slug."""
    assert _github(repo).github_slug() == "nate/demo"

    _git(repo.clone, "remote", "set-url", "origin", "git@example.org:nate/demo.git")
    assert Repository(repo.clone).github_slug() is None


def test_github_slug_raises_when_git_cannot_run(repo, tmp_path, monkeypatch):
    """Verify a git that cannot run is an error, not a non-GitHub origin."""
    empty = tmp_path / "nogit"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    with pytest.raises(CommitsError):
        Repository(repo.clone).github_slug()


def test_github_slug_is_none_without_an_origin_url(repo):
    """Verify an unset remote.origin.url is a plain None."""
    _git(repo.clone, "remote", "remove", "origin")

    assert Repository(repo.clone).github_slug() is None


def test_pulls_merged_into_trunk(repo, fake_gh):
    """Verify a PR merged into trunk gives MERGED with its merge sha."""
    fake_gh(repo.feat, stdout=_pr())

    assert _github(repo).pulls(repo.feat) == PullAnswer(PullState.MERGED, merge_sha="abc")


def test_pulls_merged_into_another_branch_is_none(repo, fake_gh):
    """Verify a merge into a non-trunk base does not count as landed on trunk."""
    fake_gh(repo.feat, stdout=_pr(base="release"))

    assert _github(repo).pulls(repo.feat).state == PullState.NONE


def test_pulls_open(repo, fake_gh):
    """Verify an open PR gives OPEN."""
    fake_gh(repo.feat, stdout=_pr(state="open", merged_at=None))

    assert _github(repo).pulls(repo.feat).state == PullState.OPEN


def test_pulls_unknown_commit_is_none(repo, fake_gh):
    """Verify GitHub's HTTP 422 for an unknown commit is a definite no."""
    fake_gh(repo.feat, stderr="gh: No commit found for SHA: x (HTTP 422)", code=1)

    assert _github(repo).pulls(repo.feat).state == PullState.NONE


def test_pulls_auth_failure_is_unanswered(repo, fake_gh):
    """Verify an auth failure is not an answer."""
    fake_gh(repo.feat, stderr="gh: Bad credentials (HTTP 401)\nmore", code=1)

    answer = _github(repo).pulls(repo.feat)

    assert answer.state == PullState.UNANSWERED
    assert answer.reason == "gh failed: gh: Bad credentials (HTTP 401)"


def test_pulls_unparsable_json_is_unanswered(repo, fake_gh):
    """Verify garbage output from gh is not an answer."""
    fake_gh(repo.feat, stdout="not json")

    assert _github(repo).pulls(repo.feat).state == PullState.UNANSWERED


def test_pulls_without_gh_is_unanswered(repo, monkeypatch, tmp_path):
    """Verify a missing gh binary is reported as such."""
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "git").symlink_to(shutil.which("git"))
    monkeypatch.setenv("PATH", str(empty))

    answer = _github(repo).pulls(repo.feat)

    assert answer == PullAnswer(PullState.UNANSWERED, reason="gh is not installed")


def test_pulls_without_github_remote_is_unanswered(repo):
    """Verify a non-GitHub origin cannot be asked."""
    answer = Repository(repo.clone).pulls(repo.feat)

    assert answer == PullAnswer(PullState.UNANSWERED, reason="origin is not on GitHub")


def test_fetch_without_origin_returns_false(tmp_path):
    """Verify a repository with no remote reports a failed fetch."""
    root = tmp_path / "solo"
    root.mkdir()
    _git(root, "init", "-q")

    assert Repository(root).fetch() is False


def test_fetch_succeeds_against_origin(repo):
    """Verify fetch returns True when origin is reachable."""
    assert Repository(repo.clone).fetch() is True


def test_trunk_falls_back_to_main_without_origin_head(repo):
    """Verify trunk is main when origin/HEAD is absent."""
    _git(repo.clone, "remote", "set-head", "origin", "-d")

    assert Repository(repo.clone).trunk == "main"


def test_pulls_422_without_no_commit_found_is_unanswered(repo, fake_gh):
    """Verify a 422 that is not GitHub's unknown-commit message is not an answer."""
    fake_gh(repo.feat, stderr="gh: Validation Failed (HTTP 422)", code=1)

    assert _github(repo).pulls(repo.feat).state == PullState.UNANSWERED


def test_pulls_timeout_is_unanswered(repo, fake_gh, monkeypatch):
    """Verify a gh call that outlives its timeout is not an answer."""
    fake_gh(repo.feat, stdout="[]", sleep=3)
    monkeypatch.setattr(commits, "GH_TIMEOUT_SECONDS", 0.2)

    answer = _github(repo).pulls(repo.feat)

    assert answer.state == PullState.UNANSWERED
    assert answer.reason.startswith("gh timed out")


def test_git_failure_raises_instead_of_looking_empty(repo, monkeypatch):
    """Verify git that cannot run raises rather than reporting no commits."""
    repository = Repository(repo.clone)
    monkeypatch.setattr(commits.gitinfo, "run_git", lambda *_a, **_k: None)

    with pytest.raises(CommitsError):
        repository.ref_commits()
    with pytest.raises(CommitsError):
        repository.trunk_commits()
    with pytest.raises(CommitsError):
        repository.in_branch(repo.feat)


def test_unreadable_repository_raises(repo):
    """Verify a repository whose .git vanished raises from every query."""
    repository = Repository(repo.clone)
    sha = repo.feat
    shutil.rmtree(repo.clone / ".git")

    with pytest.raises(CommitsError):
        repository.ref_commits()
    with pytest.raises(CommitsError):
        repository.in_branch(sha)
    with pytest.raises(CommitsError):
        repository.expand([sha[:7]])
    with pytest.raises(CommitsError):
        repository.trunk_commits()


def test_in_branch_unknown_sha_is_false(repo):
    """Verify an unknown sha is a definite no, not an error."""
    assert Repository(repo.clone).in_branch("deadbeef") is False


def test_in_branch_sees_a_tag(repo):
    """Verify a commit only a tag holds counts as held, matching doctor's ref check."""
    _git(repo.clone, "tag", "kept", repo.gone)

    assert Repository(repo.clone).in_branch(repo.gone) is True


def test_origin_url_reads_the_remote(repo):
    """Verify origin_url returns the configured URL."""
    assert Repository(repo.clone).origin_url() == str(repo.clone.parent / "origin.git")


def test_origin_url_is_none_without_an_origin(repo):
    """Verify a repository without origin has no origin URL."""
    _git(repo.clone, "remote", "remove", "origin")

    assert Repository(repo.clone).origin_url() is None


def test_is_shallow_is_false_for_a_full_clone(repo):
    """Verify a full clone is not shallow."""
    assert Repository(repo.clone).is_shallow() is False


def test_is_shallow_is_true_for_a_depth_one_clone(repo, tmp_path):
    """Verify a depth-limited clone is shallow."""
    commit(repo.clone, "second")
    _git(repo.clone, "push", "-q", "origin", "main")
    shallow = tmp_path / "shallow"
    _git(tmp_path, "clone", "-q", "--depth", "1", f"file://{tmp_path / 'origin.git'}", str(shallow))

    assert Repository(shallow).is_shallow() is True


def test_is_shallow_raises_when_git_cannot_answer(tmp_path):
    """Verify a directory outside any repository raises rather than reporting not shallow."""
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(CommitsError):
        Repository(plain).is_shallow()


def test_safe_env_keeps_ssh_settings_and_strips_other_git_variables(monkeypatch):
    """Verify fetch can still authenticate while hook variables cannot retarget git."""
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i key")
    monkeypatch.setenv("GIT_SSH", "/usr/bin/ssh")
    monkeypatch.setenv("GIT_SSH_VARIANT", "ssh")
    monkeypatch.setenv("GIT_DIR", "/elsewhere/.git")
    monkeypatch.setenv("GIT_INDEX_FILE", "/elsewhere/index")

    env = commits._safe_env()

    assert env["GIT_SSH_COMMAND"] == "ssh -i key"
    assert env["GIT_SSH"] == "/usr/bin/ssh"
    assert env["GIT_SSH_VARIANT"] == "ssh"
    assert "GIT_DIR" not in env
    assert "GIT_INDEX_FILE" not in env
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GH_PROMPT_DISABLED"] == "1"


def test_in_branch_off_trunk_skips_the_trunk_checks(repo, monkeypatch):
    """Verify off_trunk gives the default answer without asking about trunk again."""
    repository = Repository(repo.clone)
    expected = (repository.in_branch(repo.feat), repository.in_branch(repo.gone))

    def no_trunk_check(_self, _sha: str) -> bool:
        pytest.fail("checked trunk although the caller vouched the commit is off it")

    monkeypatch.setattr(Repository, "_on_trunk", no_trunk_check)

    assert expected == (True, False)
    assert repository.in_branch(repo.feat, off_trunk=True) is True
    assert repository.in_branch(repo.gone, off_trunk=True) is False


def test_is_repository_requires_the_root_to_be_the_top_level(repo):
    """Verify a plain subdirectory of a repository is not a repository root."""
    sub = repo.clone / "sub"
    sub.mkdir()

    assert Repository(repo.clone).is_repository() is True
    assert Repository(sub).is_repository() is False


def test_fetches_all_branches_for_a_full_clone(repo):
    """Verify a default clone fetches every branch."""
    assert Repository(repo.clone).fetches_all_branches() is True


def test_fetches_all_branches_is_false_for_a_single_branch_clone(repo, tmp_path):
    """Verify a single-branch clone's narrowed refspec is reported."""
    single = tmp_path / "single"
    origin = f"file://{tmp_path / 'origin.git'}"
    _git(tmp_path, "clone", "-q", "--single-branch", "-b", "main", origin, str(single))

    assert Repository(single).fetches_all_branches() is False


def test_fetches_all_branches_is_false_without_a_refspec(repo):
    """Verify an origin with no fetch refspec cannot vouch for other branches."""
    _git(repo.clone, "config", "--unset-all", "remote.origin.fetch")

    assert Repository(repo.clone).fetches_all_branches() is False


def test_fetches_all_branches_is_false_with_an_extra_refspec(repo):
    """Verify a negative refspec beside the default one counts as narrowed."""
    _git(repo.clone, "config", "--add", "remote.origin.fetch", "^refs/heads/feat")

    assert Repository(repo.clone).fetches_all_branches() is False


def test_fetches_all_branches_raises_when_git_cannot_run(repo, tmp_path, monkeypatch):
    """Verify a git that cannot run is an error, not a narrowed refspec."""
    empty = tmp_path / "nogit"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    with pytest.raises(CommitsError):
        Repository(repo.clone).fetches_all_branches()


def test_rebased_copy_finds_each_commit_of_a_rebase_merge(repo):
    """Verify every original maps to its own copy, not to the last one."""
    originals, copies = rebase_merge(repo.clone)
    repository = Repository(repo.clone)

    found = [repository.rebased_copy(sha, copies[-1]) for sha in originals]

    assert found == copies


def test_rebased_copy_is_none_for_a_squash_merge(repo):
    """Verify a squashed commit has no copy, so the caller keeps the merge commit."""
    originals, squash = squash_merge(repo.clone)
    repository = Repository(repo.clone)

    assert [repository.rebased_copy(sha, squash) for sha in originals] == [None, None]


def test_rebased_copy_reads_each_merge_commit_once(repo, monkeypatch):
    """Verify the trunk window below one merge commit is read once per run."""
    originals, copies = rebase_merge(repo.clone)
    repository = Repository(repo.clone)
    calls: list[tuple[str, ...]] = []
    patch_ids = Repository._patch_ids

    def spy(self, *revs: str) -> dict[str, list[str]]:
        calls.append(revs)
        return patch_ids(self, *revs)

    monkeypatch.setattr(Repository, "_patch_ids", spy)

    for sha in originals:
        repository.rebased_copy(sha, copies[-1])

    assert sum(1 for revs in calls if copies[-1] in revs) == 1


def test_rebased_copy_ignores_a_same_patch_commit_from_before_the_fork(repo):
    """Verify an older trunk commit the original already contains is never its copy."""
    (repo.clone / "dup.txt").write_text("dup", encoding="utf-8")
    _git(repo.clone, "add", ".")
    _git(repo.clone, "commit", "-q", "-m", "dup early")
    _git(repo.clone, "rm", "-q", "dup.txt")
    _git(repo.clone, "commit", "-q", "-m", "drop dup")
    _git(repo.clone, "checkout", "-q", "-b", "pr")
    (repo.clone / "dup.txt").write_text("dup", encoding="utf-8")
    _git(repo.clone, "add", ".")
    _git(repo.clone, "commit", "-q", "-m", "dup again")
    original = _git(repo.clone, "rev-parse", "HEAD")
    _git(repo.clone, "checkout", "-q", "main")
    commit(repo.clone, "trunk-moves")
    _git(repo.clone, "cherry-pick", original)
    copy = _git(repo.clone, "rev-parse", "HEAD")

    assert Repository(repo.clone).rebased_copy(original, copy) == copy


def test_is_repository_accepts_a_symlinked_root(repo, tmp_path):
    """Verify a root reached through a symlink is still the top level."""
    link = tmp_path / "link"
    link.symlink_to(repo.clone)

    assert Repository(link).is_repository() is True


def test_is_repository_accepts_a_root_in_another_letter_case(repo):
    """Verify a root spelled in another case on a case-insensitive disk passes."""
    other = repo.clone.parent / repo.clone.name.upper()
    if not other.is_dir():
        pytest.skip("filesystem is case-sensitive")

    assert Repository(other).is_repository() is True
