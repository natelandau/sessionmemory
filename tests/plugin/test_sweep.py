"""Verify the sweep: lock, queue, gate threshold, write validation, and run_job."""

from __future__ import annotations

import errno
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sessionhooks import log as log_mod  # ty: ignore[unresolved-import]
from sessionhooks import sweep as sweep_mod  # ty: ignore[unresolved-import]
from sessionhooks.commit import CommitOutcome  # ty: ignore[unresolved-import]
from sessionhooks.config import SessionMemoryConfig  # ty: ignore[unresolved-import]
from sessionhooks.runner import LOG_TITLE_ENV, RunResult  # ty: ignore[unresolved-import]
from sessionhooks.snapshot import Changes  # ty: ignore[unresolved-import]
from sessionhooks.store import LOCK_NAME, Store, state_root  # ty: ignore[unresolved-import]
from sessionhooks.sweep import (  # ty: ignore[unresolved-import]
    Lock,
    Sweep,
    SweepJob,
    Target,
    any_in_progress,
    in_progress,
)
from sessionhooks.vaultcli import WORKER_CLI_TIMEOUT, Lookup  # ty: ignore[unresolved-import]

from sessionmemory.lib.frontmatter import serialize
from tests._env import clean_environ
from tests.plugin._store_factory import store_at
from tests.plugin.conftest import hooks_log_text

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pytest_mock import MockerFixture


def _capture_log(tmp_path: Path) -> Path:
    """Point the hooks log at tmp_path and return the XDG state home it writes under."""
    log_mod.configure(
        "sessionend",
        config=SessionMemoryConfig(),
        env={"XDG_STATE_HOME": str(tmp_path / "xdg")},
        project="demo",
        session_id="sess-123",
    )
    return tmp_path / "xdg"


class _FakeRunner:
    """Duck-typed Runner that reports a canned set of changed files; never spawns claude."""

    def __init__(self, changed_files: list[str]) -> None:
        self.changed_files = changed_files

    def run(
        self, prompt: str, *, cwd: str, extra_env: Mapping[str, str] | None = None
    ) -> RunResult:
        return RunResult(
            success=True,
            exit_code=0,
            changed_files=list(self.changed_files),
            text="done",
            stderr="",
        )


class _WritingRunner:
    """Runner that performs filesystem actions during the run without reporting them."""

    def __init__(self, action: Callable[[], object], *, text: str = "done") -> None:
        self.action = action
        self.text = text

    def run(
        self, prompt: str, *, cwd: str, extra_env: Mapping[str, str] | None = None
    ) -> RunResult:
        self.action()
        return RunResult(success=True, exit_code=0, changed_files=[], text=self.text, stderr="")


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


def test_lock_acquire_succeeds_when_free(tmp_path: Path) -> None:
    """Verify acquire returns True and creates the lock file when nobody holds it."""
    # Given a lock path under a not-yet-created dir
    lock = Lock(tmp_path / "state" / "sweep.lock")
    # When acquiring / Then it succeeds and the file exists
    assert lock.acquire() is True
    assert lock.path.exists()
    lock.release()


def test_lock_second_holder_is_refused(tmp_path: Path) -> None:
    """Verify a second Lock on the same path is refused while the first holds it."""
    # Given a held lock
    first = Lock(tmp_path / "state" / "sweep.lock")
    assert first.acquire() is True
    # When a second holder tries / Then it is refused
    try:
        assert Lock(tmp_path / "state" / "sweep.lock").acquire() is False
    finally:
        first.release()


def test_lock_is_free_again_after_release(tmp_path: Path) -> None:
    """Verify releasing frees the lock for a new holder and a second release never raises."""
    # Given a lock acquired then released twice
    lock = Lock(tmp_path / "state" / "sweep.lock")
    assert lock.acquire() is True
    lock.release()
    lock.release()
    # When a new holder tries / Then it succeeds
    again = Lock(tmp_path / "state" / "sweep.lock")
    assert again.acquire() is True
    again.release()


def test_lock_release_by_a_non_holder_leaves_the_holder_alone(tmp_path: Path) -> None:
    """Verify releasing a Lock that never acquired does not free another holder's lock."""
    # Given a held lock
    path = tmp_path / "state" / "sweep.lock"
    holder = Lock(path)
    assert holder.acquire() is True
    try:
        # When a never-acquired Lock on the same path releases
        Lock(path).release()
        # Then the holder still holds it
        assert Lock(path).acquire() is False
    finally:
        holder.release()


def test_lock_left_on_disk_by_a_dead_holder_is_free(tmp_path: Path) -> None:
    """Verify a lock file nobody holds a lock on is free, whatever it contains."""
    # Given a lock file left behind with old content
    path = tmp_path / "state" / "sweep.lock"
    path.parent.mkdir(parents=True)
    path.write_text("12345.0", encoding="utf-8")
    lock = Lock(path)
    # When acquiring / Then it succeeds
    assert lock.acquire() is True
    lock.release()


def test_lock_is_released_when_its_holder_process_exits(tmp_path: Path) -> None:
    """Verify the kernel frees the lock when a holder exits without releasing it."""
    # Given a process that takes the lock and exits without unlocking
    path = tmp_path / "state" / "sweep.lock"
    path.parent.mkdir(parents=True)
    code = (
        "import fcntl, os, sys\n"
        "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
    )
    subprocess.run([sys.executable, "-c", code, str(path)], check=True)
    lock = Lock(path)
    # When acquiring / Then it succeeds
    assert lock.acquire() is True
    lock.release()


def test_lock_acquire_fails_when_the_parent_directory_cannot_be_created(tmp_path: Path) -> None:
    """Verify acquire fails open when the lock's parent directory cannot be made."""
    # Given a plain file occupying the path the lock's parent directory needs
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    lock = Lock(blocker / "sweep.lock")
    # When acquiring / Then it fails rather than raising
    assert lock.acquire() is False


def test_lock_reports_no_error_when_another_holder_has_it(tmp_path: Path) -> None:
    """Verify contention is told apart from a failure: it leaves no error behind."""
    first = Lock(tmp_path / "state" / "sweep.lock")
    assert first.acquire() is True
    second = Lock(first.path)
    try:
        assert second.acquire() is False
        assert second.error == ""
    finally:
        first.release()


def test_lock_names_the_error_when_its_directory_cannot_be_made(tmp_path: Path) -> None:
    """Verify a lock that cannot be created records why, so a caller stops waiting on it."""
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    lock = Lock(blocker / "sweep.lock")
    assert lock.acquire() is False
    assert lock.error


def test_lock_names_a_flock_failure_that_is_not_contention(tmp_path: Path, mocker) -> None:
    """Verify only EWOULDBLOCK means held; any other flock error is recorded as a failure."""
    mocker.patch.object(
        sweep_mod.fcntl,
        "flock",
        autospec=True,
        side_effect=OSError(errno.ENOLCK, "No locks available"),
    )
    lock = Lock(tmp_path / "state" / "sweep.lock")
    assert lock.acquire() is False
    assert "No locks available" in lock.error


def _deny_open(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """Make `os.open` refuse `path` the way a lock file with no permissions would."""
    real = os.open

    def _open(target: str | Path, flags: int, mode: int = 0o777) -> int:
        if Path(target) == path:
            raise PermissionError(errno.EACCES, "Permission denied", str(target))
        return real(target, flags, mode)

    monkeypatch.setattr(sweep_mod.os, "open", _open)


def test_in_progress_false_when_the_lock_cannot_be_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify an unopenable lock file reads as no sweep, so a commit is not skipped forever."""
    # Given a lock file nobody can open, in a project and under the state root
    store = store_at(tmp_path)
    store.state_dir.mkdir(parents=True)
    store.lock_path.touch()
    env = {"XDG_STATE_HOME": str(tmp_path / "xdg")}
    other = _project_lock(env, "other")
    other.parent.mkdir(parents=True)
    other.touch()
    _deny_open(monkeypatch, store.lock_path)
    _deny_open(monkeypatch, other)
    # When checking / Then no sweep is reported
    assert in_progress(store) is False
    assert any_in_progress(env) is False


def test_run_job_gives_up_at_once_when_the_lock_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify a lock that fails for a reason other than contention ends the wait immediately."""
    # Given a lock file that cannot be opened
    hooks_log = _capture_log(tmp_path)
    store = _job_store(tmp_path)
    store.lock_path.touch()
    _deny_open(monkeypatch, store.lock_path)
    waits: list[float] = []
    runner = _RecordingRunner()
    sweep = Sweep(
        store,
        SessionMemoryConfig(),
        runner,
        _FakeVault(_target_at(tmp_path / "vault")),
        sleep=waits.append,
    )
    # When the worker runs
    notes = sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id=""))
    # Then it never waited or ran, and logged why
    assert notes == []
    assert waits == []
    assert runner.prompt is None
    assert re.search(
        r"WARNING.*sweep skipped: lock unavailable: .*Permission denied", hooks_log_text(hooks_log)
    )


