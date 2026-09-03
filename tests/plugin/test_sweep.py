"""Verify the sweep: lock lifecycle, gate threshold, write validation, and run_job."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sessionhooks import sweep as sweep_mod  # ty: ignore[unresolved-import]
from sessionhooks.config import SessionMemoryConfig  # ty: ignore[unresolved-import]
from sessionhooks.runner import RunResult  # ty: ignore[unresolved-import]
from sessionhooks.sweep import (  # ty: ignore[unresolved-import]
    Lock,
    Sweep,
    SweepJob,
    Target,
    in_progress,
)

from tests._env import clean_environ
from tests.plugin._store_factory import store_at

if TYPE_CHECKING:
    from pytest_mock import MockerFixture
    from sessionhooks.store import Store  # ty: ignore[unresolved-import]


class _FakeRunner:
    """Duck-typed Runner that reports a canned set of changed files; never spawns claude."""

    def __init__(self, changed_files: list[str]) -> None:
        self.changed_files = changed_files

    def run(self, prompt: str, *, cwd: str) -> RunResult:
        return RunResult(
            success=True,
            exit_code=0,
            changed_files=list(self.changed_files),
            text="done",
            stderr="",
        )


# ---------------------------------------------------------------------------
# Target
# ---------------------------------------------------------------------------


def test_target_is_built_from_the_flattened_resolve_payload():
    """Verify Target reads the flattened resolve payload and carries its one write root."""
    target = Target.from_paths(
        {
            "project_dir": "/v/projects/demo",
            "learnings": "/v/projects/demo/learnings",
            "backlog": "/v/projects/demo/backlog.md",
            "logs": "/v/projects/demo/logs",
        }
    )

    assert target.project_dir == Path("/v/projects/demo")
    assert target.backlog_path == Path("/v/projects/demo/backlog.md")
    assert target.roots == (Path("/v/projects/demo"),)


def test_target_needs_every_key():
    """Verify Target.from_paths yields None when the payload is missing a required key."""
    assert Target.from_paths({"project_dir": "/v/projects/demo"}) is None


# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------


def test_lock_acquire_succeeds_first(tmp_path: Path) -> None:
    """Verify acquire returns True and writes the timestamp when the lock is free."""
    # Given a lock path under a not-yet-created dir
    lock = Lock(tmp_path / "state" / "sweep.lock")
    # When acquiring for the first time
    assert lock.acquire(now=1000.0) is True
    # Then the file exists and stores the timestamp
    assert float(lock.path.read_text(encoding="utf-8").strip()) == 1000.0


def test_lock_held_second_acquire_fails(tmp_path: Path) -> None:
    """Verify a second acquire within the stale window returns False."""
    # Given an already-held fresh lock
    lock = Lock(tmp_path / "state" / "sweep.lock")
    assert lock.acquire(now=1000.0) is True
    # When acquiring again well within the stale window
    second = Lock(tmp_path / "state" / "sweep.lock")
    # Then it fails (the lock is still fresh)
    assert second.acquire(now=1100.0) is False


def test_lock_steals_stale(tmp_path: Path) -> None:
    """Verify a lock older than stale_after is stolen and re-stamped."""
    # Given a lock acquired at t=1000 with a 300s window
    Lock(tmp_path / "state" / "sweep.lock").acquire(now=1000.0)
    # When acquiring 400s later (past the threshold)
    stealer = Lock(tmp_path / "state" / "sweep.lock", stale_after=300.0)
    # Then the stale lock is stolen and the new timestamp written
    assert stealer.acquire(now=1400.0) is True
    assert float(stealer.path.read_text(encoding="utf-8").strip()) == 1400.0


def test_lock_steals_malformed(tmp_path: Path) -> None:
    """Verify a lock with a non-numeric timestamp is treated as stale and stolen."""
    # Given an existing lock file with corrupt content
    lock = Lock(tmp_path / "state" / "sweep.lock")
    lock.path.parent.mkdir(parents=True)
    lock.path.write_text("not-a-float", encoding="utf-8")
    # When acquiring (corrupt parses to stored=0.0, always stale)
    assert lock.acquire(now=400.0) is True


def test_lock_release_removes_file(tmp_path: Path) -> None:
    """Verify release unlinks the lock file and never raises when already gone."""
    # Given a held lock
    lock = Lock(tmp_path / "state" / "sweep.lock")
    lock.acquire(now=1.0)
    # When released twice
    lock.release()
    lock.release()
    # Then the file is gone and no error was raised
    assert not lock.path.exists()


def test_lock_acquire_fails_when_the_parent_directory_cannot_be_created(tmp_path: Path) -> None:
    """Verify acquire fails open rather than raising when mkdir cannot succeed."""
    # Given a plain file occupying the path the lock's parent directory needs
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    lock = Lock(blocker / "sweep.lock")
    # Then acquiring never raises, and reports failure
    assert lock.acquire(now=1.0) is False


def test_lock_acquire_fails_when_the_stale_lock_cannot_be_unlinked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify a steal that cannot remove the stale file fails open rather than raising."""
    # Given a stale lock the steal attempt cannot unlink
    lock_path = tmp_path / "state" / "sweep.lock"
    Lock(lock_path).acquire(now=1.0)
    stealer = Lock(lock_path, stale_after=0.0)

    def _raise(self, *_args, **_kwargs) -> None:
        message = "cannot unlink"
        raise OSError(message)

    monkeypatch.setattr(Path, "unlink", _raise)

    # Then the steal fails rather than raising
    assert stealer.acquire(now=1000.0) is False


def test_lock_acquire_fails_when_recreation_after_a_steal_loses_a_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify acquire fails open when re-creating the lock after a steal cannot succeed.

    os.open is forced to always fail, so the first _try_create (before the
    steal) fails for the same reason a real race would, and so does the
    second _try_create right after the stale lock is unlinked.
    """
    # Given a stale lock to steal, and os.open forced to always fail
    lock_path = tmp_path / "state" / "sweep.lock"
    Lock(lock_path).acquire(now=1.0)  # a stale lock to steal

    def _raise(*_args, **_kwargs) -> None:
        message = "cannot open"
        raise OSError(message)

    monkeypatch.setattr(os, "open", _raise)
    stealer = Lock(lock_path, stale_after=0.0)

    # When acquiring, well past the stale window
    result = stealer.acquire(now=1000.0)

    # Then acquire fails open, and the stale lock was still removed even
    # though recreation lost the race
    assert result is False
    assert not lock_path.exists()


def test_lock_acquire_fails_when_the_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify a write failure after creating the lock file fails open, not raises."""
    # Given a fresh lock and os.write forced to always fail
    lock = Lock(tmp_path / "state" / "sweep.lock")

    def _raise(*_args, **_kwargs) -> None:
        message = "cannot write"
        raise OSError(message)

    monkeypatch.setattr(os, "write", _raise)

    # When acquiring / Then it fails open rather than raising
    assert lock.acquire(now=1.0) is False


# ---------------------------------------------------------------------------
# in_progress
# ---------------------------------------------------------------------------


def test_in_progress_true_for_a_fresh_lock(tmp_path: Path) -> None:
    """Verify a lock stamped just now reads as a worker still holding it."""
    # Given a store whose lock a worker acquired a moment ago
    store = store_at(tmp_path)
    Lock(store.lock_path).acquire(now=1000.0)
    # When checking shortly after / Then a worker is reported in progress
    assert in_progress(store, now=1001.0) is True


def test_in_progress_false_for_a_stale_lock(tmp_path: Path) -> None:
    """Verify a lock older than stale_after reads as no worker holding it."""
    # Given a store whose lock was stamped well in the past
    store = store_at(tmp_path)
    Lock(store.lock_path).acquire(now=1000.0)
    # When checking past the stale window / Then no worker is reported
    assert in_progress(store, now=2000.0, stale_after=300.0) is False


