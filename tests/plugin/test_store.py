"""Verify project-key derivation, XDG roots, and the Store path/IO helpers."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

from sessionhooks.store import (  # ty: ignore[unresolved-import]
    Store,
    encode_project_key,
    head_commit,
    project_root,
)

from tests._env import clean_environ
from tests.plugin._store_factory import store_at

_CLEAN = {"PATH": os.environ.get("PATH", "")}

# Strip the git location vars so test-spawned git commands target the tmp repo,
# not whatever checkout the suite runs from.
_GIT_ENV = clean_environ()


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, env=_GIT_ENV)


# ---------------------------------------------------------------------------
# encode_project_key
# ---------------------------------------------------------------------------


def test_encode_plain_path() -> None:
    """Verify a normal absolute path dash-encodes without a leading dash."""
    # Given a non-hidden absolute path / When encoded
    out = encode_project_key(Path("/Users/nate/repos/cc-plugin"))
    # Then slashes become dashes and the leading slash is dropped (no flag-like dash)
    assert out == "Users-nate-repos-cc-plugin"
    assert not out.startswith("-")


def test_encode_hidden_segment_double_dash() -> None:
    """Verify an interior leading-dot segment yields a double dash."""
    # Given a path containing a hidden directory / When encoded
    out = encode_project_key(Path("/Users/nate/.local/share/chezmoi/dotfiles"))
    # Then the /.local boundary becomes a double dash; interior dots are preserved
    assert out == "Users-nate--local-share-chezmoi-dotfiles"


def test_encode_interior_dot_preserved() -> None:
    """Verify a dot inside a segment is not doubled."""
    # Given a path with an interior dot / When encoded
    out = encode_project_key(Path("/srv/my.project/src"))
    # Then only the segment-leading dot rule applies (none here)
    assert out == "srv-my.project-src"


# ---------------------------------------------------------------------------
# project_root
# ---------------------------------------------------------------------------


def test_project_root_git_common_dir(tmp_path: Path) -> None:
    """Verify all worktrees of a repo resolve to the main worktree root."""
    # Given a git repo with a linked worktree
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q")
    (main / "f").write_text("x")
    _git(main, "add", "f")
    _git(main, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    wt = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", str(wt))

    # When resolving the root from inside the worktree
    root = project_root(cwd=wt, env=_CLEAN)

    # Then it is the MAIN worktree root, not the worktree dir
    assert root == main.resolve()


def test_project_root_non_git_uses_claude_project_dir(tmp_path: Path) -> None:
    """Verify a non-git dir falls back to CLAUDE_PROJECT_DIR."""
    # Given a non-git working dir and CLAUDE_PROJECT_DIR set to a project root
    proj = tmp_path / "proj"
    proj.mkdir()
    sub = proj / "sub"
    sub.mkdir()
    env = {**_CLEAN, "CLAUDE_PROJECT_DIR": str(proj)}

    # When resolving from a subdirectory
    root = project_root(cwd=sub, env=env)

    # Then the configured project root wins over raw cwd
    assert root == proj.resolve()


def test_project_root_ignores_ambient_git_dir(tmp_path: Path) -> None:
    """Verify project_root resolves cwd's repo, not a leaked ambient GIT_DIR."""
    # Given two separate repos and an env whose GIT_DIR names the other one. Git
    # exports GIT_DIR under a hook or worktree; if honored it would key the store
    # to the wrong project.
    here = tmp_path / "here"
    here.mkdir()
    _git(here, "init", "-q")
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    env = {**_CLEAN, "GIT_DIR": str(other / ".git")}

    # When resolving the root of `here` with the leaked GIT_DIR present
    root = project_root(cwd=here, env=env)

    # Then the cwd's own repo wins over the leaked GIT_DIR
    assert root == here.resolve()


def test_project_root_falls_back_when_git_cannot_run(tmp_path: Path) -> None:
    """Verify a cwd git cannot even chdir into falls back rather than raising."""
    # Given a cwd that does not exist, so the subprocess itself cannot start
    missing = tmp_path / "does-not-exist"
    env = {**_CLEAN, "CLAUDE_PROJECT_DIR": str(tmp_path)}

    # When resolving the root
    root = project_root(cwd=missing, env=env)

    # Then it falls through to CLAUDE_PROJECT_DIR rather than raising
    assert root == tmp_path.resolve()


# ---------------------------------------------------------------------------
# head_commit
# ---------------------------------------------------------------------------