def test_run_job_stops_waiting_when_the_lock_becomes_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify a queued worker stops polling once the lock fails for a reason other than contention."""
    # Given a lock another worker holds, whose file stops opening while this one waits
    hooks_log = _capture_log(tmp_path)
    store = _job_store(tmp_path)
    held = Lock(store.lock_path)
    assert held.acquire() is True
    waits: list[float] = []

    def _sleep(seconds: float) -> None:
        waits.append(seconds)
        _deny_open(monkeypatch, store.lock_path)

    runner = _RecordingRunner()
    sweep = Sweep(
        store,
        SessionMemoryConfig(),
        runner,
        _FakeVault(_target_at(tmp_path / "vault")),
        sleep=_sleep,
    )
    try:
        # When the worker runs
        notes = sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id=""))
    finally:
        held.release()
    # Then it gave up after one poll without running
    assert notes == []
    assert waits == [sweep_mod.QUEUE_POLL]
    assert runner.prompt is None
    assert "sweep skipped: lock unavailable: " in hooks_log_text(hooks_log)


# ---------------------------------------------------------------------------
# in_progress
# ---------------------------------------------------------------------------


def test_in_progress_true_while_a_worker_holds_the_lock(tmp_path: Path) -> None:
    """Verify a held lock reads as a worker in progress."""
    # Given a store whose lock a worker holds
    store = store_at(tmp_path)
    worker = Lock(store.lock_path)
    assert worker.acquire() is True
    try:
        # When checking / Then a worker is reported
        assert in_progress(store) is True
    finally:
        worker.release()


def test_in_progress_false_after_the_worker_releases(tmp_path: Path) -> None:
    """Verify a released lock reads as no worker, and the probe leaves it free."""
    # Given a store whose worker released its lock
    store = store_at(tmp_path)
    worker = Lock(store.lock_path)
    assert worker.acquire() is True
    worker.release()
    # When checking / Then no worker is reported
    assert in_progress(store) is False
    # And the probe released what it took
    after = Lock(store.lock_path)
    assert after.acquire() is True
    after.release()


def test_in_progress_false_when_no_lock_exists(tmp_path: Path) -> None:
    """Verify a store with no lock file reads as no worker holding it."""
    # Given a store with no lock file at all
    store = store_at(tmp_path)
    # When checking / Then no worker is reported
    assert in_progress(store) is False
    assert not store.lock_path.exists()


# ---------------------------------------------------------------------------
# any_in_progress
# ---------------------------------------------------------------------------


def _project_lock(env: dict[str, str], name: str) -> Path:
    """The sweep lock path of a project's store under the state root `env` names."""
    return state_root(env) / name / LOCK_NAME


def test_any_in_progress_sees_another_projects_lock(tmp_path: Path) -> None:
    """Verify a lock held by any project under the state root reads as a sweep running."""
    # Given two project stores under one state root, one of them locked
    env = {"XDG_STATE_HOME": str(tmp_path / "xdg")}
    held = Lock(_project_lock(env, "aaaa"))
    assert held.acquire() is True
    idle = _project_lock(env, "bbbb")
    idle.parent.mkdir(parents=True)
    idle.touch()
    try:
        # When checking / Then a sweep is reported
        assert any_in_progress(env) is True
    finally:
        held.release()
    # And none once it is released
    assert any_in_progress(env) is False


def test_any_in_progress_false_with_no_state_root(tmp_path: Path) -> None:
    """Verify a machine with no state root yet reads as no sweep running."""
    assert any_in_progress({"XDG_STATE_HOME": str(tmp_path / "missing")}) is False


def test_any_in_progress_skips_the_excepted_lock(tmp_path: Path) -> None:
    """Verify a worker asking about other sweeps does not count its own lock."""
    # Given a worker holding its own project's lock
    env = {"XDG_STATE_HOME": str(tmp_path / "xdg")}
    own = Lock(_project_lock(env, "aaaa"))
    assert own.acquire() is True
    try:
        # When checking with its own lock excepted / Then no other sweep is reported
        assert any_in_progress(env, except_path=own.path) is False
        assert any_in_progress(env) is True
    finally:
        own.release()


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


def test_gate_below_threshold_returns_none(tmp_path: Path) -> None:
    """Verify gate returns None below min_exchanges."""
    # Given a sparse transcript (2 meaningful) and a threshold of 5
    store = store_at(tmp_path)
    t_file = tmp_path / "sparse.jsonl"
    _write_transcript(t_file, [_user("hi"), _assistant("yo")])
    sweep = Sweep(store, SessionMemoryConfig(min_exchanges=5), _FakeRunner([]))
    event = {"cwd": str(tmp_path), "transcript_path": str(t_file)}
    # When gating
    result = sweep._gate(event)
    # Then None is returned
    assert result is None


def test_gate_logs_a_below_threshold_skip_with_every_count(tmp_path: Path) -> None:
    """Verify a skipped session explains itself: each measure against its floor."""
    hooks_log = _capture_log(tmp_path)
    store = store_at(tmp_path)
    t_file = tmp_path / "sparse.jsonl"
    _write_transcript(t_file, [_user("hi"), _assistant("yo")])
    sweep = Sweep(store, SessionMemoryConfig(min_exchanges=5), _FakeRunner([]))
    sweep._gate({"cwd": str(tmp_path), "transcript_path": str(t_file)})
    text = hooks_log_text(hooks_log)
    assert (
        "sweep skipped: below threshold (exchanges 2/5, user messages 1/3, user chars 2/200)"
        in text
    )


def test_gate_logs_a_missing_transcript(tmp_path: Path) -> None:
    """Verify a session with no transcript path and no saved pointer is named as such."""
    hooks_log = _capture_log(tmp_path)
    sweep = Sweep(store_at(tmp_path), SessionMemoryConfig(), _FakeRunner([]))
    sweep._gate({"cwd": str(tmp_path), "transcript_path": ""})
    assert "sweep skipped: no transcript" in hooks_log_text(hooks_log)


def test_gate_prefers_the_payload_session_id(tmp_path: Path) -> None:
    """Verify the job carries the session id the hook was handed, not only the transcript stem."""
    store = store_at(tmp_path)
    t_file = tmp_path / "rich.jsonl"
    _write_transcript(t_file, _meaningful(6))
    sweep = Sweep(store, SessionMemoryConfig(min_exchanges=5), _FakeRunner([]))
    job = sweep._gate(
        {"cwd": str(tmp_path), "transcript_path": str(t_file), "session_id": "from-payload"},
    )
    assert job is not None
    assert job.session_id == "from-payload"


def test_gate_above_threshold_returns_job(tmp_path: Path) -> None:
    """Verify gate returns a SweepJob carrying the window."""
    # Given a rich transcript (6 meaningful, threshold 5)
    store = store_at(tmp_path)
    entries = _meaningful(6)
    t_file = tmp_path / "rich.jsonl"
    _write_transcript(t_file, entries)
    sweep = Sweep(store, SessionMemoryConfig(min_exchanges=5), _FakeRunner([]))
    event = {"cwd": str(tmp_path / "proj"), "transcript_path": str(t_file)}
    # When gating
    result = sweep._gate(event)
    # Then a job covering all entries is returned
    assert result is not None
    assert result.cwd == str(tmp_path / "proj")
    assert len(result.window) == len(entries)


def test_gate_carries_the_transcript_path_and_online_link(tmp_path: Path) -> None:
    """Verify the job knows where its transcript lives and how to reach the session online."""
    # Given a bridged transcript above the threshold
    store = store_at(tmp_path)
    entries = [{"type": "bridge-session", "bridgeSessionId": "cse_01XYZ"}, *_meaningful(6)]
    t_file = tmp_path / "bridged.jsonl"
    _write_transcript(t_file, entries)
    sweep = Sweep(store, SessionMemoryConfig(min_exchanges=5), _FakeRunner([]))
    # When gating
    result = sweep._gate({"cwd": str(tmp_path), "transcript_path": str(t_file)})
    # Then both are carried on the job
    assert result is not None
    assert result.transcript_path == str(t_file)
    assert result.session_url == "https://claude.ai/code/session_01XYZ"


def test_gate_carries_the_session_start(tmp_path: Path) -> None:
    """Verify the job knows when the session began, so its log can be named for it."""
    # Given a transcript whose first timestamped entry precedes the window
    store = store_at(tmp_path)
    entries = [{"type": "user", "timestamp": "2026-09-03T17:53:10.187Z"}, *_meaningful(6)]
    t_file = tmp_path / "timed.jsonl"
    _write_transcript(t_file, entries)
    sweep = Sweep(store, SessionMemoryConfig(min_exchanges=5), _FakeRunner([]))
    # When gating
    result = sweep._gate({"cwd": str(tmp_path), "transcript_path": str(t_file)})
    # Then the start is carried on the job
    assert result is not None
    assert result.started == "2026-09-03T17:53:10.187Z"


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
    result = sweep._gate({"cwd": str(tmp_path), "transcript_path": ""})
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
    result = sweep._gate({"cwd": str(tmp_path), "transcript_path": str(t_file)})
    # Then the human's single message fails the substance floor
    assert result is None


def test_gate_rejects_a_session_of_terse_commands(tmp_path: Path) -> None:
    """Verify several one-word instructions do not add up to a loggable session."""
    # Given many short exchanges that clear both the count floors but say nothing
    store = store_at(tmp_path)
    t_file = tmp_path / "terse.jsonl"
    entries = [_user("ok"), _assistant("done")] * 6
    _write_transcript(t_file, entries)
    sweep = Sweep(store, SessionMemoryConfig(min_exchanges=5), _FakeRunner([]))
    # When gating
    result = sweep._gate({"cwd": str(tmp_path), "transcript_path": str(t_file)})
    # Then the character floor rejects it
    assert result is None


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
    result = sweep._gate({"cwd": str(tmp_path), "transcript_path": str(t_file)})
    # Then it is swept despite no commits
    assert result is not None


def test_gate_yields_a_job_while_another_sweep_holds_the_lock(tmp_path: Path) -> None:
    """Verify a running sweep never stops a later session from gating through."""
    # Given a rich transcript and another sweep holding the project's lock
    store = store_at(tmp_path)
    t_file = tmp_path / "rich.jsonl"
    _write_transcript(t_file, _meaningful(6))
    held = Lock(store.lock_path)
    assert held.acquire() is True
    sweep = Sweep(store, SessionMemoryConfig(min_exchanges=5), _FakeRunner([]))
    try:
        # When gating
        result = sweep._gate({"cwd": str(tmp_path), "transcript_path": str(t_file)})
        # Then a job is still returned, and the other holder keeps its lock
        assert result is not None
        assert in_progress(store) is True
    finally:
        held.release()


def test_gate_returns_none_on_an_unexpected_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify the gate never raises when something inside it breaks."""
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
    result = sweep._gate({"cwd": str(tmp_path), "transcript_path": str(t_file)})

    # Then the error is swallowed
    assert result is None


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
    itself, so `_gate`'s own error path actually runs.
    """
    # Given a rich transcript that would otherwise gate through, an internal
    # failure inside _gate, and _spawn_detached spied on so nothing forks
    hooks_log = _capture_log(tmp_path)
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

    # Then no worker is spawned
    spawn.assert_not_called()
    assert "sweep gate failed" in hooks_log_text(hooks_log)
    assert "Traceback" in hooks_log_text(hooks_log)


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
    notes = sweep._validate_writes([str(outside)], target, Changes())
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
    notes = sweep._validate_writes([str(other)], target, Changes())
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
    notes = sweep._validate_writes(["learnings/rel.md"], target, Changes())
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
    notes = sweep._validate_writes([str(tracked)], target, Changes())
    # Then the note survives with its committed content
    assert tracked.read_text(encoding="utf-8") == "the original"
    assert notes == [f"escaped-reverted: {tracked}"]