def test_in_progress_false_when_no_lock_exists(tmp_path: Path) -> None:
    """Verify a store with no lock file reads as no worker holding it."""
    # Given a store with no lock file at all
    store = store_at(tmp_path)
    # When checking / Then no worker is reported
    assert in_progress(store, now=1000.0) is False


def test_in_progress_false_for_a_malformed_lock(tmp_path: Path) -> None:
    """Verify a lock file with non-numeric content reads as no worker holding it."""
    # Given a lock file with corrupt content
    store = store_at(tmp_path)
    store.state_dir.mkdir(parents=True)
    store.lock_path.write_text("not-a-float", encoding="utf-8")
    # When checking / Then no worker is reported
    assert in_progress(store, now=1000.0) is False


# ---------------------------------------------------------------------------
# Sweep._gate
# ---------------------------------------------------------------------------


def _user(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _assistant(text: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def _meaningful(n: int) -> list[dict]:
    # User turns are verbose enough to clear the substance floors, so these cases
    # exercise the message-count threshold in isolation.
    return [
        _user(f"u{i} " + "detail " * 40) if i % 2 == 0 else _assistant(f"a{i}") for i in range(n)
    ]


def _write_transcript(path: Path, entries: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


def test_gate_below_threshold_returns_none_and_releases(tmp_path: Path) -> None:
    """Verify gate returns None and releases the lock below min_exchanges."""
    # Given a sparse transcript (2 meaningful) and a threshold of 5
    store = store_at(tmp_path)
    t_file = tmp_path / "sparse.jsonl"
    _write_transcript(t_file, [_user("hi"), _assistant("yo")])
    sweep = Sweep(store, SessionMemoryConfig(min_exchanges=5), _FakeRunner([]))
    event = {"cwd": str(tmp_path), "transcript_path": str(t_file)}
    # When gating
    result = sweep._gate(event, now=1000.0)
    # Then None is returned and the lock is cleaned up
    assert result is None
    assert not store.lock_path.exists()


def test_gate_above_threshold_returns_job_and_holds_lock(tmp_path: Path) -> None:
    """Verify gate returns a SweepJob with the window and keeps the lock held."""
    # Given a rich transcript (6 meaningful, threshold 5)
    store = store_at(tmp_path)
    entries = _meaningful(6)
    t_file = tmp_path / "rich.jsonl"
    _write_transcript(t_file, entries)
    sweep = Sweep(store, SessionMemoryConfig(min_exchanges=5), _FakeRunner([]))
    event = {"cwd": str(tmp_path / "proj"), "transcript_path": str(t_file)}
    # When gating
    result = sweep._gate(event, now=1000.0)
    # Then a job covering all entries is returned and the lock stays held for run_job
    assert result is not None
    assert result.cwd == str(tmp_path / "proj")
    assert len(result.window) == len(entries)
    assert store.lock_path.exists()


def test_gate_falls_back_to_transcript_pointer(tmp_path: Path) -> None:
    """Verify gate reads the saved pointer when the event transcript path is empty."""
    # Given a store with a saved transcript pointer
    store = store_at(tmp_path)
    entries = _meaningful(6)
    t_file = tmp_path / "session.jsonl"
    _write_transcript(t_file, entries)
    store.save_transcript_pointer(str(t_file))
    sweep = Sweep(store, SessionMemoryConfig(min_exchanges=5), _FakeRunner([]))
    # When gating with an empty event transcript path
    result = sweep._gate({"cwd": str(tmp_path), "transcript_path": ""}, now=1000.0)
    # Then the job is built from the pointer's transcript
    assert result is not None
    assert len(result.window) == len(entries)


def test_gate_rejects_a_session_the_human_barely_spoke_in(tmp_path: Path) -> None:
    """Verify a long agent monologue over one human message never earns a sweep."""
    # Given one human message answered at length, clearing the message-count floor
    store = store_at(tmp_path)
    t_file = tmp_path / "monologue.jsonl"
    entries = [_user("update my plugins")] + [_assistant("x" * 500) for _ in range(9)]
    _write_transcript(t_file, entries)
    sweep = Sweep(store, SessionMemoryConfig(min_exchanges=5), _FakeRunner([]))
    # When gating
    result = sweep._gate({"cwd": str(tmp_path), "transcript_path": str(t_file)}, now=1000.0)
    # Then the human's single message fails the substance floor
    assert result is None
    assert not store.lock_path.exists()


def test_gate_rejects_a_session_of_terse_commands(tmp_path: Path) -> None:
    """Verify several one-word instructions do not add up to a loggable session."""
    # Given many short exchanges that clear both the count floors but say nothing
    store = store_at(tmp_path)
    t_file = tmp_path / "terse.jsonl"
    entries = [_user("ok"), _assistant("done")] * 6
    _write_transcript(t_file, entries)
    sweep = Sweep(store, SessionMemoryConfig(min_exchanges=5), _FakeRunner([]))
    # When gating
    result = sweep._gate({"cwd": str(tmp_path), "transcript_path": str(t_file)}, now=1000.0)
    # Then the character floor rejects it
    assert result is None
    assert not store.lock_path.exists()


def test_gate_accepts_an_exploratory_session_with_no_commits(tmp_path: Path) -> None:
    """Verify substance, not committed work, is what earns a sweep."""
    # Given a genuine question-and-answer session that changed no code
    store = store_at(tmp_path)
    t_file = tmp_path / "explore.jsonl"
    entries = [
        _user("why does the Company model have so many edges? " + "explain more. " * 20),
        _assistant("Because every child model carries the FK."),
        _user("trace it for me and show the betweenness maths " + "in detail. " * 20),
        _assistant("Production-only betweenness drops to 0.0832."),
    ]
    _write_transcript(t_file, entries)
    sweep = Sweep(store, SessionMemoryConfig(min_exchanges=4, min_user_messages=2), _FakeRunner([]))
    # When gating
    result = sweep._gate({"cwd": str(tmp_path), "transcript_path": str(t_file)}, now=1000.0)
    # Then it is swept despite no commits
    assert result is not None
    assert store.lock_path.exists()


def test_gate_returns_none_when_the_lock_is_already_held(tmp_path: Path) -> None:
    """Verify a session cannot gate while another sweep already holds the lock."""
    # Given a lock another process holds, fresh
    store = store_at(tmp_path)
    Lock(store.lock_path).acquire(now=1000.0)
    sweep = Sweep(store, SessionMemoryConfig(min_exchanges=1), _FakeRunner([]))
    # When gating shortly after
    result = sweep._gate({"cwd": str(tmp_path), "transcript_path": ""}, now=1000.5)
    # Then nothing is returned, and the other holder's lock is left alone
    assert result is None
    assert store.lock_path.exists()


def test_gate_releases_the_lock_and_returns_none_on_an_unexpected_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify the gate never raises or leaks the lock when something inside it breaks."""
    # Given a rich transcript that would otherwise gate through
    store = store_at(tmp_path)
    entries = _meaningful(6)
    t_file = tmp_path / "rich.jsonl"
    _write_transcript(t_file, entries)
    sweep = Sweep(store, SessionMemoryConfig(min_exchanges=5), _FakeRunner([]))

    def _raise(_entries) -> None:
        message = "boom"
        raise RuntimeError(message)

    monkeypatch.setattr(sweep_mod.transcript, "window_since_compact", _raise)

    # When gating
    result = sweep._gate({"cwd": str(tmp_path), "transcript_path": str(t_file)}, now=1000.0)

    # Then the error is swallowed and the lock is released
    assert result is None
    assert not store.lock_path.exists()


def test_trigger_spawns_the_detached_worker_when_the_gate_yields_a_job(tmp_path: Path) -> None:
    """Verify trigger hands a gated job off to be spawned."""
    # Given a rich transcript that clears the gate
    store = store_at(tmp_path)
    entries = _meaningful(6)
    t_file = tmp_path / "rich.jsonl"
    _write_transcript(t_file, entries)
    sweep = Sweep(store, SessionMemoryConfig(min_exchanges=5), _FakeRunner([]))
    spawned: list[SweepJob] = []
    sweep._spawn_detached = spawned.append  # type: ignore[method-assign]

    # When triggering
    sweep.trigger({"cwd": str(tmp_path), "transcript_path": str(t_file)})

    # Then the gated job was handed to the spawn step
    assert len(spawned) == 1
    assert spawned[0].cwd == str(tmp_path)


def test_trigger_never_raises_when_the_gate_itself_blows_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture
) -> None:
    """Verify a hook calling trigger is never at risk from an internal sweep failure.

    The failure is forced inside the real `_gate`, not by replacing `_gate`
    itself, so `_gate`'s own lock-release-on-error path actually runs; a
    faked `_gate` would never have acquired the lock it was meant to protect.
    """
    # Given a rich transcript that would otherwise gate through, an internal
    # failure inside _gate, and _spawn_detached spied on so nothing forks
    store = store_at(tmp_path)
    entries = _meaningful(6)
    t_file = tmp_path / "rich.jsonl"
    _write_transcript(t_file, entries)
    sweep = Sweep(store, SessionMemoryConfig(min_exchanges=5), _FakeRunner([]))
    spawn = mocker.patch.object(sweep, "_spawn_detached", autospec=True)

    def _raise(_entries) -> None:
        message = "boom"
        raise RuntimeError(message)

    monkeypatch.setattr(sweep_mod.transcript, "window_since_compact", _raise)

    # When triggering
    sweep.trigger({"cwd": str(tmp_path), "transcript_path": str(t_file)})  # must not raise

    # Then no worker is spawned and the lock the gate acquired is released
    spawn.assert_not_called()
    assert not store.lock_path.exists()


# ---------------------------------------------------------------------------
# Sweep._git_context / _changes (real git subprocesses)
# ---------------------------------------------------------------------------


def test_git_context_reports_recent_commit_subjects(tmp_path: Path) -> None:
    """Verify _git_context surfaces recent commit history for the sweep prompt."""
    # Given a repo with one commit
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "--allow-empty",
            "-qm",
            "hello world",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    # When reading git context / Then the commit subject is present
    assert "hello world" in sweep_mod._git_context(str(tmp_path))


def test_git_context_returns_empty_when_git_cannot_run(tmp_path: Path) -> None:
    """Verify a cwd git cannot even chdir into yields '' rather than raising."""
    # Given a cwd that does not exist, so the subprocess itself cannot start
    missing = tmp_path / "does-not-exist"

    # When reading git context / Then it yields '' rather than raising
    assert sweep_mod._git_context(str(missing)) == ""


def test_changes_lists_commit_subjects_since_the_base(tmp_path: Path) -> None:
    """Verify _changes reports what happened in the repo since the session started."""
    # Given a repo whose base commit is recorded, then a second commit lands
    store = store_at(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "--allow-empty",
            "-qm",
            "base",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout.strip()
    store.save_base_commit(base)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "--allow-empty",
            "-qm",
            "second change",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    sweep = Sweep(store, SessionMemoryConfig(), _FakeRunner([]))

    # When listing changes since the base
    changes = sweep._changes(_job(cwd=str(tmp_path)))

    # Then the second commit's subject is reported
    assert "second change" in changes


def test_changes_returns_empty_when_git_cannot_run(tmp_path: Path) -> None:
    """Verify a base commit recorded but an unrunnable git yields '' rather than raising."""
    # Given a recorded base commit but a cwd that does not exist
    store = store_at(tmp_path)
    store.save_base_commit("deadbeef")
    sweep = Sweep(store, SessionMemoryConfig(), _FakeRunner([]))

    # When listing changes / Then it yields '' rather than raising
    changes = sweep._changes(_job(cwd=str(tmp_path / "does-not-exist")))

    assert changes == ""


# ---------------------------------------------------------------------------
# Sweep._validate_writes (containment + secret scrub)
# ---------------------------------------------------------------------------


def _target_at(root: Path, *, logs: Path | None = None) -> Target:
    """A vault target rooted at `root`, with its project folders created."""
    project = root / "projects" / "proj"
    (project / "learnings").mkdir(parents=True, exist_ok=True)
    logs_dir = logs if logs is not None else project / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return Target(
        project_dir=project,
        learnings_dir=project / "learnings",
        backlog_path=project / "backlog.md",
        logs_dir=logs_dir,
    )


def _job(*, cwd: str, session_id: str = "") -> SweepJob:
    """A minimal SweepJob for tests that only exercise command/log composition."""
    return SweepJob(window=[], cwd=cwd, session_id=session_id)


@pytest.fixture
def sweep_factory(tmp_path: Path):
    """Build a Sweep with a throwaway store and no runner writes, for tests with no vault."""

    def _make() -> Sweep:
        return Sweep(store_at(tmp_path), SessionMemoryConfig(), _FakeRunner([]))

    return _make


def _sweep_with_data(tmp_path: Path) -> tuple[Sweep, Target]:
    store = store_at(tmp_path)
    target = _target_at(tmp_path / "vault")
    return Sweep(store, SessionMemoryConfig(), _FakeRunner([])), target


def test_validate_writes_escaped_file_quarantined(tmp_path: Path) -> None:
    """Verify a changed file outside every allowed root is quarantined, not deleted.

    Git has nothing to restore this path from (it is outside the vault
    entirely), and nothing snapshotted it before the run either, so there is no
    way to prove its content is safe to erase; it must survive somewhere.
    """
    # Given a file outside the vault
    sweep, target = _sweep_with_data(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("nope", encoding="utf-8")
    # When validating
    notes = sweep._validate_writes([str(outside)], target, started_at=0.0)
    # Then the file is gone from its original path, its content survives in
    # quarantine, and the note says so
    assert not outside.exists()
    assert notes == [f"escaped-quarantined: {outside}"]
    quarantined = list((sweep.store.state_dir / "quarantine").glob("*-outside.md"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "nope"


def test_validate_writes_rejects_a_sibling_project(tmp_path: Path) -> None:
    """Verify one session cannot write into another project's folder.

    The sibling note may predate this run and simply never have been
    committed, so it is quarantined rather than deleted: nothing here can tell
    a note this session created from one an earlier, uncommitted sweep left
    behind.
    """
    # Given a file written into a different project's vault folder
    sweep, target = _sweep_with_data(tmp_path)
    other = target.project_dir.parent / "other" / "learnings" / "x.md"
    other.parent.mkdir(parents=True)
    other.write_text("not mine", encoding="utf-8")
    # When validating
    notes = sweep._validate_writes([str(other)], target, started_at=0.0)
    # Then it is gone from the sibling project's folder, its content survives
    # in quarantine, and the note says so
    assert not other.exists()
    assert notes == [f"escaped-quarantined: {other}"]
    quarantined = list((sweep.store.state_dir / "quarantine").glob("*-x.md"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "not mine"


def test_validate_writes_anchors_a_relative_path_to_the_project(tmp_path: Path) -> None:
    """Verify a relative write is judged inside the vault, not against the worker's cwd."""
    # Given a note the agent reports by a path relative to the folder it ran in
    sweep, target = _sweep_with_data(tmp_path)
    written = target.project_dir / "learnings" / "rel.md"
    written.write_text("a learning", encoding="utf-8")
    # When validating
    notes = sweep._validate_writes(["learnings/rel.md"], target, started_at=0.0)
    # Then it is treated as contained and left alone
    assert written.exists()
    assert notes == []


def test_validate_writes_restores_a_tracked_escape(tmp_path: Path) -> None:
    """Verify an escaped edit to an existing vault note is restored, never deleted."""
    # Given a committed note outside this project's roots that the agent overwrote
    vault_root = tmp_path / "vault"
    target = _target_at(vault_root)
    tracked = vault_root / "global" / "shared.md"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("the original", encoding="utf-8")
    git_env = clean_environ()
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@e.com"],
        ["config", "user.name", "T"],
        ["add", "-A"],
        ["commit", "-qm", "seed"],
    ):
        subprocess.run(["git", *args], cwd=vault_root, check=True, capture_output=True, env=git_env)
    tracked.write_text("steered by injection", encoding="utf-8")
    sweep = Sweep(
        store_at(tmp_path),
        SessionMemoryConfig(),
        _FakeRunner([]),
        _FakeVault(target, root=vault_root),
    )
    # When validating
    notes = sweep._validate_writes([str(tracked)], target, started_at=0.0)
    # Then the note survives with its committed content
    assert tracked.read_text(encoding="utf-8") == "the original"
    assert notes == [f"escaped-reverted: {tracked}"]


@pytest.mark.parametrize(
    ("name", "secret", "content"),
    [
        ("aws", "AKIAIOSFODNN7EXAMPLE", "key: {secret}"),
        ("github", "ghp_" + "a" * 36, "token: {secret}"),
        # Split so a secret scanner does not flag this file as holding a real key.
        ("pem", "-----BEGIN RSA " + "PRIVATE KEY-----", "{secret}\nMIIEow==\n"),
        ("apikey", "s3cr3tv4lu3" + "x" * 12, 'api_key = "{secret}"'),
    ],
)
def test_validate_writes_redacts_a_secret(
    tmp_path: Path, name: str, secret: str, content: str
) -> None:
    """Verify a credential written inside an allowed root is redacted in place."""
    # Given a note in the project folder containing a credential
    sweep, target = _sweep_with_data(tmp_path)
    written = target.project_dir / f"{name}.md"
    written.write_text(content.format(secret=secret), encoding="utf-8")
    # When validating
    notes = sweep._validate_writes([str(written)], target, started_at=0.0)
    # Then the secret is gone, the file remains, and the redaction is noted
    after = written.read_text(encoding="utf-8")
    assert secret not in after
    assert "«redacted-secret»" in after
    assert notes == [f"secret-redacted: {written}"]


def test_validate_writes_clean_file_untouched(tmp_path: Path) -> None:
    """Verify a clean file inside an allowed root is left byte-identical with no note."""
    # Given a clean note in the project folder
    sweep, target = _sweep_with_data(tmp_path)
    written = target.project_dir / "clean.md"
    written.write_text("nothing secret here", encoding="utf-8")
    # When validating
    notes = sweep._validate_writes([str(written)], target, started_at=0.0)
    # Then it is unchanged and unremarked
    assert written.read_text(encoding="utf-8") == "nothing secret here"
    assert notes == []


def test_validate_writes_missing_file_no_note(tmp_path: Path) -> None:
    """Verify a reported path that was never written produces no note."""
    # Given a path inside the project folder that does not exist
    sweep, target = _sweep_with_data(tmp_path)
    ghost = target.project_dir / "ghost.md"
    # When validating
    notes = sweep._validate_writes([str(ghost)], target, started_at=0.0)
    # Then nothing is reported
    assert notes == []


def test_validate_writes_reports_escaped_for_a_reported_but_nonexistent_path(
    tmp_path: Path,
) -> None:
    """Verify a reported path outside every root that was never actually written just reports escaped.

    There is nothing to restore from git (no vault) and nothing to quarantine
    (the path does not exist), so the only honest outcome is naming it escaped.
    """
    # Given a path outside every root that was reported changed but never written
    sweep, target = _sweep_with_data(tmp_path)
    ghost = tmp_path / "outside-ghost.md"

    # When validating writes
    notes = sweep._validate_writes([str(ghost)], target, started_at=0.0)

    # Then it is reported escaped, with nothing quarantined
    assert notes == [f"escaped: {ghost}"]
    assert not (sweep.store.state_dir / "quarantine").exists()


def test_revert_and_note_reports_unremovable_when_git_cannot_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify a revert that cannot even spawn git is reported, not raised."""
    # Given a vault present and subprocess.run replaced with one that always raises
    sweep, target = _sweep_with_data(tmp_path)
    sweep.vault = _FakeVault(target, root=tmp_path / "vault")
    tracked = sweep.vault.root / "shared.md"

    def _raise(*_args, **_kwargs) -> None:
        message = "git executable vanished"
        raise OSError(message)

    monkeypatch.setattr(sweep_mod.subprocess, "run", _raise)

    # When reverting and noting the outcome
    note = sweep._revert_and_note(tracked, str(tracked))

    # Then the failure is reported as unremovable rather than raised
    assert note.startswith(f"escaped-unremovable: {tracked}")


# ---------------------------------------------------------------------------
# Sweep._existing_log
# ---------------------------------------------------------------------------


def test_existing_log_returns_empty_when_the_logs_dir_cannot_be_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify a logs directory that cannot be globbed fails open to ''."""
    # Given a sweep and Path.glob forced to always fail
    sweep, target = _sweep_with_data(tmp_path)

    def _raise(self, _pattern) -> None:
        message = "cannot list"
        raise OSError(message)

    monkeypatch.setattr(Path, "glob", _raise)

    # When reading the existing log / Then it fails open to ''
    assert sweep._existing_log(target, "abc123") == ""


def test_existing_log_skips_a_file_it_cannot_read(tmp_path: Path) -> None:
    """Verify a candidate log file that cannot be decoded is skipped, not fatal."""
    # Given one candidate log with undecodable bytes and one that matches the session
    sweep, target = _sweep_with_data(tmp_path)
    (target.logs_dir / "bad.md").write_bytes(b"\xff\xfe bad bytes")
    (target.logs_dir / "good.md").write_text("session_id: abc123\nthe body", encoding="utf-8")

    # When reading the existing log
    result = sweep._existing_log(target, "abc123")

    # Then the undecodable file is skipped and the matching one is returned
    assert "the body" in result


def test_existing_log_skips_files_for_a_different_session(tmp_path: Path) -> None:
    """Verify a log matching a different session_id is passed over for one that matches."""
    # Given one log for a different session and one for the requested session
    sweep, target = _sweep_with_data(tmp_path)
    (target.logs_dir / "a.md").write_text("session_id: other\nirrelevant", encoding="utf-8")
    (target.logs_dir / "b.md").write_text("session_id: abc123\nmine", encoding="utf-8")

    # When reading the existing log
    result = sweep._existing_log(target, "abc123")

    # Then only the matching session's log is returned
    assert "mine" in result


# ---------------------------------------------------------------------------
# Sweep._touched_since
# ---------------------------------------------------------------------------


def test_touched_since_returns_empty_for_a_missing_directory(tmp_path: Path) -> None:
    """Verify a directory that was never created reads as having nothing touched."""
    # Given a directory that was never created
    sweep, target = _sweep_with_data(tmp_path)
    missing = target.project_dir / "does-not-exist"

    # When listing what was touched / Then nothing is reported
    assert sweep._touched_since(missing, 0.0) == []


def test_touched_since_returns_empty_when_listing_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify a directory that cannot be listed (e.g. permission denied) fails open, not raises.

    A merely-missing directory does not raise from rglob (covered above); this
    forces the actual OSError-handling branch by making the walk itself fail.
    """
    # Given a real directory and Path.rglob forced to always fail
    sweep, target = _sweep_with_data(tmp_path)

    def _raise(self, _pattern) -> None:
        message = "permission denied"
        raise OSError(message)

    monkeypatch.setattr(Path, "rglob", _raise)

    # When listing what was touched / Then it fails open to an empty list
    assert sweep._touched_since(target.project_dir, 0.0) == []


# ---------------------------------------------------------------------------
# Sweep._existing_memory
# ---------------------------------------------------------------------------


def test_existing_memory_without_a_learnings_directory(tmp_path: Path) -> None:
    """Verify a target whose learnings directory does not exist yields no learnings section."""
    # Given a target whose learnings directory does not exist
    sweep, target = _sweep_with_data(tmp_path)
    no_learnings = Target(
        project_dir=target.project_dir,
        learnings_dir=target.project_dir / "does-not-exist",
        backlog_path=target.backlog_path,
        logs_dir=target.logs_dir,
    )

    # When reading existing memory
    result = sweep._existing_memory(no_learnings)

    # Then no learnings section appears
    assert "learnings/" not in result


def test_existing_memory_concatenates_backlog_and_learnings(tmp_path: Path) -> None:
    """Verify the backlog and every learning file are concatenated for the agent to read."""
    # Given a backlog and one learning file
    sweep, target = _sweep_with_data(tmp_path)
    target.backlog_path.write_text("todo: check the cache", encoding="utf-8")
    (target.learnings_dir / "a.md").write_text("cache invalidation notes", encoding="utf-8")

    # When reading existing memory
    result = sweep._existing_memory(target)

    # Then both are concatenated into the result
    assert "todo: check the cache" in result
    assert "# learnings/a.md" in result
    assert "cache invalidation notes" in result


# ---------------------------------------------------------------------------
# Sweep._validate_writes (mtime-derived: writes Bash made, never reported)
# ---------------------------------------------------------------------------


def test_a_secret_bash_wrote_into_the_project_dir_is_scrubbed_by_mtime(tmp_path, sweep_factory):
    """Verify a file written after the run started is scrubbed even when no tool reported it."""
    target = _target_at(tmp_path / "vault")
    target.learnings_dir.mkdir(parents=True, exist_ok=True)
    started_at = time.time() - 1
    leaked = target.learnings_dir / "leak.md"
    leaked.write_text("token = 'ghp_" + "a" * 36 + "'", encoding="utf-8")

    notes = sweep_factory()._validate_writes([], target, started_at=started_at)

    assert any(note.startswith("secret-redacted") for note in notes)
    assert "ghp_" not in leaked.read_text(encoding="utf-8")


def test_a_file_untouched_since_the_run_started_is_not_rescanned(tmp_path, sweep_factory):
    """Verify the mtime pass skips files older than the run."""
    target = _target_at(tmp_path / "vault")
    target.learnings_dir.mkdir(parents=True, exist_ok=True)
    old = target.learnings_dir / "old.md"
    old.write_text("token = 'ghp_" + "b" * 36 + "'", encoding="utf-8")

    notes = sweep_factory()._validate_writes([], target, started_at=time.time() + 60)

    assert notes == []


def test_a_binary_file_does_not_abort_the_mtime_scrub(tmp_path, sweep_factory):
    """Verify an undecodable file next to a leaking page is skipped, not fatal to the pass."""
    target = _target_at(tmp_path / "vault")
    target.learnings_dir.mkdir(parents=True, exist_ok=True)
    started_at = time.time() - 1
    (target.learnings_dir / "stub.sqlite3").write_bytes(b"\x00\x8a\xff")
    leaked = target.learnings_dir / "leak.md"
    leaked.write_text("token = 'ghp_" + "a" * 36 + "'", encoding="utf-8")

    notes = sweep_factory()._validate_writes([], target, started_at=started_at)

    assert any(note.startswith("secret-redacted") for note in notes)
    assert "ghp_" not in leaked.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Sweep._run_job (fake runner, never spawns real claude)
# ---------------------------------------------------------------------------


def _job_store(tmp_path: Path) -> Store:
    store = store_at(tmp_path)
    store.state_dir.mkdir(parents=True)
    store.lock_path.write_text("12345.0", encoding="utf-8")  # pre-held lock
    return store


class _FakeVault:
    """A reachable vault, without a CLI on disk to spawn.

    Duck-typed rather than a VaultCLI subclass: the sweep only ever calls these
    methods and reads `cli`, so a stand-in keeps the run_job cases from
    depending on a real subprocess.
    """

    def __init__(self, target: Target, *, root: Path | None = None) -> None:
        self.target = target
        self.root = root or Path("/fake")
        self.cli = self.root / "bin" / "sessionmemory"
        self.command = str(self.cli)

    def project_paths(self, *, cwd: Path, env: dict) -> dict[str, str]:
        return {
            "slug": "proj",
            "project_dir": str(self.target.project_dir),
            "learnings": str(self.target.learnings_dir),
            "backlog": str(self.target.backlog_path),
            "logs": str(self.target.logs_dir),
        }

    def commit(self, *, env: dict) -> str | None:
        return "abc1234"


class _EnvSpyVault(_FakeVault):
    """A `_FakeVault` that records the env it was resolved with, for `_prepare` tests."""

    def __init__(self, target: Target) -> None:
        super().__init__(target)
        self.seen_env: dict | None = None

    def project_paths(self, *, cwd: Path, env: dict) -> dict[str, str]:
        self.seen_env = dict(env)
        return super().project_paths(cwd=cwd, env=env)


def test_prepare_uses_the_threaded_env_not_a_fresh_os_environ_read(tmp_path: Path) -> None:
    """Verify _prepare resolves vault paths from the env run_sweep threaded in, not os.environ.

    run_sweep resolves the vault, which may come from `[vault] root` in the
    plugin config rather than the environment, using its own `env`, and pins
    that resolution into the env every agent command runs with. `_prepare`
    reading a fresh `os.environ` instead of the threaded value would silently
    drop that pin for any variable the caller's env carries that the process
    environment does not.
    """
    store = store_at(tmp_path)
    target = _target_at(tmp_path / "vault")
    vault = _EnvSpyVault(target)
    sweep = Sweep(
        store,
        SessionMemoryConfig(),
        _FakeRunner([]),
        vault,
        env={"SESSIONMEMORY_TEST_MARKER": "threaded"},
    )
    job = SweepJob(window=[], cwd=str(tmp_path), session_id="")

    sweep._prepare(job)

    assert vault.seen_env is not None
    assert vault.seen_env.get("SESSIONMEMORY_TEST_MARKER") == "threaded"


def test_prepare_returns_none_for_an_unregistered_project(tmp_path: Path) -> None:
    """Verify _prepare skips a project the vault cannot resolve, rather than raising."""

    # Given a vault whose project_paths always resolves to None (unregistered)
    class _UnregisteredVault(_FakeVault):
        def project_paths(self, *, cwd: Path, env: dict) -> dict[str, str] | None:
            return None

    store = store_at(tmp_path)
    target = _target_at(tmp_path / "vault")
    sweep = Sweep(store, SessionMemoryConfig(), _FakeRunner([]), _UnregisteredVault(target))
    job = SweepJob(window=[], cwd=str(tmp_path), session_id="")

    # When preparing / Then nothing is returned
    assert sweep._prepare(job) is None


def test_prepare_returns_none_when_the_resolved_paths_are_incomplete(tmp_path: Path) -> None:
    """Verify _prepare skips a payload missing a key Target.from_paths requires."""

    # Given a vault whose resolved paths are missing required keys
    class _IncompleteVault(_FakeVault):
        def project_paths(self, *, cwd: Path, env: dict) -> dict[str, str]:
            return {"slug": "demo", "project_dir": str(self.target.project_dir)}

    store = store_at(tmp_path)
    target = _target_at(tmp_path / "vault")
    sweep = Sweep(store, SessionMemoryConfig(), _FakeRunner([]), _IncompleteVault(target))
    job = SweepJob(window=[], cwd=str(tmp_path), session_id="")

    # When preparing / Then nothing is returned
    assert sweep._prepare(job) is None


class _RecordingRunner:
    """Captures the prompt it is handed so tests can assert on the sweep body."""

    def __init__(self) -> None:
        self.prompt: str | None = None

    def run(self, prompt: str, *, cwd: str) -> RunResult:
        self.prompt = prompt
        return RunResult(success=True, exit_code=0, changed_files=[], text="done", stderr="")


def test_run_job_prompt_carries_only_user_and_agent_text(tmp_path: Path) -> None:
    """Verify the sweep prompt body excludes thinking, tool calls, and tool results."""
    # Given a window with a user message and a rich assistant turn
    store = _job_store(tmp_path)
    runner = _RecordingRunner()
    sweep = Sweep(store, SessionMemoryConfig(), runner, _FakeVault(_target_at(tmp_path / "vault")))
    window = [
        _user("remember the threshold is five"),
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "PRIVATE-REASONING"},
                    {"type": "text", "text": "Noted, threshold is five."},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "secret-tool-call"}},
                ]
            },
        },
    ]
    job = SweepJob(window=window, cwd=str(tmp_path), session_id="")

    # When running the job
    sweep._run_job(job)

    # Then the prompt carries the human and agent text but none of the rest
    assert runner.prompt is not None
    assert "remember the threshold is five" in runner.prompt
    assert "Noted, threshold is five." in runner.prompt
    assert "PRIVATE-REASONING" not in runner.prompt
    assert "secret-tool-call" not in runner.prompt


def test_run_job_clean_write_logs_and_releases_lock(tmp_path: Path) -> None:
    """Verify run_job validates writes, commits, logs, and releases the lock on a clean run."""
    # Given a store with a clean target file and a pre-held lock
    store = _job_store(tmp_path)
    vault_target = _target_at(tmp_path / "vault")
    written = vault_target.project_dir / "memory.md"
    written.write_text("clean memory content", encoding="utf-8")
    sweep = Sweep(
        store, SessionMemoryConfig(), _FakeRunner([str(written)]), _FakeVault(vault_target)
    )
    job = SweepJob(window=[], cwd=str(tmp_path), session_id="")
    # When running the job
    notes = sweep._run_job(job)
    # Then the clean file yields no remediation notes beyond the commit record,
    # the log is written, and the lock is freed
    assert notes == ["committed: abc1234"]
    assert store.log_path.exists()
    assert not store.lock_path.exists()


def test_run_job_notes_a_skipped_commit(tmp_path: Path) -> None:
    """Verify a vault that commits nothing (e.g. a clean tree already) is noted as skipped."""

    # Given a vault whose commit call returns None
    class _NoCommitVault(_FakeVault):
        def commit(self, *, env: dict) -> str | None:
            return None

    store = _job_store(tmp_path)
    vault_target = _target_at(tmp_path / "vault")
    written = vault_target.project_dir / "memory.md"
    written.write_text("clean memory content", encoding="utf-8")
    sweep = Sweep(
        store, SessionMemoryConfig(), _FakeRunner([str(written)]), _NoCommitVault(vault_target)
    )
    job = SweepJob(window=[], cwd=str(tmp_path), session_id="")

    # When running the job
    notes = sweep._run_job(job)

    # Then the run notes the skip rather than a sha
    assert notes == ["commit-skipped"]


def test_run_job_escaped_write_quarantined_and_lock_released(tmp_path: Path) -> None:
    """Verify run_job quarantines a write outside the project dir and still releases the lock."""
    # Given a runner that reports a file written outside the project dir
    store = _job_store(tmp_path)
    vault_target = _target_at(tmp_path / "vault")
    outside = tmp_path / "outside.md"
    outside.write_text("nope", encoding="utf-8")
    sweep = Sweep(
        store, SessionMemoryConfig(), _FakeRunner([str(outside)]), _FakeVault(vault_target)
    )
    job = SweepJob(window=[], cwd=str(tmp_path), session_id="")
    # When running the job
    notes = sweep._run_job(job)
    # Then the escaped file is gone from its original path, its content
    # survives in quarantine, the commit is still recorded, and the lock is
    # released
    assert not outside.exists()
    assert notes == [f"escaped-quarantined: {outside}", "committed: abc1234"]
    assert not store.lock_path.exists()
    quarantined = list((store.state_dir / "quarantine").glob("*-outside.md"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "nope"


def test_run_job_releases_lock_on_runner_failure(tmp_path: Path) -> None:
    """Verify run_job releases the lock even when the runner raises."""
    # Given a runner that always raises

    class _Exploding:
        def run(self, prompt: str, *, cwd: str) -> RunResult:
            msg = "boom"
            raise RuntimeError(msg)

    store = _job_store(tmp_path)
    sweep = Sweep(store, SessionMemoryConfig(), _Exploding())
    job = SweepJob(window=[], cwd=str(tmp_path), session_id="")
    # When running the job
    notes = sweep._run_job(job)
    # Then it returns no notes and the lock is still released
    assert notes == []
    assert not store.lock_path.exists()


def test_run_job_returns_empty_and_releases_lock_when_the_agent_run_itself_raises(
    tmp_path: Path,
) -> None:
    """Verify an exception raised after prepare succeeds is still swallowed and the lock freed.

    Unlike the case above, a vault is present so `_prepare` succeeds and the
    exploding runner is actually reached, exercising the broad `except
    Exception` around the whole run rather than short-circuiting on no-vault.
    """

    # Given a vault present (so _prepare succeeds) and a runner that raises
    class _Exploding:
        def run(self, prompt: str, *, cwd: str) -> RunResult:
            msg = "boom"
            raise RuntimeError(msg)

    store = _job_store(tmp_path)
    vault_target = _target_at(tmp_path / "vault")
    sweep = Sweep(store, SessionMemoryConfig(), _Exploding(), _FakeVault(vault_target))
    job = SweepJob(window=[], cwd=str(tmp_path), session_id="sess-boom")

    # When running the job
    notes = sweep._run_job(job)

    # Then the failure is swallowed and the lock is still released
    assert notes == []
    assert not store.lock_path.exists()


def test_render_inlines_capture_criteria() -> None:
    """Verify every placeholder the sweep supplies is substituted, leaving no literal `{{name}}`."""
    # Given the sweep template and its criteria fragment
    # When the prompt is rendered
    rendered = sweep_mod._render_template(
        sweep_mod.PROMPT_PATH,
        transcript="[]",
        existing_memory="",
        git_context="",
        capture_criteria=sweep_mod.CRITERIA_PATH.read_text(encoding="utf-8"),
        vault_cli="/v/bin/sessionmemory",
        repo="/repo",
        log_command="/v/bin/sessionmemory log --session-id s --title t --cwd /repo",
        nothing_sentinel=sweep_mod.NOTHING_TO_RECORD,
        changes="",
        existing_log="",
    )
    # Then the criteria text is present and no placeholder remains
    assert "The two-gate test" in rendered
    assert "{{capture_criteria}}" not in rendered
    # Then a literal "{{name}}" never reaches the model as an instruction it cannot follow.
    assert "{{" not in rendered


def test_run_job_records_the_session_and_a_successful_outcome(tmp_path: Path) -> None:
    """Verify the sweep log answers "was that session swept, and did it work"."""
    # Given a store and a sweep whose fake runner reports success
    store = store_at(tmp_path)
    # _FakeRunner(changed_files: list[str]) is defined at the top of this file and
    # always returns RunResult(success=True, ...); pass [] for no writes.
    sweep = Sweep(
        store, SessionMemoryConfig(), _FakeRunner([]), _FakeVault(_target_at(tmp_path / "vault"))
    )
    job = SweepJob(window=[], cwd=str(tmp_path), session_id="sess-123")

    # When the job runs
    sweep._run_job(job)

    # Then one line names the session and reports that it succeeded
    line = store.log_path.read_text(encoding="utf-8")
    assert "session=sess-123" in line
    assert "ok=True" in line


def test_run_job_records_a_failed_sweep_as_failed(tmp_path: Path) -> None:
    """Verify a session that was attempted but failed is distinguishable from one never swept."""
    # Given a runner that completes but reports failure (non-zero exit)

    class _FailingRunner:
        def run(self, prompt: str, *, cwd: str) -> RunResult:
            return RunResult(success=False, exit_code=1, changed_files=[], text="", stderr="fail")

    store = store_at(tmp_path)
    sweep = Sweep(
        store, SessionMemoryConfig(), _FailingRunner(), _FakeVault(_target_at(tmp_path / "vault"))
    )
    job = SweepJob(window=[], cwd=str(tmp_path), session_id="sess-fail")

    # When the job runs
    sweep._run_job(job)

    # Then the attempt is on record, marked as having failed
    line = store.log_path.read_text(encoding="utf-8")
    assert "session=sess-fail" in line
    assert "ok=False" in line


def test_run_job_without_a_vault_still_records_the_attempt(tmp_path: Path) -> None:
    """Verify an unreachable vault leaves a trace, rather than a session vanishing silently."""
    # Given a sweep with no vault to write into
    store = store_at(tmp_path)
    sweep = Sweep(store, SessionMemoryConfig(), _FakeRunner([]))
    job = SweepJob(window=[], cwd=str(tmp_path), session_id="sess-novault")

    # When the job runs
    sweep._run_job(job)

    # Then the reason is on record against the session
    line = store.log_path.read_text(encoding="utf-8")
    assert "session=sess-novault" in line
    assert "no-vault" in line


# ---------------------------------------------------------------------------
# Sweep gate 2: the model decides whether the session is worth recording at all
# ---------------------------------------------------------------------------


class _TextRunner:
    """A runner that returns a caller-supplied final text, for the log-decision cases."""

    def __init__(self, text: str, *, changed_files: list[str] | None = None) -> None:
        self.text = text
        self.changed_files = changed_files or []
        self.prompt: str | None = None

    def run(self, prompt: str, *, cwd: str) -> RunResult:
        self.prompt = prompt
        return RunResult(
            success=True,
            exit_code=0,
            changed_files=list(self.changed_files),
            text=self.text,
            stderr="",
        )


def test_run_job_prompt_carries_a_ready_to_run_log_command(tmp_path: Path) -> None:
    """Verify the model is handed the composed command, so it invents no CLI arguments."""
    # Given a session ready to be swept
    store = _job_store(tmp_path)
    runner = _RecordingRunner()
    sweep = Sweep(store, SessionMemoryConfig(), runner, _FakeVault(_target_at(tmp_path / "vault")))
    # When the job runs
    sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id="sess-xyz"))
    # Then the prompt carries the CLI path and every argument already resolved
    assert runner.prompt is not None
    assert "bin/sessionmemory log" in runner.prompt
    assert "--session-id sess-xyz" in runner.prompt


def test_run_job_records_a_session_the_model_judged_not_worth_recording(tmp_path: Path) -> None:
    """Verify a deliberately unrecorded session is distinguishable from a failed sweep."""
    # Given a model that ends its run with the nothing-to-record sentinel
    store = store_at(tmp_path)
    sweep = Sweep(
        store,
        SessionMemoryConfig(),
        _TextRunner(f"Nothing durable here.\n{sweep_mod.NOTHING_TO_RECORD}"),
        _FakeVault(_target_at(tmp_path / "vault")),
    )
    # When the job runs
    sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id="sess-quiet"))
    # Then the decision is on record as a success, not a failure
    line = store.log_path.read_text(encoding="utf-8")
    assert "session=sess-quiet" in line
    assert "nothing-to-record" in line
    assert "ok=True" in line


def test_run_job_does_not_record_a_productive_sweep_as_empty(tmp_path: Path) -> None:
    """Verify an ordinary sweep is never mislabelled as having recorded nothing."""
    # Given a model that wrote a log and said so
    store = store_at(tmp_path)
    sweep = Sweep(
        store,
        SessionMemoryConfig(),
        _TextRunner("Wrote the session log and one learning."),
        _FakeVault(_target_at(tmp_path / "vault")),
    )
    # When the job runs
    sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id="sess-loud"))
    # Then no decline is recorded
    assert "nothing-to-record" not in store.log_path.read_text(encoding="utf-8")


def test_run_job_ignores_the_sentinel_when_the_model_actually_wrote(tmp_path: Path) -> None:
    """Verify a sweep that wrote notes is never logged as having recorded nothing.

    The sentinel is part of the prompt the agent summarizes, so it can surface in
    the final text of a run that did its job.
    """
    # Given a model that wrote a note and still echoed the sentinel
    store = store_at(tmp_path)
    target = _target_at(tmp_path / "vault")
    written = target.project_dir / "note.md"
    written.write_text("real content", encoding="utf-8")
    sweep = Sweep(
        store,
        SessionMemoryConfig(),
        _TextRunner(
            f"Considered {sweep_mod.NOTHING_TO_RECORD} and wrote anyway.",
            changed_files=[str(written)],
        ),
        _FakeVault(target),
    )
    # When the job runs
    sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id="sess-mixed"))
    # Then the write outranks the sentinel
    assert "nothing-to-record" not in store.log_path.read_text(encoding="utf-8")


def test_run_sweep_pins_the_resolved_vault_root_for_the_agent(tmp_path: Path, monkeypatch) -> None:
    """Verify a vault found via config, not the environment, still reaches the agent's CLI."""
    # Given a vault reachable only through the plugin config
    root = tmp_path / "vault-root"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "sessionmemory").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    (root / "_system").mkdir(parents=True)
    (root / "_system" / "vault.toml").write_text("", encoding="utf-8")
    (tmp_path / "home" / ".claude").mkdir(parents=True)
    (tmp_path / "home" / ".claude" / "sessionmemory.toml").write_text(
        f'[vault]\nroot = "{root}"\n', encoding="utf-8"
    )
    captured: dict[str, object] = {}

    class _Spy:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def run(self, prompt: str, *, cwd: str) -> RunResult:  # pragma: no cover - never gated in
            raise AssertionError

    def _fake_home() -> Path:
        return tmp_path / "home"

    monkeypatch.setattr(sweep_mod, "ClaudeRunner", _Spy)
    monkeypatch.setattr(Path, "home", staticmethod(_fake_home))
    # When the sweep is wired up
    sweep_mod.run_sweep({"cwd": str(tmp_path)}, env={"XDG_STATE_HOME": str(tmp_path / "state")})
    # Then the runner carries the root the CLI can only read from its environment
    assert captured["extra_env"] == {sweep_mod.ROOT_ENV: str(root)}


# ---------------------------------------------------------------------------
# Sweep log body inputs: the composed command, the commit list, the carry-forward log
# ---------------------------------------------------------------------------


def test_the_log_command_carries_no_body(sweep_factory, tmp_path):
    """Verify the composed log command never includes --body, which only the model can write."""
    sweep = sweep_factory()
    job = _job(cwd=str(tmp_path), session_id="abc")

    command = sweep._log_command(job, slug="demo")

    assert "--body" not in command
    assert "--session-id abc" in command
    assert "log" in command


def test_changes_are_empty_without_a_base_commit(sweep_factory, tmp_path):
    """Verify _changes reads as empty when no base commit was recorded for the session."""
    sweep = sweep_factory()

    assert sweep._changes(_job(cwd=str(tmp_path), session_id="abc")) == ""


def test_the_existing_log_is_found_by_session_id(sweep_factory, tmp_path):
    """Verify _existing_log matches a log note by its literal session_id frontmatter line."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "2026-01-01-demo.md").write_text(
        "---\nid: x\nsession_id: abc\n---\n\n## Summary\n\nearlier work\n", encoding="utf-8"
    )
    target = _target_at(tmp_path, logs=logs)

    assert "earlier work" in sweep_factory()._existing_log(target, "abc")


def test_existing_log_strips_the_frontmatter_block(sweep_factory, tmp_path):
    """Verify _existing_log returns only the body, with no frontmatter the model could carry into --body."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "2026-01-01-demo.md").write_text(
        "---\nid: x\nsession_id: abc\n---\n\n## Summary\n\nearlier work\n", encoding="utf-8"
    )
    target = _target_at(tmp_path, logs=logs)

    body = sweep_factory()._existing_log(target, "abc")

    assert "---" not in body
    assert "session_id" not in body
    assert "## Summary" in body