def test_head_commit_returns_the_checked_out_sha(tmp_path: Path) -> None:
    """Verify head_commit reports the sha checked out at cwd."""
    # Given a repo with one commit
    _git(tmp_path, "init", "-q")
    (tmp_path / "f").write_text("x")
    _git(tmp_path, "add", "f")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")

    # When reading the head commit
    sha = head_commit(cwd=tmp_path, env=_CLEAN)

    # Then it is a full, non-empty sha
    assert len(sha) == 40


def test_head_commit_outside_a_repository_returns_empty(tmp_path: Path) -> None:
    """Verify a non-git directory yields '' rather than raising."""
    # Given a plain, non-git directory
    # When reading its head commit
    # Then it yields '' rather than raising
    assert head_commit(cwd=tmp_path, env=_CLEAN) == ""


def test_head_commit_returns_empty_when_git_cannot_run(tmp_path: Path) -> None:
    """Verify a cwd git cannot even chdir into yields '' rather than raising."""
    # Given a cwd that does not exist, so the subprocess itself cannot start
    missing = tmp_path / "does-not-exist"

    # When reading the head commit
    result = head_commit(cwd=missing, env=_CLEAN)

    # Then it yields '' rather than raising
    assert result == ""


# ---------------------------------------------------------------------------
# Store.for_cwd: XDG roots and key hashing
# ---------------------------------------------------------------------------


def test_for_cwd_key_encodes_the_project_root(tmp_path: Path) -> None:
    """Verify the store key is the encoded project root, whatever the XDG roots are."""
    # Given a non-git project
    proj = tmp_path / "proj"
    proj.mkdir()
    env = {**_CLEAN, "CLAUDE_PROJECT_DIR": str(proj), "XDG_STATE_HOME": str(tmp_path / "xdg")}

    # When building the store for that cwd
    store = Store.for_cwd(cwd=proj, env=env)

    # Then the key is derived from the resolved root
    assert store.key == encode_project_key(proj.resolve())


def test_for_cwd_state_dir_hashes_key(tmp_path: Path) -> None:
    """Verify the state dir nests a 12-char sha1 of the key under $XDG_STATE_HOME."""
    # Given a non-git project and an explicit XDG_STATE_HOME
    proj = tmp_path / "proj"
    proj.mkdir()
    env = {**_CLEAN, "CLAUDE_PROJECT_DIR": str(proj), "XDG_STATE_HOME": str(tmp_path / "st")}

    # When building the store
    store = Store.for_cwd(cwd=proj, env=env)

    # Then the state dir is hashed (not the raw key) under the plugin namespace
    expected_hash = hashlib.sha1(store.key.encode("utf-8")).hexdigest()[:12]  # noqa: S324
    assert store.state_dir == tmp_path / "st" / "sessionmemory" / expected_hash


# ---------------------------------------------------------------------------
# Store path accessors + IO helpers
# ---------------------------------------------------------------------------


def test_path_accessors(tmp_path: Path) -> None:
    """Verify the path properties point at the expected store locations."""
    # Given a store rooted at tmp dirs
    store = store_at(tmp_path)
    # Then each accessor composes the right path
    assert store.handoff_path == tmp_path / "state" / "HANDOFF.md"
    assert store.base_commit_path == tmp_path / "state" / "base-commit"
    assert store.lock_path == tmp_path / "state" / "sweep.lock"
    assert store.transcript_pointer_path == tmp_path / "state" / "transcript-path"
    assert store.log_path == tmp_path / "state" / "sweep.log"


def test_save_and_read_transcript_pointer(tmp_path: Path) -> None:
    """Verify the transcript pointer round-trips through the state dir."""
    # Given a store with no state dir yet
    store = store_at(tmp_path)
    # When saving a transcript path
    store.save_transcript_pointer("/tmp/x/t.jsonl")  # noqa: S108
    # Then it is read back verbatim (mkdir handled internally)
    assert store.read_transcript_pointer() == "/tmp/x/t.jsonl"  # noqa: S108


def test_read_transcript_pointer_missing_returns_empty(tmp_path: Path) -> None:
    """Verify reading an absent pointer returns '' rather than raising."""
    # Given a store whose pointer was never written
    store = store_at(tmp_path)
    # Then reading fails open to an empty string
    assert store.read_transcript_pointer() == ""


def _seed_handoff(store: Store, text: str) -> None:
    store.state_dir.mkdir(parents=True, exist_ok=True)
    store.handoff_path.write_text(text, encoding="utf-8")


def test_read_handoff_returns_contents(tmp_path: Path) -> None:
    """Verify read_handoff returns the handoff contents verbatim when present."""
    # Given a store with a seeded handoff
    store = store_at(tmp_path)
    _seed_handoff(store, "# Handoff\nbody")
    # Then the contents come back verbatim
    assert store.read_handoff() == "# Handoff\nbody"