@pytest.mark.parametrize(
    ("name", "secret", "content"),
    [
        ("aws", "AKIAIOSFODNN7EXAMPLE", "key: {secret}"),
        ("github", "ghp_" + "a" * 36, "token: {secret}"),
        # Split so a secret scanner does not flag this file as holding a real key.
        (
            "pem",
            (
                "-----BEGIN RSA "
                "PRIVATE KEY-----\n"
                "MIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLYgJ\n"
                "-----END RSA "
                "PRIVATE KEY-----"
            ),
            "{secret}\n",
        ),
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
    notes = sweep._validate_writes([str(written)], target, Changes())
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
    notes = sweep._validate_writes([str(written)], target, Changes())
    # Then it is unchanged and unremarked
    assert written.read_text(encoding="utf-8") == "nothing secret here"
    assert notes == []


def test_validate_writes_missing_file_no_note(tmp_path: Path) -> None:
    """Verify a reported path that was never written produces no note."""
    # Given a path inside the project folder that does not exist
    sweep, target = _sweep_with_data(tmp_path)
    ghost = target.project_dir / "ghost.md"
    # When validating
    notes = sweep._validate_writes([str(ghost)], target, Changes())
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
    notes = sweep._validate_writes([str(ghost)], target, Changes())

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
# Sweep._validate_writes (hash-derived: writes Bash made, never reported)
# ---------------------------------------------------------------------------


def test_a_secret_bash_wrote_into_the_project_dir_is_scrubbed(tmp_path, sweep_factory):
    """Verify a changed file is scrubbed even when no tool reported it."""
    target = _target_at(tmp_path / "vault")
    leaked = target.learnings_dir / "leak.md"
    leaked.write_text("token = 'ghp_" + "a" * 36 + "'", encoding="utf-8")

    notes = sweep_factory()._validate_writes([], target, Changes(created=(leaked.resolve(),)))

    assert any(note.startswith("secret-redacted") for note in notes)
    assert "ghp_" not in leaked.read_text(encoding="utf-8")


def test_a_file_the_run_did_not_change_is_not_rescanned(tmp_path, sweep_factory):
    """Verify the hash pass skips files absent from the change set."""
    target = _target_at(tmp_path / "vault")
    old = target.learnings_dir / "old.md"
    old.write_text("token = 'ghp_" + "b" * 36 + "'", encoding="utf-8")

    notes = sweep_factory()._validate_writes([], target, Changes())

    assert notes == []


def test_a_binary_file_does_not_abort_the_scrub(tmp_path, sweep_factory):
    """Verify an undecodable file next to a leaking page is skipped, not fatal to the pass."""
    target = _target_at(tmp_path / "vault")
    binary = target.learnings_dir / "stub.bin"
    binary.write_bytes(b"\x00\x8a\xff")
    leaked = target.learnings_dir / "leak.md"
    leaked.write_text("token = 'ghp_" + "a" * 36 + "'", encoding="utf-8")

    notes = sweep_factory()._validate_writes(
        [], target, Changes(created=(binary.resolve(), leaked.resolve()))
    )

    assert any(note.startswith("secret-redacted") for note in notes)
    assert "ghp_" not in leaked.read_text(encoding="utf-8")


def test_validate_writes_names_each_deletion(tmp_path, sweep_factory):
    """Verify a deleted file is reported in the notes."""
    target = _target_at(tmp_path / "vault")
    gone = target.learnings_dir / "gone.md"

    notes = sweep_factory()._validate_writes([], target, Changes(deleted=(gone,)))

    assert notes == [f"deleted: {gone}"]


# ---------------------------------------------------------------------------
# Sweep._run_job (fake runner, never spawns real claude)
# ---------------------------------------------------------------------------


def _job_store(tmp_path: Path) -> Store:
    store = store_at(tmp_path)
    store.state_dir.mkdir(parents=True)
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
        self.notes: list[str] = []

    def project_lookup(self, *, cwd: Path, env: dict, timeout: int) -> Lookup:
        return Lookup(
            {
                "slug": "proj",
                "project_dir": str(self.target.project_dir),
                "learnings": str(self.target.learnings_dir),
                "backlog": str(self.target.backlog_path),
                "logs": str(self.target.logs_dir),
            }
        )

    def commit(self, *, env: dict, note: str = "") -> CommitOutcome:
        self.notes.append(note)
        return CommitOutcome(sha="abc1234")


class _EnvSpyVault(_FakeVault):
    """A `_FakeVault` that records the env it was resolved with, for `_prepare` tests."""

    def __init__(self, target: Target) -> None:
        super().__init__(target)
        self.seen_env: dict | None = None

    def project_lookup(self, *, cwd: Path, env: dict, timeout: int) -> Lookup:
        self.seen_env = dict(env)
        return super().project_lookup(cwd=cwd, env=env, timeout=timeout)


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


def test_prepare_returns_the_lookup_reason_for_an_unregistered_project(tmp_path: Path) -> None:
    """Verify _prepare skips a project the vault cannot resolve, naming why."""

    # Given a vault whose lookup reports the project as unregistered
    class _UnregisteredVault(_FakeVault):
        def project_lookup(self, *, cwd: Path, env: dict, timeout: int) -> Lookup:
            return Lookup(None, "project not registered")

    store = store_at(tmp_path)
    target = _target_at(tmp_path / "vault")
    sweep = Sweep(store, SessionMemoryConfig(), _FakeRunner([]), _UnregisteredVault(target))
    job = SweepJob(window=[], cwd=str(tmp_path), session_id="")

    # When preparing / Then the reason is returned
    assert sweep._prepare(job) == "project not registered"


def test_prepare_names_the_first_missing_key_when_the_paths_are_incomplete(tmp_path: Path) -> None:
    """Verify _prepare skips a payload missing a key Target.from_paths requires."""

    # Given a vault whose resolved paths are missing required keys
    class _IncompleteVault(_FakeVault):
        def project_lookup(self, *, cwd: Path, env: dict, timeout: int) -> Lookup:
            return Lookup({"slug": "demo", "project_dir": str(self.target.project_dir)})

    store = store_at(tmp_path)
    target = _target_at(tmp_path / "vault")
    sweep = Sweep(store, SessionMemoryConfig(), _FakeRunner([]), _IncompleteVault(target))
    job = SweepJob(window=[], cwd=str(tmp_path), session_id="")

    # When preparing / Then the first absent key is named
    assert sweep._prepare(job) == "project payload lacked learnings"


class _RecordingRunner:
    """Captures the prompt it is handed so tests can assert on the sweep body."""

    def __init__(self) -> None:
        self.prompt: str | None = None
        self.extra_env: Mapping[str, str] | None = None

    def run(
        self, prompt: str, *, cwd: str, extra_env: Mapping[str, str] | None = None
    ) -> RunResult:
        self.prompt = prompt
        self.extra_env = extra_env
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


def test_run_job_commit_carries_the_log_summary(tmp_path: Path) -> None:
    """Verify the commit note is the summary of this session's log."""
    store = _job_store(tmp_path)
    vault_target = _target_at(tmp_path / "vault")
    (vault_target.logs_dir / "2026-01-01-demo.md").write_text(
        "---\nid: x\nsession_id: abc\nsummary: Made the handler idempotent\n---\n\nbody\n",
        encoding="utf-8",
    )
    vault = _FakeVault(vault_target)
    sweep = Sweep(store, SessionMemoryConfig(), _FakeRunner([]), vault)

    sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id="abc"))

    assert vault.notes == ["Made the handler idempotent"]


def test_log_summary_unquotes_the_first_summary_line(tmp_path: Path) -> None:
    """Verify _log_summary reads a quoted frontmatter summary and ignores body lines."""
    path = tmp_path / "log.md"
    path.write_text('---\nsummary: "Fixed it: done"\n---\n\nsummary: not this\n', encoding="utf-8")

    assert sweep_mod._log_summary(path) == "Fixed it: done"
    assert sweep_mod._log_summary(tmp_path / "missing.md") == ""


# Rendered by the CLI's own `frontmatter.serialize`, which wraps at 88 columns.
_WRAPPED_SUMMARIES = [
    (
        (
            "summary: Made the charge handler idempotent so retried webhooks never double-bill"
            " a customer,\n  and documented the key expiry window in the learnings\n"
        ),
        (
            "Made the charge handler idempotent so retried webhooks never double-bill a customer,"
            " and documented the key expiry window in the learnings"
        ),
    ),
    (
        (
            "summary: 'Fixed it: the handler now retries, and it''s idempotent across every"
            " webhook delivery\n  the provider sends after a timeout'\n"
        ),
        (
            "Fixed it: the handler now retries, and it's idempotent across every webhook delivery"
            " the provider sends after a timeout"
        ),
    ),
    (
        (
            'summary: "Tab\\there \\"quoted\\" and a long tail that keeps going on and on past'
            ' the eighty-eight\\\n  \\ column width limit"\n'
        ),
        (
            'Tab here "quoted" and a long tail that keeps going on and on past the eighty-eight'
            " column width limit"
        ),
    ),
]


@pytest.mark.parametrize(("summary_yaml", "expected"), _WRAPPED_SUMMARIES)
def test_log_summary_folds_a_wrapped_summary(
    tmp_path: Path, summary_yaml: str, expected: str
) -> None:
    """Verify a summary the CLI wrapped onto continuation lines is read whole and unquoted."""
    # Given a log whose summary the serializer folded across lines
    path = tmp_path / "log.md"
    path.write_text(
        f"---\ntitle: t\n{summary_yaml}session_id: abc\n---\n\nbody\n", encoding="utf-8"
    )
    # When reading the summary / Then the whole sentence comes back on one line
    assert sweep_mod._log_summary(path) == expected