def test_no_existing_log_reads_as_empty(sweep_factory, tmp_path):
    """Verify _existing_log reads as empty when no log carries the session's id."""
    (tmp_path / "logs").mkdir()

    assert sweep_factory()._existing_log(_target_at(tmp_path), "abc") == ""


# ---------------------------------------------------------------------------
# Sweep._log_run / _redirect_stdio (best-effort IO, must never raise)
# ---------------------------------------------------------------------------


def test_log_run_swallows_an_os_error(tmp_path: Path) -> None:
    """Verify a state dir that cannot be created leaves the log call a no-op, not a crash."""
    # Given a plain file occupying the state dir's own path, so mkdir fails
    store = store_at(tmp_path)
    blocker = store.state_dir
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("blocker", encoding="utf-8")
    sweep = Sweep(store, SessionMemoryConfig(), _FakeRunner([]))

    # When logging the run
    sweep._log_run(session="s", ok=True, changed=[], notes=[])  # must not raise

    # Then nothing was written: the blocker file is untouched and holds no log line
    assert blocker.read_text(encoding="utf-8") == "blocker"


def test_redirect_stdio_points_stdio_at_sweep_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify the daemon's stdio is redirected to sweep.out rather than the hook's own stdout.

    os.dup2 is replaced with a recorder: calling it for real here would
    redirect this very test process's stdio, corrupting pytest's own output.
    """
    # Given os.dup2 replaced with a recorder
    store = store_at(tmp_path)
    sweep = Sweep(store, SessionMemoryConfig(), _FakeRunner([]))
    dup_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "dup2", lambda src, dst: dup_calls.append((src, dst)))

    # When redirecting stdio
    sweep._redirect_stdio()

    # Then fds 0, 1, and 2 are redirected and sweep.out is created
    assert [dst for _src, dst in dup_calls] == [0, 1, 2]
    assert (store.state_dir / "sweep.out").exists()


def test_redirect_stdio_swallows_an_os_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify a state dir that cannot be created leaves the redirect a no-op, not a crash."""
    # Given a plain file occupying the state dir's own path, so mkdir fails,
    # and os.dup2 spied on so a real redirect would be caught
    store = store_at(tmp_path)
    blocker = store.state_dir
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("blocker", encoding="utf-8")
    sweep = Sweep(store, SessionMemoryConfig(), _FakeRunner([]))
    dup_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "dup2", lambda src, dst: dup_calls.append((src, dst)))

    # When redirecting stdio
    sweep._redirect_stdio()  # must not raise

    # Then nothing was created and stdio was never touched
    assert not (store.state_dir / "sweep.out").exists()
    assert dup_calls == []