def test_read_handoff_missing_returns_none(tmp_path: Path) -> None:
    """Verify read_handoff fails open to None when no handoff exists."""
    # Given a store with no handoff
    store = store_at(tmp_path)
    # Then reading returns None rather than raising
    assert store.read_handoff() is None


def test_read_handoff_empty_returns_none(tmp_path: Path) -> None:
    """Verify an empty handoff file reads as None (nothing to carry)."""
    # Given a store with an empty handoff file
    store = store_at(tmp_path)
    _seed_handoff(store, "")
    # Then it is treated as nothing to carry
    assert store.read_handoff() is None


def test_read_handoff_invalid_utf8_returns_none(tmp_path: Path) -> None:
    """Verify a handoff with invalid UTF-8 fails open to None rather than raising."""
    # Given a store whose handoff holds bytes that are not valid UTF-8
    store = store_at(tmp_path)
    store.state_dir.mkdir(parents=True, exist_ok=True)
    store.handoff_path.write_bytes(b"\xff\xfe bad bytes")
    # Then reading it is nothing-to-carry, not a crash
    assert store.read_handoff() is None


def test_delete_handoff_removes_file(tmp_path: Path) -> None:
    """Verify delete_handoff removes the handoff file."""
    # Given a store with a seeded handoff
    store = store_at(tmp_path)
    _seed_handoff(store, "x")
    # When deleting it
    store.delete_handoff()
    # Then the file is gone
    assert not store.handoff_path.exists()


def test_delete_handoff_missing_is_noop(tmp_path: Path) -> None:
    """Verify delete_handoff is a no-op (never raises) when the file is already gone."""
    # Given a store with no handoff
    store = store_at(tmp_path)
    # Then deleting it does not raise
    store.delete_handoff()
    assert not store.handoff_path.exists()


def test_save_transcript_pointer_ignores_empty(tmp_path: Path) -> None:
    """Verify saving an empty transcript path writes nothing."""
    # Given a store
    store = store_at(tmp_path)
    # When saving an empty path
    store.save_transcript_pointer("")
    # Then no pointer file is created
    assert not store.transcript_pointer_path.exists()


def test_base_commit_round_trips(tmp_path: Path) -> None:
    """Verify the starting commit survives from session start to the sweep."""
    # Given a store with no state dir yet
    store = store_at(tmp_path)
    # When the starting commit is recorded
    store.save_base_commit("abc123")
    # Then it reads back verbatim (mkdir handled internally)
    assert store.read_base_commit() == "abc123"


def test_base_commit_missing_reads_as_empty(tmp_path: Path) -> None:
    """Verify an absent record fails open, so a log still gets written without it."""
    # Given a store that never recorded one
    store = store_at(tmp_path)
    # Then reading it yields nothing rather than raising
    assert store.read_base_commit() == ""


def test_an_empty_base_commit_is_not_written(tmp_path: Path) -> None:
    """Verify a repository with no commits leaves no misleading empty record."""
    # Given a store
    store = store_at(tmp_path)
    # When there is no commit to record
    store.save_base_commit("")
    # Then no file is created
    assert not store.base_commit_path.exists()


def test_save_transcript_pointer_swallows_an_os_error(tmp_path: Path) -> None:
    """Verify a write the OS refuses is swallowed rather than raised."""
    # Given a state dir path already occupied by a plain file, so mkdir fails
    state_dir = tmp_path / "state"
    state_dir.parent.mkdir(parents=True, exist_ok=True)
    state_dir.write_text("blocker", encoding="utf-8")
    store = Store(key="k", state_dir=state_dir)
    # When saving a transcript path, it must not raise
    store.save_transcript_pointer("/tmp/x/t.jsonl")  # noqa: S108
    # Then nothing was recorded
    assert store.read_transcript_pointer() == ""


def test_save_base_commit_swallows_an_os_error(tmp_path: Path) -> None:
    """Verify a write the OS refuses is swallowed rather than raised."""
    # Given a state dir path already occupied by a plain file, so mkdir fails
    state_dir = tmp_path / "state"
    state_dir.parent.mkdir(parents=True, exist_ok=True)
    state_dir.write_text("blocker", encoding="utf-8")
    store = Store(key="k", state_dir=state_dir)
    # When saving a base commit, it must not raise
    store.save_base_commit("abc123")
    # Then nothing was recorded
    assert store.read_base_commit() == ""