def test_log_summary_matches_the_cli_serializer(tmp_path: Path) -> None:
    """Verify every wrapped form the CLI's serializer produces reads back to the original."""
    summaries = [expected for _, expected in _WRAPPED_SUMMARIES]
    for index, summary in enumerate(summaries):
        # Given a log written by the CLI's own serializer
        path = tmp_path / f"log-{index}.md"
        path.write_text(
            serialize({"summary": summary, "session_id": "s"}, "body"), encoding="utf-8"
        )
        # When reading the summary / Then it round-trips
        assert sweep_mod._log_summary(path) == summary


# (summary as written, summary as a commit body shows it). Each needs an escape or a
# line break the serializer emits only for a character outside printable ASCII.
_ESCAPED_SUMMARIES = [
    ("ship it \U0001f600 now\a", "ship it \U0001f600 now"),
    ("\ufeffbom at the start", "bom at the start"),
    ("ring the \a bell", "ring the bell"),
    ("line\u2028separated", "line separated"),
    ("para\u2029graph", "para graph"),
    (
        "a long one " * 8 + "\u2028 then more " * 5,
        "a long one " * 7 + "a long one" + " then more" * 5,
    ),
    (
        "x" * 80 + " \U0001f600 \a and more words to wrap past the width",
        "x" * 80 + " \U0001f600 and more words to wrap past the width",
    ),
    ("nul \x00 esc \x1b del \x7f nel \x85 nbsp \xa0 ok", "nul esc del nel nbsp ok"),
    ("two\n\nparagraphs\ttabbed", "two paragraphs tabbed"),
]


@pytest.mark.parametrize(("summary", "expected"), _ESCAPED_SUMMARIES)
def test_log_summary_decodes_every_yaml_escape(tmp_path: Path, summary: str, expected: str) -> None:
    """Verify a summary the CLI escaped reads back decoded, on one line, with no raw escape."""
    # Given a log written by the CLI's own serializer
    path = tmp_path / "log.md"
    path.write_text(serialize({"summary": summary, "session_id": "s"}, "body"), encoding="utf-8")
    # When reading the summary
    folded = sweep_mod._log_summary(path)
    # Then every escape is decoded and every control character is gone
    assert folded == expected
    assert "\\" not in folded
    assert all(ch == " " or ch.isprintable() for ch in folded)


@pytest.mark.parametrize(
    ("escape", "char"),
    [
        ("\\0", "\x00"),
        ("\\a", "\a"),
        ("\\b", "\b"),
        ("\\t", "\t"),
        ("\\n", "\n"),
        ("\\v", "\v"),
        ("\\f", "\f"),
        ("\\r", "\r"),
        ("\\e", "\x1b"),
        ("\\ ", " "),
        ('\\"', '"'),
        ("\\/", "/"),
        ("\\\\", "\\"),
        ("\\N", "\x85"),
        ("\\_", "\xa0"),
        ("\\L", "\u2028"),
        ("\\P", "\u2029"),
        ("\\x41", "A"),
        ("\\u00e9", "\u00e9"),
        ("\\U0001F600", "\U0001f600"),
    ],
)
def test_decode_double_quoted_handles_each_escape(escape: str, char: str) -> None:
    """Verify each YAML double-quoted escape decodes to its character."""
    # When decoding a value holding the escape / Then it becomes the character
    assert sweep_mod._decode_double_quoted(f"a{escape}b") == f"a{char}b"


def test_run_job_clean_write_logs_and_releases_lock(tmp_path: Path) -> None:
    """Verify run_job validates writes, commits, logs, and releases the lock on a clean run."""
    # Given a store with a clean target file
    hooks_log = _capture_log(tmp_path)
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
    assert notes == ["committed abc1234"]
    assert "sweep finished" in hooks_log_text(hooks_log)
    assert not in_progress(store)


def test_run_job_notes_a_skipped_commit(tmp_path: Path) -> None:
    """Verify a vault that commits nothing (e.g. a clean tree already) is noted as skipped."""

    # Given a vault whose commit call returns None
    class _NoCommitVault(_FakeVault):
        def commit(self, *, env: dict, note: str = "") -> CommitOutcome:
            return CommitOutcome(reason="clean")

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
    assert notes == ["commit skipped: clean"]


def test_run_job_skips_its_commit_while_another_projects_sweep_runs(tmp_path: Path) -> None:
    """Verify a worker leaves the vault commit to another sweep still writing."""
    # Given another project's sweep lock held under this machine's state root
    env = {"XDG_STATE_HOME": str(tmp_path / "xdg")}
    other = Lock(_project_lock(env, "other"))
    assert other.acquire() is True
    vault = _FakeVault(_target_at(tmp_path / "vault"))
    sweep = Sweep(_job_store(tmp_path), SessionMemoryConfig(), _FakeRunner([]), vault, env=env)
    try:
        # When this project's worker finishes
        notes = sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id=""))
    finally:
        other.release()
    # Then it does not commit, since the other worker commits the whole vault when it ends
    assert vault.notes == []
    assert notes == ["commit skipped: another sweep in progress"]


def test_run_job_commits_when_only_its_own_lock_is_held(tmp_path: Path) -> None:
    """Verify a worker's own lock under the state root never blocks its commit."""
    # Given a store that lives under the state root the worker probes
    env = {"XDG_STATE_HOME": str(tmp_path / "xdg")}
    store = Store(key="k", state_dir=_project_lock(env, "self").parent)
    vault = _FakeVault(_target_at(tmp_path / "vault"))
    sweep = Sweep(store, SessionMemoryConfig(), _FakeRunner([]), vault, env=env)
    # When the worker runs, holding its own lock throughout
    notes = sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id=""))
    # Then it commits
    assert notes == ["committed abc1234"]


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
    assert notes == [f"escaped-quarantined: {outside}", "committed abc1234"]
    assert not in_progress(store)
    quarantined = list((store.state_dir / "quarantine").glob("*-outside.md"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "nope"


def test_run_job_releases_lock_on_runner_failure(tmp_path: Path) -> None:
    """Verify run_job releases the lock even when the runner raises."""
    # Given a runner that always raises

    class _Exploding:
        def run(
            self, prompt: str, *, cwd: str, extra_env: Mapping[str, str] | None = None
        ) -> RunResult:
            msg = "boom"
            raise RuntimeError(msg)

    store = _job_store(tmp_path)
    sweep = Sweep(store, SessionMemoryConfig(), _Exploding())
    job = SweepJob(window=[], cwd=str(tmp_path), session_id="")
    # When running the job
    notes = sweep._run_job(job)
    # Then it returns no notes and the lock is still released
    assert notes == []
    assert not in_progress(store)


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
        def run(
            self, prompt: str, *, cwd: str, extra_env: Mapping[str, str] | None = None
        ) -> RunResult:
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
    assert not in_progress(store)


def test_run_job_waits_for_a_held_lock_then_runs(tmp_path: Path) -> None:
    """Verify a sweep queued behind another waits for its lock, then runs."""
    # Given another sweep holding the lock, released during the first wait
    hooks_log = _capture_log(tmp_path)
    store = _job_store(tmp_path)
    held = Lock(store.lock_path)
    assert held.acquire() is True
    waits: list[float] = []

    def _sleep(seconds: float) -> None:
        waits.append(seconds)
        held.release()

    runner = _RecordingRunner()
    sweep = Sweep(
        store,
        SessionMemoryConfig(),
        runner,
        _FakeVault(_target_at(tmp_path / "vault")),
        sleep=_sleep,
    )
    job = SweepJob(window=[], cwd=str(tmp_path), session_id="")

    # When running the job
    sweep._run_job(job)

    # Then it waited once, ran, and logged the queue and dequeue
    assert waits == [sweep_mod.QUEUE_POLL]
    assert runner.prompt is not None
    text = hooks_log_text(hooks_log)
    assert "sweep queued" in text
    assert "sweep dequeued after about 5s" in text


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (SessionMemoryConfig(), 3600.0),
        (SessionMemoryConfig(sweep_timeout=3000), 12480.0),
    ],
)
def test_queue_wait_scales_with_the_sweep_timeout(
    config: SessionMemoryConfig, expected: float
) -> None:
    """Verify the queue wait covers several queued sweeps and never drops below an hour."""
    # When computing the wait / Then it is the floor or four sweeps' worth, whichever is longer
    assert sweep_mod.queue_wait(config) == expected


def test_run_job_queue_wait_follows_the_configured_timeout(tmp_path: Path) -> None:
    """Verify a queued worker keeps polling for as long as its configured timeout allows."""
    # Given a lock held for the whole run and a sweep timeout of 3000 seconds
    store = _job_store(tmp_path)
    held = Lock(store.lock_path)
    assert held.acquire() is True
    waits: list[float] = []
    sweep = Sweep(
        store,
        SessionMemoryConfig(sweep_timeout=3000),
        _RecordingRunner(),
        _FakeVault(_target_at(tmp_path / "vault")),
        sleep=waits.append,
    )
    try:
        # When running the job
        sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id=""))
    finally:
        held.release()
    # Then it polled every 5 seconds for 12480 seconds
    assert sum(waits) == 12480.0
    assert set(waits) == {sweep_mod.QUEUE_POLL}