# ---------------------------------------------------------------------------
# Sweep._spawn_detached (double-fork daemonization, os.fork/_exit fully mocked)
# ---------------------------------------------------------------------------


class _ExitCalled(Exception):  # noqa: N818
    """Stands in for os._exit, which never returns, so a mock must not either."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


def _mock_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    def _exit(code: int) -> None:
        raise _ExitCalled(code)

    monkeypatch.setattr(os, "_exit", _exit)


def test_spawn_detached_releases_the_lock_when_the_first_fork_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify a fork failure is treated like any other unavailable resource: skip, don't block."""
    # Given a held lock and os.fork forced to always fail
    store = store_at(tmp_path)
    Lock(store.lock_path).acquire(now=1.0)
    sweep = Sweep(store, SessionMemoryConfig(), _FakeRunner([]))

    def _raise() -> None:
        message = "cannot fork"
        raise OSError(message)

    monkeypatch.setattr(os, "fork", _raise)

    # When spawning the detached worker
    sweep._spawn_detached(_job(cwd=str(tmp_path)))

    # Then the lock is released rather than left held
    assert not store.lock_path.exists()


def test_spawn_detached_parent_reaps_the_intermediate_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify the calling process (the hook) reaps its child and returns without blocking."""
    # Given os.fork reporting a positive pid, as the parent branch sees it
    waited: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "fork", lambda: 4242)
    monkeypatch.setattr(os, "waitpid", lambda pid, opts: waited.append((pid, opts)) or (pid, 0))
    store = store_at(tmp_path)
    sweep = Sweep(store, SessionMemoryConfig(), _FakeRunner([]))

    # When spawning the detached worker
    sweep._spawn_detached(_job(cwd=str(tmp_path)))

    # Then the parent reaps the intermediate child
    assert waited == [(4242, 0)]


def test_spawn_detached_intermediate_child_releases_the_lock_when_the_second_fork_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify a daemon that never reaches _run_job still releases the lock it inherited."""
    # Given a held lock, the first fork landing in the intermediate child, and
    # the second fork forced to fail
    store = store_at(tmp_path)
    Lock(store.lock_path).acquire(now=1.0)
    sweep = Sweep(store, SessionMemoryConfig(), _FakeRunner([]))
    calls = {"n": 0}

    def _fork() -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            return 0  # the intermediate child
        message = "cannot fork"
        raise OSError(message)

    monkeypatch.setattr(os, "fork", _fork)
    monkeypatch.setattr(os, "setsid", lambda: None)
    _mock_exit(monkeypatch)

    # When spawning the detached worker
    with pytest.raises(_ExitCalled) as excinfo:
        sweep._spawn_detached(_job(cwd=str(tmp_path)))

    # Then the intermediate child exits cleanly and the lock is released
    assert excinfo.value.code == 0
    assert not store.lock_path.exists()