def test_run_job_gives_up_after_the_queue_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify a sweep whose lock never frees gives up after the queue wait and never runs."""
    # Given a lock held for the whole run and a short queue wait
    monkeypatch.setattr(sweep_mod, "queue_wait", lambda _config: 10.0)
    hooks_log = _capture_log(tmp_path)
    store = _job_store(tmp_path)
    held = Lock(store.lock_path)
    assert held.acquire() is True
    runner = _RecordingRunner()
    sweep = Sweep(
        store,
        SessionMemoryConfig(),
        runner,
        _FakeVault(_target_at(tmp_path / "vault")),
        sleep=lambda _: None,
    )
    job = SweepJob(window=[], cwd=str(tmp_path), session_id="")

    try:
        # When running the job
        notes = sweep._run_job(job)
        # Then it gives up without running, and the holder keeps its lock
        assert notes == []
        assert runner.prompt is None
        assert in_progress(store) is True
    finally:
        held.release()
    assert re.search(r"WARNING.*lock still held after 10s", hooks_log_text(hooks_log))


def test_run_job_releases_the_lock_it_took(tmp_path: Path) -> None:
    """Verify a finished sweep leaves the lock free for the next one."""
    # Given a free lock
    store = _job_store(tmp_path)
    sweep = Sweep(
        store, SessionMemoryConfig(), _RecordingRunner(), _FakeVault(_target_at(tmp_path / "vault"))
    )
    # When running the job
    sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id=""))
    # Then no sweep is in progress
    assert not in_progress(store)


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
    assert "The three-gate test" in rendered
    assert "{{capture_criteria}}" not in rendered
    # Then a literal "{{name}}" never reaches the model as an instruction it cannot follow.
    assert "{{" not in rendered


def test_run_job_logs_a_successful_sweep_under_the_worker_name(tmp_path: Path) -> None:
    """Verify the worker relabels itself `sweep` and reports the files and the commit."""
    hooks_log = _capture_log(tmp_path)
    store = store_at(tmp_path)
    target = _target_at(tmp_path / "vault")
    written = target.logs_dir / "note.md"
    sweep = Sweep(
        store,
        SessionMemoryConfig(),
        _WritingRunner(lambda: written.write_text("clean", encoding="utf-8")),
        _FakeVault(target),
    )
    sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id="sess-123"))
    (line,) = [x for x in hooks_log_text(hooks_log).splitlines() if "sweep finished" in x]
    assert (
        "INFO  [sweep       ] demo: sweep finished: ok, 1 created, 0 modified, 0 deleted, committed abc1234 (session=sess-123)"
        in line
    )


def test_run_job_logs_a_failed_sweep_with_the_exit_code_and_stderr(tmp_path: Path) -> None:
    """Verify a failed run is distinguishable from one never attempted, and names the cause."""

    class _FailingRunner:
        def run(
            self, prompt: str, *, cwd: str, extra_env: Mapping[str, str] | None = None
        ) -> RunResult:
            return RunResult(
                success=False,
                exit_code=-3,
                changed_files=[],
                text="",
                stderr="claude CLI not found",
            )

    hooks_log = _capture_log(tmp_path)
    sweep = Sweep(
        store_at(tmp_path),
        SessionMemoryConfig(),
        _FailingRunner(),
        _FakeVault(_target_at(tmp_path / "vault")),
    )
    sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id="fail-001"))
    text = hooks_log_text(hooks_log)
    assert (
        "ERROR [sweep       ] demo: sweep failed: claude exited -3 (stderr: claude CLI not found),"
        " 0 created, 0 modified, 0 deleted, committed abc1234 (session=fail-001)" in text
    )


def test_run_job_logs_the_result_text_of_a_failed_run(tmp_path: Path) -> None:
    """Verify a failed run logs the result text beside the stderr tail."""

    class _FailingRunner:
        def run(
            self, prompt: str, *, cwd: str, extra_env: Mapping[str, str] | None = None
        ) -> RunResult:
            return RunResult(
                success=False,
                exit_code=1,
                changed_files=[],
                text="Invalid API key",
                stderr="⚠ claude.ai connectors are disabled because ANTHROPIC_API_KEY is set",
            )

    hooks_log = _capture_log(tmp_path)
    sweep = Sweep(
        store_at(tmp_path),
        SessionMemoryConfig(),
        _FailingRunner(),
        _FakeVault(_target_at(tmp_path / "vault")),
    )
    sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id="fail-002"))
    text = hooks_log_text(hooks_log)
    assert "Invalid API key" in text
    assert "stderr: ⚠ claude.ai connectors are disabled" in text


def _failed_run_log(tmp_path: Path, *, text: str, stderr: str, session: str) -> str:
    class _FailingRunner:
        def run(
            self, prompt: str, *, cwd: str, extra_env: Mapping[str, str] | None = None
        ) -> RunResult:
            return RunResult(success=False, exit_code=1, changed_files=[], text=text, stderr=stderr)

    hooks_log = _capture_log(tmp_path)
    sweep = Sweep(
        store_at(tmp_path),
        SessionMemoryConfig(),
        _FailingRunner(),
        _FakeVault(_target_at(tmp_path / "vault")),
    )
    sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id=session))
    return hooks_log_text(hooks_log)


def test_run_job_logs_only_the_result_text_when_stderr_is_empty(tmp_path: Path) -> None:
    """Verify a failed run with no stderr logs the result text without a stderr label."""
    text = _failed_run_log(tmp_path, text="Invalid API key", stderr="", session="f-3")
    assert "claude exited 1 (Invalid API key), 0 created" in text


def test_run_job_logs_no_output_when_a_failed_run_says_nothing(tmp_path: Path) -> None:
    """Verify a failed run with neither result text nor stderr still names the gap."""
    text = _failed_run_log(tmp_path, text="", stderr="", session="f-4")
    assert "claude exited 1 (no output), 0 created" in text


def test_run_job_logs_a_multiline_failure_on_one_line(tmp_path: Path) -> None:
    """Verify newlines in the result text and stderr never split the log line."""
    text = _failed_run_log(tmp_path, text="bad\nkey\n", stderr="a\nb", session="f-5")
    assert "claude exited 1 (bad key; stderr: a b), 0 created" in text


def test_run_job_logs_what_a_failed_run_wrote_and_committed(tmp_path: Path) -> None:
    """Verify a failed run still reports its writes, its notes, and the commit outcome."""

    # Given a run that writes a malformed learning and then exits non-zero
    class _WriteThenFail:
        def __init__(self, page: Path) -> None:
            self.page = page

        def run(
            self, prompt: str, *, cwd: str, extra_env: Mapping[str, str] | None = None
        ) -> RunResult:
            self.page.write_text("no frontmatter\n", encoding="utf-8")
            return RunResult(success=False, exit_code=2, changed_files=[], text="", stderr="")

    hooks_log = _capture_log(tmp_path)
    target = _target_at(tmp_path / "vault")
    page = target.learnings_dir / "half-done.md"
    sweep = Sweep(
        _job_store(tmp_path), SessionMemoryConfig(), _WriteThenFail(page), _FakeVault(target)
    )
    # When the worker finishes
    sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id="f-6"))
    # Then the error line names the change counts, the commit, and the notes
    (line,) = [x for x in hooks_log_text(hooks_log).splitlines() if "sweep failed" in x]
    assert (
        "sweep failed: claude exited 2 (no output), 1 created, 0 modified, 0 deleted,"
        " committed abc1234 (notes: malformed-quarantined: "
    ) in line
    assert "(no frontmatter)) (session=f-6)" in line


def test_run_job_without_a_vault_logs_the_skip(tmp_path: Path) -> None:
    """Verify an unreachable vault leaves a trace rather than a session vanishing silently."""
    hooks_log = _capture_log(tmp_path)
    sweep = Sweep(store_at(tmp_path), SessionMemoryConfig(), _FakeRunner([]))
    sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id="sess-novault"))
    assert (
        "sweep skipped: no CLI: neither PATH nor the plugin shim (session=sess-nov)"
        in hooks_log_text(hooks_log)
    )


def test_run_job_records_the_decline_when_the_model_found_nothing(tmp_path: Path) -> None:
    """Verify a model that chose not to record is logged as a success that wrote nothing."""
    hooks_log = _capture_log(tmp_path)
    sweep = Sweep(
        store_at(tmp_path),
        SessionMemoryConfig(),
        _TextRunner(f"Nothing durable here.\n{sweep_mod.NOTHING_TO_RECORD}"),
        _FakeVault(_target_at(tmp_path / "vault")),
    )
    sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id="sess-quiet"))
    text = hooks_log_text(hooks_log)
    assert (
        "sweep finished: ok, 0 created, 0 modified, 0 deleted, committed abc1234 (notes: nothing to record)"
        in text
    )


def test_run_job_logs_a_crash_inside_the_worker(tmp_path: Path) -> None:
    """Verify an exception the worker swallows is on record with its traceback."""

    class _ExplodingRunner:
        def run(
            self, prompt: str, *, cwd: str, extra_env: Mapping[str, str] | None = None
        ) -> RunResult:
            message = "kaboom"
            raise RuntimeError(message)

    hooks_log = _capture_log(tmp_path)
    sweep = Sweep(
        store_at(tmp_path),
        SessionMemoryConfig(),
        _ExplodingRunner(),
        _FakeVault(_target_at(tmp_path / "vault")),
    )
    sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id="sess-crash"))
    text = hooks_log_text(hooks_log)
    assert "sweep worker failed" in text
    assert "RuntimeError: kaboom" in text


def test_run_job_scrubs_a_file_written_through_bash(tmp_path: Path) -> None:
    """Verify a secret written without a tool report is redacted after the run."""
    store = _job_store(tmp_path)
    target = _target_at(tmp_path / "vault")
    leaked = target.logs_dir / "leak.md"
    sweep = Sweep(
        store,
        SessionMemoryConfig(),
        _WritingRunner(lambda: leaked.write_text("token: ghp_" + "a" * 36, encoding="utf-8")),
        _FakeVault(target),
    )

    sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id="sess-bash"))

    assert "ghp_" not in leaked.read_text(encoding="utf-8")


def test_run_job_reports_created_modified_and_deleted(tmp_path: Path) -> None:
    """Verify the result line counts each kind of change and a deletion is noted, not undone."""
    hooks_log = _capture_log(tmp_path)
    store = _job_store(tmp_path)
    target = _target_at(tmp_path / "vault")
    edited = target.learnings_dir / "edited.md"
    edited.write_text("before", encoding="utf-8")
    doomed = target.learnings_dir / "doomed.md"
    doomed.write_text("bye", encoding="utf-8")
    created = target.learnings_dir / "created.md"

    def _act() -> None:
        created.write_text("new", encoding="utf-8")
        edited.write_text("after", encoding="utf-8")
        doomed.unlink()

    sweep = Sweep(store, SessionMemoryConfig(), _WritingRunner(_act), _FakeVault(target))

    notes = sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id="sess-cmd"))

    text = hooks_log_text(hooks_log)
    assert "1 created, 1 modified, 1 deleted" in text
    assert f"deleted: {doomed.resolve()}" in notes
    assert not doomed.exists()


def test_run_job_scrubs_but_keeps_a_concurrent_edit(tmp_path: Path) -> None:
    """Verify an unreported edit to an existing spec is scrubbed in place, not reverted."""
    store = _job_store(tmp_path)
    target = _target_at(tmp_path / "vault")
    spec = target.project_dir / "specs" / "design.md"
    spec.parent.mkdir()
    spec.write_text("original", encoding="utf-8")
    sweep = Sweep(
        store,
        SessionMemoryConfig(),
        _WritingRunner(lambda: spec.write_text("edited key: ghp_" + "c" * 36, encoding="utf-8")),
        _FakeVault(target),
    )

    notes = sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id="sess-conc"))

    assert spec.exists()
    assert "«redacted-secret»" in spec.read_text(encoding="utf-8")
    assert f"secret-redacted: {spec.resolve()}" in notes
    assert not any(
        n.startswith(("escaped", "quarantined")) and str(spec.resolve()) in n for n in notes
    )


def test_run_job_leaves_the_target_of_a_new_symlink_alone(tmp_path: Path) -> None:
    """Verify a symlink created during the run never moves or edits the file it points at."""
    store = _job_store(tmp_path)
    target = _target_at(tmp_path / "vault")
    outside = tmp_path / "outside.md"
    outside.write_text("token: ghp_" + "d" * 36, encoding="utf-8")
    link = target.learnings_dir / "x.md"
    sweep = Sweep(
        store,
        SessionMemoryConfig(),
        _WritingRunner(lambda: link.symlink_to(outside)),
        _FakeVault(target),
    )

    notes = sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id="sess-link"))

    assert outside.read_text(encoding="utf-8") == "token: ghp_" + "d" * 36
    assert not any("escaped" in n or "quarantined" in n for n in notes)
    assert not (store.state_dir / "quarantine").exists()


# ---------------------------------------------------------------------------
# Sweep gate 2: the model decides whether the session is worth recording at all
# ---------------------------------------------------------------------------


class _TextRunner:
    """A runner that returns a caller-supplied final text, for the log-decision cases."""

    def __init__(self, text: str, *, changed_files: list[str] | None = None) -> None:
        self.text = text
        self.changed_files = changed_files or []
        self.prompt: str | None = None

    def run(
        self, prompt: str, *, cwd: str, extra_env: Mapping[str, str] | None = None
    ) -> RunResult:
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


def test_run_job_log_command_names_the_transcript_and_link(tmp_path: Path) -> None:
    """Verify the composed log command passes the transcript path and online link."""
    store = _job_store(tmp_path)
    runner = _RecordingRunner()
    sweep = Sweep(store, SessionMemoryConfig(), runner, _FakeVault(_target_at(tmp_path / "vault")))

    sweep._run_job(
        SweepJob(
            window=[],
            cwd=str(tmp_path),
            session_id="sess-xyz",
            transcript_path="/t/sess-xyz.jsonl",
            session_url="https://claude.ai/code/session_01XYZ",
        )
    )

    assert runner.prompt is not None
    assert "--transcript /t/sess-xyz.jsonl" in runner.prompt
    assert "--url https://claude.ai/code/session_01XYZ" in runner.prompt


def test_run_job_log_command_omits_what_the_session_lacks(tmp_path: Path) -> None:
    """Verify an unbridged session's command carries no empty --url or --transcript."""
    store = _job_store(tmp_path)
    runner = _RecordingRunner()
    sweep = Sweep(store, SessionMemoryConfig(), runner, _FakeVault(_target_at(tmp_path / "vault")))

    sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id="sess-xyz"))

    assert runner.prompt is not None
    assert "--url" not in runner.prompt
    assert "--transcript" not in runner.prompt


def test_run_job_does_not_record_a_productive_sweep_as_empty(tmp_path: Path) -> None:
    """Verify an ordinary sweep is never mislabelled as having recorded nothing."""
    # Given a model that wrote a log and said so
    hooks_log = _capture_log(tmp_path)
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
    assert "nothing to record" not in hooks_log_text(hooks_log)


def test_run_job_ignores_the_sentinel_when_the_model_actually_wrote(tmp_path: Path) -> None:
    """Verify a sweep that wrote notes is never logged as having recorded nothing.

    The sentinel is part of the prompt the agent summarizes, so it can surface in
    the final text of a run that did its job.
    """
    # Given a model that wrote a note and still echoed the sentinel
    hooks_log = _capture_log(tmp_path)
    store = store_at(tmp_path)
    target = _target_at(tmp_path / "vault")
    written = target.project_dir / "note.md"
    sweep = Sweep(
        store,
        SessionMemoryConfig(),
        _WritingRunner(
            lambda: written.write_text("real content", encoding="utf-8"),
            text=f"Considered {sweep_mod.NOTHING_TO_RECORD} and wrote anyway.",
        ),
        _FakeVault(target),
    )
    # When the job runs
    sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id="sess-mixed"))
    # Then the write outranks the sentinel
    assert "nothing to record" not in hooks_log_text(hooks_log)


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

        def run(
            self, prompt: str, *, cwd: str, extra_env: Mapping[str, str] | None = None
        ) -> RunResult:  # pragma: no cover - never gated in
            raise AssertionError

    def _fake_home() -> Path:
        return tmp_path / "home"

    monkeypatch.setattr(sweep_mod, "ClaudeRunner", _Spy)
    monkeypatch.setattr(Path, "home", staticmethod(_fake_home))
    # When the sweep is wired up
    sweep_mod.run_sweep({"cwd": str(tmp_path)}, env={"XDG_STATE_HOME": str(tmp_path / "state")})
    # Then the runner carries the root the CLI can only read from its environment
    assert captured["extra_env"] == {sweep_mod.ROOT_ENV: str(root)}