def test_spawn_detached_intermediate_child_exits_after_a_successful_second_fork(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify the intermediate child exits once the daemon (grandchild) is forked off."""
    # Given the first fork landing in the intermediate child and the second
    # fork succeeding with a real grandchild pid
    calls = {"n": 0}

    def _fork() -> int:
        calls["n"] += 1
        return 0 if calls["n"] == 1 else 999

    monkeypatch.setattr(os, "fork", _fork)
    monkeypatch.setattr(os, "setsid", lambda: None)
    _mock_exit(monkeypatch)
    store = store_at(tmp_path)
    sweep = Sweep(store, SessionMemoryConfig(), _FakeRunner([]))

    # When spawning the detached worker
    with pytest.raises(_ExitCalled) as excinfo:
        sweep._spawn_detached(_job(cwd=str(tmp_path)))

    # Then the intermediate child exits cleanly
    assert excinfo.value.code == 0


def test_spawn_detached_grandchild_runs_the_job_then_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify the daemon (both forks returning 0) redirects stdio, runs the job, then exits."""
    # Given both forks landing in the child branch, so this call is the daemon itself
    monkeypatch.setattr(os, "fork", lambda: 0)
    monkeypatch.setattr(os, "setsid", lambda: None)
    _mock_exit(monkeypatch)
    store = store_at(tmp_path)
    sweep = Sweep(store, SessionMemoryConfig(), _FakeRunner([]))
    ran: list[str] = []
    monkeypatch.setattr(sweep, "_redirect_stdio", lambda: ran.append("redirect"))
    monkeypatch.setattr(sweep, "_run_job", lambda job: ran.append("run"))

    # When spawning the detached worker
    with pytest.raises(_ExitCalled) as excinfo:
        sweep._spawn_detached(_job(cwd=str(tmp_path)))

    # Then stdio is redirected, the job runs, and the daemon exits cleanly
    assert ran == ["redirect", "run"]
    assert excinfo.value.code == 0


class _CwdCheckingRunner:
    """Records whether the working directory existed when the sweep handed it over."""

    def __init__(self) -> None:
        self.cwd_existed: bool | None = None

    def run(self, prompt: str, *, cwd: str) -> RunResult:
        self.cwd_existed = Path(cwd).is_dir()
        return RunResult(success=True, exit_code=0, changed_files=[], text="done", stderr="")


def test_run_job_creates_the_project_folder_before_running(tmp_path: Path) -> None:
    """Verify a freshly registered project, with no folder yet, is swept in its first session."""
    # Given a target whose project folder does not exist on disk
    store = _job_store(tmp_path)
    project = tmp_path / "vault" / "projects" / "proj"
    target = Target(
        project_dir=project,
        learnings_dir=project / "learnings",
        backlog_path=project / "backlog.md",
        logs_dir=project / "logs",
    )
    runner = _CwdCheckingRunner()
    sweep = Sweep(store, SessionMemoryConfig(), runner, _FakeVault(target))
    job = SweepJob(window=[_user("remember this")], cwd=str(tmp_path), session_id="s1")

    # When running the job
    sweep._run_job(job)

    # Then the folder existed by the time the model was run inside it
    assert runner.cwd_existed is True
    assert project.is_dir()