def test_run_sweep_builds_the_runner_with_the_configured_timeout(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """Verify the sweep's runner is constructed with the configured timeout."""
    # Given a located vault and a config with a custom timeout
    root = tmp_path / "vault-root"
    (root / "_system").mkdir(parents=True)
    (root / "_system" / "vault.toml").write_text("", encoding="utf-8")
    runner_cls = mocker.patch.object(sweep_mod, "ClaudeRunner", autospec=True)
    # When the sweep is wired up
    sweep_mod.run_sweep(
        {"cwd": str(tmp_path), "transcript_path": ""},
        env={"XDG_STATE_HOME": str(tmp_path / "state"), sweep_mod.ROOT_ENV: str(root)},
        store=store_at(tmp_path),
        config=SessionMemoryConfig(sweep_timeout=42),
    )
    # Then the runner got that timeout
    assert runner_cls.call_args.kwargs["timeout"] == 42


def test_run_sweep_reuses_a_store_the_caller_already_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify a hook that already resolved its store is not charged for it a second time."""

    def _resolved_twice(**_: object) -> Store:
        message = "resolved twice"
        raise AssertionError(message)

    # Given a store the caller resolved for its own log line, a reachable vault so
    # the sweep proceeds past the no-vault check into gating, and Store.for_cwd
    # wired to fail if called again
    root = tmp_path / "vault-root"
    (root / "_system").mkdir(parents=True)
    (root / "_system" / "vault.toml").write_text("", encoding="utf-8")
    hooks_log = _capture_log(tmp_path)
    store = store_at(tmp_path)
    monkeypatch.setattr(sweep_mod.Store, "for_cwd", staticmethod(_resolved_twice))
    # When the sweep runs with that store passed straight through
    sweep_mod.run_sweep(
        {"cwd": str(tmp_path), "transcript_path": ""},
        env={"XDG_STATE_HOME": str(tmp_path / "state"), sweep_mod.ROOT_ENV: str(root)},
        store=store,
    )
    # Then it gates on the given store without ever resolving one itself
    assert "sweep skipped: no transcript" in hooks_log_text(hooks_log)


def test_run_sweep_reuses_a_config_the_caller_already_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify a hook that already loaded its config is not charged for loading it again."""

    def _loaded_twice(**_: object) -> SessionMemoryConfig:
        message = "loaded twice"
        raise AssertionError(message)

    # Given a config the caller already loaded, and SessionMemoryConfig.load wired
    # to fail if called again
    monkeypatch.setattr(sweep_mod.SessionMemoryConfig, "load", staticmethod(_loaded_twice))
    hooks_log = _capture_log(tmp_path)
    store = store_at(tmp_path)
    # When the sweep runs with that config passed straight through
    sweep_mod.run_sweep(
        {"cwd": str(tmp_path), "transcript_path": ""},
        env={"XDG_STATE_HOME": str(tmp_path / "state")},
        store=store,
        config=SessionMemoryConfig(),
    )
    # Then it proceeds on the given config without ever loading one itself
    assert "sweep skipped: no vault" in hooks_log_text(hooks_log)


def test_run_sweep_chooses_no_cli_before_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify the hook leaves the version handshake to the detached worker."""

    def _handshake(*_: object, **__: object) -> None:
        message = "handshake ran inside the hook"
        raise AssertionError(message)

    # Given a reachable vault and a handshake wired to fail if the hook runs it
    root = tmp_path / "vault-root"
    (root / "_system").mkdir(parents=True)
    (root / "_system" / "vault.toml").write_text("", encoding="utf-8")
    monkeypatch.setattr(sweep_mod.VaultCLI, "for_root", classmethod(_handshake))
    hooks_log = _capture_log(tmp_path)
    # When the sweep gates a session with no transcript
    sweep_mod.run_sweep(
        {"cwd": str(tmp_path), "transcript_path": ""},
        env={"XDG_STATE_HOME": str(tmp_path / "state"), sweep_mod.ROOT_ENV: str(root)},
        store=store_at(tmp_path),
        config=SessionMemoryConfig(),
    )
    # Then it reaches the gate without ever choosing a CLI
    assert "sweep skipped: no transcript" in hooks_log_text(hooks_log)


def test_run_job_chooses_the_cli_for_the_located_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify the worker resolves its CLI from the root the hook located."""
    store = _job_store(tmp_path)
    root = tmp_path / "vault"
    chosen: list[Path] = []

    def _for_root(cls: type, found: Path, *, env: object, version_timeout: int) -> _FakeVault:
        chosen.append(found)
        return _FakeVault(_target_at(root), root=found)

    monkeypatch.setattr(sweep_mod.VaultCLI, "for_root", classmethod(_for_root))
    runner = _RecordingRunner()
    sweep = Sweep(store, SessionMemoryConfig(), runner, vault_root=root)

    sweep._run_job(SweepJob(window=[_user("hi")], cwd=str(tmp_path), session_id="s"))

    assert chosen == [root]
    assert runner.prompt


def test_run_job_chooses_the_cli_with_the_worker_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify the worker gives its version handshake the worker timeout."""
    store = _job_store(tmp_path)
    root = tmp_path / "vault"
    seen: list[int] = []

    def _for_root(cls: type, found: Path, *, env: object, version_timeout: int) -> _FakeVault:
        seen.append(version_timeout)
        return _FakeVault(_target_at(root), root=found)

    monkeypatch.setattr(sweep_mod.VaultCLI, "for_root", classmethod(_for_root))
    sweep = Sweep(store, SessionMemoryConfig(), _RecordingRunner(), vault_root=root)

    sweep._run_job(SweepJob(window=[_user("hi")], cwd=str(tmp_path), session_id="s"))

    assert seen == [WORKER_CLI_TIMEOUT]


def test_run_job_logs_the_skip_reason(tmp_path: Path) -> None:
    """Verify a lookup failure is logged with its reason rather than a generic skip."""

    class _TimedOutVault(_FakeVault):
        def project_lookup(self, *, cwd: Path, env: dict, timeout: int) -> Lookup:
            return Lookup(None, "project timed out after 120s")

    hooks_log = _capture_log(tmp_path)
    sweep = Sweep(
        store_at(tmp_path),
        SessionMemoryConfig(),
        _FakeRunner([]),
        _TimedOutVault(_target_at(tmp_path / "vault")),
    )
    sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id="sess-timeout"))
    assert "sweep skipped: project timed out after 120s" in hooks_log_text(hooks_log)


def test_run_sweep_skips_before_gating_when_there_is_no_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify a vault-less session logs the skip and never gates or spawns a worker."""
    spawned: list[object] = []
    monkeypatch.setattr(sweep_mod.Sweep, "_spawn_detached", lambda self, job: spawned.append(job))
    hooks_log = _capture_log(tmp_path)

    # When the sweep runs with nothing naming a vault
    sweep_mod.run_sweep(
        {"cwd": str(tmp_path), "transcript_path": "x.jsonl"},
        env={"XDG_STATE_HOME": str(tmp_path / "state")},
    )

    # Then it logs why and never reaches the gate or the worker
    assert "sweep skipped: no vault" in hooks_log_text(hooks_log)
    assert spawned == []


# ---------------------------------------------------------------------------
# Sweep log body inputs: the composed command, the commit list, the carry-forward log
# ---------------------------------------------------------------------------


def test_the_log_command_carries_no_body(sweep_factory, tmp_path):
    """Verify the composed log command never includes --body, which only the model can write."""
    sweep = sweep_factory()
    job = _job(cwd=str(tmp_path), session_id="abc")

    command = sweep._log_command(job)

    assert "--body" not in command
    assert "--session-id abc" in command
    assert "log" in command


@pytest.fixture
def utc_clock(monkeypatch: pytest.MonkeyPatch):
    """Pin local time to UTC so a rendered clock time is the same on every machine."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _title_handed_to_model(tmp_path: Path, job: SweepJob) -> str:
    """Run `job` and return the log title the model's environment received."""
    runner = _RecordingRunner()
    sweep = Sweep(
        _job_store(tmp_path), SessionMemoryConfig(), runner, _FakeVault(_target_at(tmp_path / "v"))
    )
    sweep._run_job(job)
    assert runner.extra_env is not None
    return runner.extra_env[LOG_TITLE_ENV]


@pytest.mark.usefixtures("utc_clock")
def test_run_job_hands_the_title_to_the_model_env(tmp_path):
    """Verify the title reaches the model through its environment, as the session's start."""
    job = SweepJob(window=[], cwd=str(tmp_path), session_id="abc", started="2026-09-03T17:53:10Z")

    assert _title_handed_to_model(tmp_path, job) == "2026-09-03 17:53"


@pytest.mark.usefixtures("utc_clock")
def test_the_log_title_renders_the_start_in_local_time(tmp_path):
    """Verify a start recorded with an offset is shown on the local clock, not the recorded one."""
    job = SweepJob(
        window=[], cwd=str(tmp_path), session_id="abc", started="2026-09-03T23:30:00-04:00"
    )

    assert _title_handed_to_model(tmp_path, job) == "2026-09-04 03:30"


@pytest.mark.parametrize("started", ["", "not a time"])
def test_the_log_title_falls_back_to_now_without_a_usable_start(tmp_path, started):
    """Verify a session with no readable start is still titled with a date and a time."""
    job = SweepJob(window=[], cwd=str(tmp_path), session_id="abc", started=started)

    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", _title_handed_to_model(tmp_path, job))


def test_run_job_log_command_carries_no_title(tmp_path):
    """Verify the prompt's log command omits --title, which the environment supplies."""
    runner = _RecordingRunner()
    sweep = Sweep(
        _job_store(tmp_path), SessionMemoryConfig(), runner, _FakeVault(_target_at(tmp_path / "v"))
    )

    sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id="abc"))

    assert runner.prompt is not None
    command_lines = [line for line in runner.prompt.splitlines() if " log --session-id abc" in line]
    assert len(command_lines) == 1
    assert "--title" not in command_lines[0]


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
# Sweep._redirect_stdio (best-effort IO, must never raise)
# ---------------------------------------------------------------------------


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


def test_spawn_detached_skips_when_the_first_fork_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify a fork failure is treated like any other unavailable resource: skip, don't block."""
    # Given os.fork forced to always fail
    hooks_log = _capture_log(tmp_path)
    store = store_at(tmp_path)
    sweep = Sweep(store, SessionMemoryConfig(), _FakeRunner([]))

    def _raise() -> None:
        message = "cannot fork"
        raise OSError(message)

    monkeypatch.setattr(os, "fork", _raise)

    # When spawning the detached worker
    sweep._spawn_detached(_job(cwd=str(tmp_path)))

    # Then the sweep is skipped and says so
    assert "sweep skipped: fork failed" in hooks_log_text(hooks_log)


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


def test_spawn_detached_intermediate_child_exits_when_the_second_fork_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify an intermediate child that cannot fork the daemon exits cleanly."""
    # Given the first fork landing in the intermediate child, and the second
    # fork forced to fail
    store = store_at(tmp_path)
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

    # Then the intermediate child exits cleanly
    assert excinfo.value.code == 0


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

    def run(
        self, prompt: str, *, cwd: str, extra_env: Mapping[str, str] | None = None
    ) -> RunResult:
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


_GOOD_LEARNING = '---\ntitle: "T"\nuuid: u\nsummary: "S"\ncreated: "2026-10-04"\nupdated: "2026-10-04"\n---\n\nBody\n'


def test_run_job_quarantines_a_malformed_new_learning(tmp_path: Path) -> None:
    """Verify a new learning without the page shape is set aside and noted."""
    store = _job_store(tmp_path)
    target = _target_at(tmp_path / "vault")
    bad = target.learnings_dir / "bad-page.md"
    sweep = Sweep(
        store,
        SessionMemoryConfig(),
        _WritingRunner(lambda: bad.write_text("---\nname: x\n---\nBody\n", encoding="utf-8")),
        _FakeVault(target),
    )

    notes = sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id="sess-bad"))

    assert not bad.exists()
    assert f"malformed-quarantined: {bad.resolve()} (unexpected keys: name)" in notes
    assert len(list((store.state_dir / "quarantine").glob("*-bad-page.md"))) == 1


def test_run_job_keeps_a_conformant_new_learning(tmp_path: Path) -> None:
    """Verify a page with the right shape is left in place."""
    store = _job_store(tmp_path)
    target = _target_at(tmp_path / "vault")
    good = target.learnings_dir / "good-page.md"
    sweep = Sweep(
        store,
        SessionMemoryConfig(),
        _WritingRunner(lambda: good.write_text(_GOOD_LEARNING, encoding="utf-8")),
        _FakeVault(target),
    )

    notes = sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id="sess-good"))

    assert good.exists()
    assert not any(n.startswith("malformed") for n in notes)


def test_run_job_restores_a_learning_rewritten_into_auto_memory_shape(tmp_path: Path) -> None:
    """Verify a committed learning rewritten with the wrong frontmatter returns to HEAD."""
    vault_root = tmp_path / "vault"
    store = _job_store(tmp_path)
    target = _target_at(vault_root)
    page = target.learnings_dir / "good-page.md"
    page.write_text(_GOOD_LEARNING, encoding="utf-8")
    git_env = clean_environ()
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@e.com"],
        ["config", "user.name", "T"],
        ["add", "-A"],
        ["commit", "-qm", "seed"],
    ):
        subprocess.run(["git", *args], cwd=vault_root, check=True, capture_output=True, env=git_env)
    sweep = Sweep(
        store,
        SessionMemoryConfig(),
        _WritingRunner(
            lambda: page.write_text("---\nname: x\nmetadata:\n  a: b\n---\nNew\n", encoding="utf-8")
        ),
        _FakeVault(target, root=vault_root),
    )

    notes = sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id="sess-auto"))

    assert page.read_text(encoding="utf-8") == _GOOD_LEARNING
    assert f"malformed-restored: {page.resolve()} (unexpected keys: name, metadata)" in notes


def test_modified_local_md_is_restored(tmp_path: Path) -> None:
    """Verify an edit to the person's CLAUDE.local.md returns to HEAD."""
    vault_root = tmp_path / "vault"
    store = _job_store(tmp_path)
    target = _target_at(vault_root)
    local_md = target.project_dir / "CLAUDE.local.md"
    local_md.write_text("private notes\n", encoding="utf-8")
    _seed_git(vault_root)
    sweep = Sweep(
        store,
        SessionMemoryConfig(),
        _WritingRunner(lambda: local_md.write_text("rewritten\n", encoding="utf-8")),
        _FakeVault(target, root=vault_root),
    )

    notes = sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id="sess-local"))

    assert local_md.read_text(encoding="utf-8") == "private notes\n"
    message = "CLAUDE.local.md is the person's, not the sweep's"
    saved = list((store.state_dir / "quarantine").glob("*-CLAUDE.local.md"))
    assert len(saved) == 1
    assert saved[0].read_text(encoding="utf-8") == "rewritten\n"
    expected = (
        f"malformed-restored: {local_md.resolve()} ({message}; displaced copy saved to {saved[0]})"
    )
    assert expected in notes


def test_uncommitted_local_md_edit_survives_a_run_that_changes_it(tmp_path: Path) -> None:
    """Verify the restore returns the pre-run bytes, not HEAD, when the person's edit is uncommitted."""
    vault_root = tmp_path / "vault"
    store = _job_store(tmp_path)
    target = _target_at(vault_root)
    local_md = target.project_dir / "CLAUDE.local.md"
    local_md.write_text("committed\n", encoding="utf-8")
    _seed_git(vault_root)
    local_md.write_text("uncommitted edit\n", encoding="utf-8")
    sweep = Sweep(
        store,
        SessionMemoryConfig(),
        _WritingRunner(lambda: local_md.write_text("rewritten\n", encoding="utf-8")),
        _FakeVault(target, root=vault_root),
    )

    notes = sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id="sess-new-local"))

    assert local_md.read_text(encoding="utf-8") == "uncommitted edit\n"
    saved = list((store.state_dir / "quarantine").glob("*-CLAUDE.local.md"))
    assert len(saved) == 1
    assert saved[0].read_text(encoding="utf-8") == "rewritten\n"
    assert any(n.startswith("malformed-restored: ") and str(saved[0]) in n for n in notes)


def test_local_md_deleted_during_a_run_returns_with_its_pre_run_bytes(tmp_path: Path) -> None:
    """Verify a deleted CLAUDE.local.md comes back with uncommitted pre-run content."""
    vault_root = tmp_path / "vault"
    store = _job_store(tmp_path)
    target = _target_at(vault_root)
    local_md = target.project_dir / "CLAUDE.local.md"
    local_md.write_text("committed\n", encoding="utf-8")
    _seed_git(vault_root)
    local_md.write_text("uncommitted edit\n", encoding="utf-8")
    sweep = Sweep(
        store,
        SessionMemoryConfig(),
        _WritingRunner(local_md.unlink),
        _FakeVault(target, root=vault_root),
    )

    notes = sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id="sess-del-pre"))

    assert local_md.read_text(encoding="utf-8") == "uncommitted edit\n"
    assert any(n.startswith("deleted-restored: ") for n in notes)


def test_deleted_local_md_is_restored(tmp_path: Path) -> None:
    """Verify a CLAUDE.local.md the run deletes comes back from HEAD."""
    vault_root = tmp_path / "vault"
    store = _job_store(tmp_path)
    target = _target_at(vault_root)
    local_md = target.project_dir / "CLAUDE.local.md"
    local_md.write_text("private notes\n", encoding="utf-8")
    _seed_git(vault_root)
    sweep = Sweep(
        store,
        SessionMemoryConfig(),
        _WritingRunner(local_md.unlink),
        _FakeVault(target, root=vault_root),
    )

    notes = sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id="sess-del-local"))

    assert local_md.read_text(encoding="utf-8") == "private notes\n"
    message = "CLAUDE.local.md is the person's, not the sweep's"
    assert any(n.startswith("deleted-restored: ") and message in n for n in notes)


def _seed_git(root: Path) -> None:
    """Commit everything under `root` into a fresh repository, ignored files included."""
    git_env = clean_environ()
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@e.com"],
        ["config", "user.name", "T"],
        ["add", "-A", "-f"],
        ["commit", "-qm", "seed"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, env=git_env)


def _write_unreadable(path: Path, text: str) -> None:
    """Write `text` to `path`, then take away every permission so no read succeeds."""
    path.write_text(text, encoding="utf-8")
    path.chmod(0)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a file whatever its mode")
def test_run_job_quarantines_an_unreadable_new_learning(tmp_path: Path) -> None:
    """Verify a learning the run created that cannot be read is set aside, not left to block git."""
    # Given a run that writes a learning nobody can read
    store = _job_store(tmp_path)
    target = _target_at(tmp_path / "vault")
    page = target.learnings_dir / "locked-page.md"
    sweep = Sweep(
        store,
        SessionMemoryConfig(),
        _WritingRunner(lambda: _write_unreadable(page, _GOOD_LEARNING)),
        _FakeVault(target),
    )

    # When running the job
    notes = sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id="sess-locked"))

    # Then the page is quarantined and noted as unreadable
    assert not page.exists()
    assert f"unreadable-quarantined: {page.resolve()}" in notes
    assert len(list((store.state_dir / "quarantine").glob("*-locked-page.md"))) == 1


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a file whatever its mode")
def test_run_job_quarantines_an_unreadable_modified_learning(tmp_path: Path) -> None:
    """Verify a committed learning the run left unreadable is quarantined rather than restored."""
    # Given a committed learning that the run rewrites and leaves unreadable
    vault_root = tmp_path / "vault"
    store = _job_store(tmp_path)
    target = _target_at(vault_root)
    page = target.learnings_dir / "good-page.md"
    page.write_text(_GOOD_LEARNING, encoding="utf-8")
    _seed_git(vault_root)
    sweep = Sweep(
        store,
        SessionMemoryConfig(),
        _WritingRunner(lambda: _write_unreadable(page, _GOOD_LEARNING + "More\n")),
        _FakeVault(target, root=vault_root),
    )

    # When running the job
    notes = sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id="sess-locked"))

    # Then the unknown content is moved aside, not overwritten from HEAD
    assert not page.exists()
    assert f"unreadable-quarantined: {page.resolve()}" in notes
    assert not any(n.startswith("malformed-restored") for n in notes)
    assert len(list((store.state_dir / "quarantine").glob("*-good-page.md"))) == 1


def test_enforce_shapes_quarantines_a_learning_the_snapshot_could_not_read(
    tmp_path: Path,
) -> None:
    """Verify a learning the after-snapshot recorded as unreadable is quarantined."""
    # Given a learning the snapshot could not read, though it reads now
    store = _job_store(tmp_path)
    target = _target_at(tmp_path / "vault")
    page = target.learnings_dir / "flaky-page.md"
    page.write_text(_GOOD_LEARNING, encoding="utf-8")
    resolved = page.resolve()
    sweep = Sweep(store, SessionMemoryConfig(), _RecordingRunner(), _FakeVault(target))

    # When enforcing shapes
    notes = sweep._enforce_shapes(Changes(created=(resolved,), unreadable=(resolved,)), target)

    # Then it is quarantined as unreadable
    assert notes == [f"unreadable-quarantined: {resolved}"]
    assert not page.exists()


def test_run_job_quarantines_a_stray_top_level_file(tmp_path: Path) -> None:
    """Verify a file written directly in the project folder is set aside."""
    store = _job_store(tmp_path)
    target = _target_at(tmp_path / "vault")
    stray = target.project_dir / "MEMORY.md"
    sweep = Sweep(
        store,
        SessionMemoryConfig(),
        _WritingRunner(lambda: stray.write_text("index", encoding="utf-8")),
        _FakeVault(target),
    )

    notes = sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id="sess-stray"))

    assert not stray.exists()
    assert f"malformed-quarantined: {stray.resolve()} (stray top-level file)" in notes


@pytest.mark.parametrize(
    "relative",
    [
        "learnings/.DS_Store",
        "logs/2026-10-13-09-41.md~",
        "Thumbs.db",
        "learnings/page.sync-conflict-20260101-120000-ABCDEFG.md",
    ],
)
def test_run_job_leaves_debris_alone(tmp_path: Path, relative: str) -> None:
    """Verify sync, editor, and OS debris is never quarantined as a malformed page."""
    store = _job_store(tmp_path)
    target = _target_at(tmp_path / "vault")
    debris = target.project_dir / relative
    sweep = Sweep(
        store,
        SessionMemoryConfig(),
        _WritingRunner(lambda: debris.write_text("x", encoding="utf-8")),
        _FakeVault(target),
    )

    notes = sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id="sess-debris"))

    assert debris.exists()
    assert not any(n.startswith("malformed") for n in notes)


@pytest.mark.parametrize("field_name", ["learnings", "logs"])
def test_run_job_quarantines_a_page_nested_in_a_field(tmp_path: Path, field_name: str) -> None:
    """Verify a new file in a field's sub-directory is set aside, since no reader sees it."""
    store = _job_store(tmp_path)
    target = _target_at(tmp_path / "vault")
    nested = target.project_dir / field_name / "sub" / "page.md"

    def _write() -> None:
        nested.parent.mkdir()
        nested.write_text(_GOOD_LEARNING, encoding="utf-8")

    sweep = Sweep(store, SessionMemoryConfig(), _WritingRunner(_write), _FakeVault(target))

    notes = sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id="sess-nest"))

    assert not nested.exists()
    assert f"malformed-quarantined: {nested.resolve()} (nested in a field)" in notes


def test_local_md_restore_that_raises_names_the_saved_copy(tmp_path: Path, monkeypatch) -> None:
    """Verify a restore that fails to run keeps the file and names the copy saved aside."""
    vault_root = tmp_path / "vault"
    store = _job_store(tmp_path)
    target = _target_at(vault_root)
    local_md = target.project_dir / "CLAUDE.local.md"
    local_md.write_text("private notes\n", encoding="utf-8")
    _seed_git(vault_root)
    sweep = Sweep(
        store,
        SessionMemoryConfig(),
        _WritingRunner(lambda: local_md.write_text("edited\n", encoding="utf-8")),
        _FakeVault(target, root=vault_root),
    )

    def failing_write(_path: Path, _data: bytes) -> None:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr(sweep, "_write_atomic", failing_write)

    notes = sweep._run_job(SweepJob(window=[], cwd=str(tmp_path), session_id="sess-hung"))

    saved = list((store.state_dir / "quarantine").glob("*-CLAUDE.local.md"))
    assert len(saved) == 1
    assert saved[0].read_text(encoding="utf-8") == "edited\n"
    assert local_md.read_text(encoding="utf-8") == "edited\n"
    note = next(n for n in notes if n.startswith("malformed-unremovable: "))
    assert str(saved[0]) in note
