"""The end-of-session memory sweep: gate, detach, run, validate, commit.

The gate runs inline in the SessionEnd/PreCompact hook: it resolves the transcript,
windows it since the last compaction, drops system/hook noise, and yields a SweepJob
only when the session clears every floor in `_shortfall`. The heavy `claude -p` pass
runs in a double-forked daemon that outlives session teardown. The daemon takes a
per-project `flock` before it runs, so a second sweep for the same project queues
behind the first rather than being dropped, and gives up after `QUEUE_WAIT`.

The agent may write anywhere inside this project's vault folder and nowhere else. A
tool-reported write outside it is reverted or quarantined. The agent keeps Bash for
the whole run, and a write made through it never appears in the tool-use stream, so
the project folder is hashed before and after the run and every file whose hash
changed is scrubbed for secrets as well. A Bash write outside the project folder is
caught by nothing here. Every boundary fails open so a sweep never wedges the session.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from sessionhooks import log, shape, snapshot, transcript
from sessionhooks.config import SessionMemoryConfig
from sessionhooks.paths import is_within_root
from sessionhooks.runner import LOG_TITLE_ENV, ClaudeRunner
from sessionhooks.safety import scrub
from sessionhooks.store import Store, git_safe_env
from sessionhooks.vaultcli import ROOT_ENV, WORKER_CLI_TIMEOUT, VaultCLI

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from sessionhooks.runner import Runner, RunResult
    from sessionhooks.snapshot import Changes

_log = log.logger("sweep")

# A sweep queued behind another polls this often and gives up after QUEUE_WAIT, so
# a worker wedged past its own timeout cannot hold a queue of sweeps forever.
QUEUE_POLL = 5.0
QUEUE_WAIT = 3600.0
_GIT_TIMEOUT = 10
# How much of a failed run's stderr the log keeps; enough to name the error, short
# enough that a runaway traceback never dominates the file.
STDERR_TAIL_CHARS = 500
# The model emits this when it judges the session worth no memory at all. It is
# a diagnostic only: nothing reads it back, and a model that forgets it simply
# leaves a sweep that recorded nothing looking like any other quiet sweep.
NOTHING_TO_RECORD = "SESSIONMEMORY-NOTHING-TO-RECORD"
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "sweep.md"
CRITERIA_PATH = Path(__file__).resolve().parent.parent / "prompts" / "_capture-criteria.md"


@dataclass(slots=True)
class SweepJob:
    """A gated, ready-to-run sweep over one session's windowed transcript."""

    window: list[dict[str, Any]]
    cwd: str
    session_id: str
    transcript_path: str = ""
    session_url: str = ""
    started: str = ""


@dataclass(frozen=True, slots=True)
class Target:
    """Where in the vault one project's sweep may write."""

    project_dir: Path
    learnings_dir: Path
    backlog_path: Path
    logs_dir: Path

    REQUIRED: ClassVar[tuple[str, ...]] = ("project_dir", "learnings", "backlog", "logs")

    @classmethod
    def from_paths(cls, payload: dict[str, str]) -> Target | None:
        """Build a target from a flattened `sessionmemory project` payload, or None when incomplete."""
        if not all(payload.get(k) for k in cls.REQUIRED):
            return None
        return cls(
            project_dir=Path(payload["project_dir"]),
            learnings_dir=Path(payload["learnings"]),
            backlog_path=Path(payload["backlog"]),
            logs_dir=Path(payload["logs"]),
        )

    @property
    def roots(self) -> tuple[Path, ...]:
        """The one directory a sweep may write into."""
        return (self.project_dir,)


class Lock:
    """A per-project single-worker lock held as a kernel `flock`; every operation fails open.

    Two invariants make it safe without any stale-lock recovery. The kernel drops
    the lock when the holding process exits, however it exits, so a crashed worker
    never wedges the next one. The file is never deleted: a worker that unlinked it
    while another waited would let a third lock a fresh file at the same path, and
    two workers would then each hold "the" lock.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> bool:
        """Take the lock without blocking, returning False when another holder has it."""
        if self._fd is not None:
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        """Drop the lock if this object holds it; never raises and never unlinks the file."""
        fd, self._fd = self._fd, None
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)


def in_progress(store: Store) -> bool:
    """Report whether a sweep worker holds this project's lock right now."""
    if not store.lock_path.exists():
        return False
    probe = Lock(store.lock_path)
    if probe.acquire():
        probe.release()
        return False
    return True


def _transcript_text(window: list[dict[str, Any]]) -> str:
    """Serialize the window's real conversation (role + text only) for the prompt.

    Only the human's messages and the agent's user-facing text are sent; the
    agent's thinking, its tool calls/results, and system/hook noise are dropped
    by `transcript.meaningful_text` since none of them are durable memory.
    """
    return json.dumps(transcript.meaningful_text(window), ensure_ascii=False)


def _git_context(cwd: str, *, timeout: int = 10) -> str:
    """Return recent commit subjects (project repo ground truth), or '' on failure."""
    try:
        proc = subprocess.run(
            ["git", "log", "--format=%h %s (%cr)", "-20"],  # noqa: S607
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            # Strip leaked git-location vars so `git log` reads the repo at `cwd`,
            # not whatever repo an ambient GIT_DIR names.
            env=git_safe_env(os.environ),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _log_moment(started: str) -> str:
    """When a session began, as local `YYYY-MM-DD HH:MM`: the whole of a log's title.

    The log already sits in its project's folder, so the title carries nothing but the
    moment, and the clock time is what keeps two sessions on one day from sharing a
    title and a filename. It is the session's start rather than the sweep's, so a
    second sweep of the same session renders the same title. A start that is missing
    or unreadable falls back to now, since a log titled for the wrong minute beats no
    log.
    """
    try:
        moment = datetime.fromisoformat(started) if started else datetime.now(UTC)
    except ValueError:
        moment = datetime.now(UTC)
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


def _render_template(path: Path, **variables: str) -> str:
    """Read a template file and substitute {{name}} placeholders."""
    content = path.read_text(encoding="utf-8")
    for key, value in variables.items():
        content = content.replace("{{" + key + "}}", value)
    return content


# Matches a leading `---`-delimited YAML block: the vault CLI's frontmatter shape.
_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n?", re.DOTALL)


def _strip_frontmatter(text: str) -> str:
    """Return `text` with a leading frontmatter block removed, body only."""
    return _FRONTMATTER_RE.sub("", text, count=1).lstrip("\n")


class Sweep:
    """The boundary-capture pipeline: gate -> detach -> run -> validate."""

    def __init__(
        self,
        store: Store,
        config: SessionMemoryConfig,
        runner: Runner,
        vault: VaultCLI | None = None,
        *,
        env: Mapping[str, str] | None = None,
        vault_root: Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.store = store
        self.config = config
        self.runner = runner
        self.vault = vault
        # Choosing a CLI runs a version handshake, which the worker pays after it
        # detaches rather than the hook paying it inside Claude Code's budget.
        self.vault_root = vault_root
        # The env run_sweep resolved the vault/store from, not a fresh read of
        # os.environ: a vault found via `[vault] root` in the config is invisible
        # to the environment a second, independent read would see.
        self.env = env if env is not None else os.environ
        self.sleep = sleep

    def trigger(self, event: dict[str, Any]) -> None:
        """Gate the sweep and, if worthwhile, spawn the detached worker. Never raises."""
        with contextlib.suppress(Exception):
            job = self._gate(event)
            if job is not None:
                self._spawn_detached(job)

    def _shortfall(self, window: list[dict[str, Any]]) -> str | None:
        """Why a session is not worth any memory, or None when it clears every floor.

        Governs the whole sweep, not just the log: a session that falls short writes
        no learning, no backlog item, and no log, and never spawns the model pass.
        Two floors on the human's own messages, because a single instruction
        answered at length clears a combined message count. Every measure is
        reported against its floor so a skip explains itself.
        """
        meaningful = transcript.meaningful_messages(window)
        substance = transcript.user_substance(meaningful)
        counts = (
            f"exchanges {len(meaningful)}/{self.config.min_exchanges}, "
            f"user messages {substance.messages}/{self.config.min_user_messages}, "
            f"user chars {substance.characters}/{self.config.min_user_chars}"
        )
        short = (
            len(meaningful) < self.config.min_exchanges
            or substance.messages < self.config.min_user_messages
            or substance.characters < self.config.min_user_chars
        )
        return f"below threshold ({counts})" if short else None

    def _gate(self, event: dict[str, Any]) -> SweepJob | None:
        """Return a SweepJob, or None when the session is not worth sweeping.

        Takes no lock: a sweep already running for this project only delays this
        one, and the detached worker waits for it in `_await_lock`. Never raises.
        Every None is logged with its reason, since a gate that declines silently
        is indistinguishable from a hook that never ran.
        """
        try:
            transcript_path = event.get("transcript_path") or self.store.read_transcript_pointer()
            if not transcript_path:
                _log.info("sweep skipped: no transcript")
                return None
            entries = transcript.read_entries(transcript_path)
            window = transcript.window_since_compact(entries)
            shortfall = self._shortfall(window)
            if shortfall is not None:
                _log.info("sweep skipped: %s", shortfall)
                return None
            session_id = str(event.get("session_id") or "") or Path(transcript_path).stem
            return SweepJob(
                window=window,
                cwd=str(event.get("cwd") or Path.cwd()),
                session_id=session_id,
                transcript_path=str(transcript_path),
                # Read from every entry rather than the window: the bridge entry is
                # written at the top of the transcript, before any compaction.
                session_url=transcript.session_url(entries),
                started=transcript.session_start(entries),
            )
        except Exception:  # noqa: BLE001 - gate must never raise
            _log.exception("sweep gate failed")
            return None

    def _await_lock(self, lock: Lock) -> bool:
        """Wait for the project's lock, returning False once QUEUE_WAIT has passed.

        Counts polls rather than reading a clock, so the wait is the sum of the
        sleeps asked for and a test can drive it without real time passing.
        """
        if lock.acquire():
            return True
        _log.info("sweep queued: another sweep holds the lock")
        polls = 0
        while polls * QUEUE_POLL < QUEUE_WAIT:
            self.sleep(QUEUE_POLL)
            polls += 1
            if lock.acquire():
                _log.info("sweep dequeued after about %.0fs", polls * QUEUE_POLL)
                return True
        _log.warning("sweep skipped: lock still held after %.0fs", polls * QUEUE_POLL)
        return False

    def _prepare(self, job: SweepJob) -> tuple[Target, str] | str:
        """Resolve where to write and compose this session's log command, or the skip reason.

        The command is built here rather than run here: a session the model
        judges empty must leave no log behind, and a file scaffolded before the
        model runs cannot be un-created without deleting a note. Every argument
        is resolved so the model only runs the command, never composes one, and
        the CLI still owns the filename, the frontmatter, and `## Changes`.
        """
        if self.vault is None:
            return "no CLI: neither PATH nor the plugin shim"
        lookup = self.vault.project_lookup(
            cwd=Path(job.cwd), env=dict(self.env), timeout=WORKER_CLI_TIMEOUT
        )
        if lookup.paths is None:
            return lookup.reason
        target = Target.from_paths(lookup.paths)
        if target is None:
            missing = next(k for k in Target.REQUIRED if not lookup.paths.get(k))
            return f"project payload lacked {missing}"
        return target, self._log_command(job)

    def _log_command(self, job: SweepJob) -> str:
        """Build the `sessionmemory log` command line, complete except for the body.

        The body is the one thing the model composes rather than runs, because the
        summary does not exist until it writes one, and it arrives on stdin through
        `--body-file -` so its quoting cannot be mangled. Everything that decides
        placement, filename, and frontmatter stays resolved here. The title is not on
        the line: the hook resolves it and hands it over in the environment, because a
        model asked to type it rewrites it.
        """
        args = [
            self.vault.command if self.vault else "",
            "log",
            "--session-id",
            job.session_id,
            "--cwd",
            job.cwd,
        ]
        if job.transcript_path:
            args += ["--transcript", job.transcript_path]
        if job.session_url:
            args += ["--url", job.session_url]
        return shlex.join(args)

    def _changes(self, job: SweepJob) -> str:
        """Commit subjects since the session started, or '' when there is no base."""
        base = self.store.read_base_commit()
        if not base:
            return ""
        try:
            proc = subprocess.run(  # noqa: S603
                ["git", "log", "--format=%h %s", f"{base}..HEAD"],  # noqa: S607
                cwd=job.cwd,
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT,
                check=False,
                env=git_safe_env(os.environ),
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def _existing_log(self, target: Target, session_id: str) -> str:
        """This session's log body, with no frontmatter, or '' when it has none yet.

        `sessionmemory log` replaces the body on every call, so a second sweep of one
        session has to resend everything the first sweep wrote. The matching
        `session_id:` line is matched literally rather than parsed as YAML: the
        hooks carry no dependencies, and the CLI is the only writer of this
        field. The frontmatter block itself is stripped before returning, since
        this text is interpolated straight into a template the model is told to
        carry forward into the new body, and frontmatter carried into a note's body
        corrupts it on the next `sessionmemory log`.
        """
        if not session_id:
            return ""
        try:
            candidates = sorted(target.logs_dir.glob("*.md"))
        except OSError:
            return ""
        needle = f"session_id: {session_id}"
        for path in candidates:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if needle in text:
                return _strip_frontmatter(text)
        return ""

    def _run_job(self, job: SweepJob) -> list[str]:
        """Take the lock, build the prompt, run the agent, validate its writes, log.

        Waits behind any sweep already running for this project, and returns no
        notes when that wait runs out. Releases the lock it took and never raises
        (it runs in a detached worker with no one to catch it). Returns the
        remediation notes.
        """
        lock = Lock(self.store.lock_path)
        log.bind(step="sweep", session=job.session_id)
        if not self._await_lock(lock):
            return []
        try:
            if self.vault is None and self.vault_root is not None:
                self.vault = VaultCLI.for_root(
                    self.vault_root, env=self.env, version_timeout=WORKER_CLI_TIMEOUT
                )
            prepared = self._prepare(job)
            if isinstance(prepared, str):
                _log.info("sweep skipped: %s", prepared)
                return []
            target, log_command = prepared
            # A project registered on another machine has no folder until its first
            # page is written, and the model runs inside the folder.
            target.project_dir.mkdir(parents=True, exist_ok=True)
            started_at = time.time()
            prompt = _render_template(
                PROMPT_PATH,
                transcript=_transcript_text(job.window),
                existing_memory=self._existing_memory(target),
                git_context=_git_context(job.cwd),
                capture_criteria=CRITERIA_PATH.read_text(encoding="utf-8"),
                vault_cli=self.vault.command if self.vault else "",
                repo=job.cwd,
                log_command=log_command,
                nothing_sentinel=NOTHING_TO_RECORD,
                changes=self._changes(job),
                existing_log=self._existing_log(target, job.session_id),
            )
            before = snapshot.take(target.project_dir)
            result = self.runner.run(
                prompt,
                cwd=str(target.project_dir),
                extra_env={LOG_TITLE_ENV: _log_moment(job.started)},
            )
            changes = snapshot.diff(before, snapshot.take(target.project_dir))
            _log.debug("claude ran %.1fs", time.time() - started_at)
            notes = self._validate_writes(result.changed_files, target, changes)
            notes.extend(self._enforce_shapes(changes, target))
            # The sentinel also appears in the transcript the agent summarizes, so
            # an actual write outranks it: a run that wrote something did record.
            if NOTHING_TO_RECORD in result.text and not changes.touched() and not changes.deleted:
                notes.append("nothing to record")
            outcome = self.vault.commit(env=self.env) if self.vault else None
            commit_clause = outcome.describe() if outcome else "commit skipped: no vault"
            self._log_result(result, changes=changes, commit=commit_clause, notes=notes)
        except Exception:  # noqa: BLE001 - the detached worker must never raise
            _log.exception("sweep worker failed")
            return []
        else:
            return [*notes, commit_clause]
        finally:
            lock.release()

    def _log_result(
        self, result: RunResult, *, changes: Changes, commit: str, notes: list[str]
    ) -> None:
        """One line for the run: what it wrote and how the commit went, or why it failed."""
        if result.success:
            suffix = f" (notes: {'; '.join(notes)})" if notes else ""
            _log.info("sweep finished: ok, %s, %s%s", changes.describe(), commit, suffix)
            return
        tail = (result.stderr or "").strip()[-STDERR_TAIL_CHARS:]
        _log.error("sweep failed: claude exited %s (%s)", result.exit_code, tail or "no stderr")

    def _quarantine(self, path: Path) -> None:
        """Move `path` into this project's quarantine directory rather than deleting it.

        Nothing outside the write roots is snapshotted before a run, so a write
        git cannot restore from HEAD might still be a note an earlier, not-yet-
        committed sweep created, or content that predates this run entirely.
        Moving it aside keeps it recoverable instead of guessing it was safe to
        erase. The random prefix keeps two quarantined files of the same name
        from colliding.
        """
        quarantine_dir = self.store.state_dir / "quarantine"
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        dest = quarantine_dir / f"{uuid.uuid4().hex}-{path.name}"
        shutil.move(str(path), str(dest))

    def _revert(self, path: Path) -> str:
        """Undo one escaped write and report how it was undone.

        A path the vault repository tracks is restored from HEAD, because the
        agent edited a note that already existed and deleting it would turn an
        unauthorized edit into permanent data loss. When git cannot restore it,
        there is no snapshot to check it against either, since it sits outside
        every write root: rather than assume it is safe to erase, it is
        quarantined. A path that never existed in the first place has nothing to
        lose, so that case alone reports `escaped` with no file left behind.
        """
        if self._git_restore(path):
            return "escaped-reverted"
        if not path.exists():
            return "escaped"
        self._quarantine(path)
        return "escaped-quarantined"

    def _git_restore(self, path: Path) -> bool:
        """Check `path` out of the vault's HEAD; True when git restored it."""
        if self.vault is None:
            return False
        restore = subprocess.run(  # noqa: S603
            ["git", "-C", str(self.vault.root), "checkout", "HEAD", "--", str(path)],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            check=False,
            env=git_safe_env(os.environ),
        )
        return restore.returncode == 0

    def _restore_or_quarantine(self, path: Path) -> str:
        """Return `path` to its committed content, or move it aside when git has none."""
        if self._git_restore(path):
            return "restored"
        self._quarantine(path)
        return "quarantined"

    def _enforce_shapes(self, changes: Changes, target: Target) -> list[str]:
        """Set aside each written page that lacks the shape its folder requires.

        A new file that fails is quarantined. A modified learning that fails is
        restored from HEAD, so a good page the model rewrote into another format is
        not lost; only learnings are checked on edit, since a log or backlog edit
        keeps its name and has no frontmatter to break.
        """
        notes: list[str] = []
        judged = [(path, True) for path in changes.created]
        judged += [(path, False) for path in changes.modified]
        for path, is_new in judged:
            if not path.exists():
                continue
            parent = path.parent
            if parent == target.learnings_dir.resolve():
                problem = shape.learning_problem(path)
            elif parent == target.logs_dir.resolve() and is_new:
                problem = shape.log_problem(path)
            elif is_new:
                problem = shape.stray_problem(path, target.project_dir)
            else:
                problem = None
            if problem is None:
                continue
            try:
                if is_new:
                    self._quarantine(path)
                    outcome = "quarantined"
                else:
                    outcome = self._restore_or_quarantine(path)
            except (OSError, subprocess.SubprocessError) as exc:
                notes.append(f"malformed-unremovable: {path} ({problem}; {exc})")
                continue
            notes.append(f"malformed-{outcome}: {path} ({problem})")
        return notes

    def _revert_and_note(self, path: Path, raw: str) -> str:
        """Revert one out-of-root write and phrase the outcome as a note; never raises."""
        try:
            outcome = self._revert(path)
        except (OSError, subprocess.SubprocessError) as exc:
            return f"escaped-unremovable: {raw} ({exc})"
        return f"{outcome}: {raw}"

    def _scrub_path(self, path: Path, raw: str) -> str | None:
        """Redact any secret-shaped content at `path` in place; a note, or None when clean.

        `raw` is what the note names, so a diff-derived path (already resolved
        and absolute) and a tool-reported one (possibly relative, possibly
        still carrying a symlink) both read the same in the hooks log.
        """
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None  # absent, unreadable, or binary: nothing to scan
        scrubbed, changed = scrub(content)
        if not changed:
            return None
        try:
            path.write_text(scrubbed, encoding="utf-8")
        except OSError as exc:
            return f"secret-found-unredactable: {raw} ({exc})"
        return f"secret-redacted: {raw}"

    def _judge_write(
        self, path: Path, raw: str, target: Target, *, seen: set[Path], notes: list[str]
    ) -> None:
        """Apply containment and the secret scrub to one path, once."""
        resolved = path.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        if not any(is_within_root(path, root) for root in target.roots):
            notes.append(self._revert_and_note(path, raw))
            return
        note = self._scrub_path(path, raw)
        if note is not None:
            notes.append(note)

    def _validate_writes(
        self, changed_files: list[str], target: Target, changes: Changes
    ) -> list[str]:
        """Enforce containment on tool-reported writes and scrub every file the run changed.

        Bash never reports a path, so the second pass scrubs every file under the project
        folder whose content hash changed during the run, and names each deletion. A
        relative tool-reported path is anchored to the directory the agent ran in.
        """
        notes: list[str] = []
        seen: set[Path] = set()
        for raw in changed_files:
            path = Path(raw)
            if not path.is_absolute():
                path = target.project_dir / path
            self._judge_write(path, raw, target, seen=seen, notes=notes)
        for path in changes.touched():
            self._judge_write(path, str(path), target, seen=seen, notes=notes)
        notes.extend(f"deleted: {path}" for path in changes.deleted)
        return notes

    def _existing_memory(self, target: Target, *, max_chars: int = 50_000) -> str:
        """Concatenate this project's backlog and learnings so the agent can dedup and refine."""
        parts: list[str] = []
        with contextlib.suppress(OSError):
            parts.append(f"# backlog.md\n{target.backlog_path.read_text(encoding='utf-8')}")
        if target.learnings_dir.is_dir():
            for f in sorted(target.learnings_dir.glob("*.md")):
                with contextlib.suppress(OSError):
                    parts.append(f"# learnings/{f.name}\n{f.read_text(encoding='utf-8')}")
        return "\n\n".join(parts)[:max_chars]

    def _redirect_stdio(self) -> None:
        """Point the daemon's stdio at sweep.out so it never touches the hook's stdout."""
        try:
            self.store.state_dir.mkdir(parents=True, exist_ok=True)
            out = os.open(
                str(self.store.state_dir / "sweep.out"),
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            )
            null = os.open(os.devnull, os.O_RDONLY)
            os.dup2(null, 0)
            os.dup2(out, 1)
            os.dup2(out, 2)
        except OSError:
            pass

    def _spawn_detached(self, job: SweepJob) -> None:
        """Daemonize the sweep so it outlives session teardown.

        Double-fork + setsid detaches the worker into its own session; the parent
        (the hook) reaps the intermediate child and returns immediately, so the
        hook that triggered this never blocks on the sweep. The worker redirects
        stdio away from the hook's stdout before running the job, since a
        detached process still sharing the hook's stdout would corrupt whatever
        the hook itself writes there.
        """
        try:
            pid = os.fork()
        except OSError:
            _log.info("sweep skipped: fork failed")
            return  # cannot fork; skip the sweep rather than block the hook
        if pid > 0:
            with contextlib.suppress(OSError):
                os.waitpid(pid, 0)  # reap the intermediate child (grandchild reparents to init)
            _log.info("sweep spawned")
            return
        # intermediate child
        try:
            os.setsid()
            pid2 = os.fork()
        except OSError:
            _log.info("sweep skipped: fork failed")
            os._exit(0)
        if pid2 > 0:
            os._exit(0)
        # grandchild = the daemon
        self._redirect_stdio()
        try:
            self._run_job(job)
        except BaseException:  # noqa: BLE001 - no caller to report to  # pragma: no cover
            _log.exception("sweep worker crashed")
        os._exit(0)


def run_sweep(
    event: dict[str, Any],
    *,
    env: Mapping[str, str],
    store: Store | None = None,
    config: SessionMemoryConfig | None = None,
) -> None:
    """Build the store/config/runner from the event and trigger the sweep. Never raises.

    The single wiring point both `sessionend.py` and `precompact.py` call, so the
    thin scripts don't duplicate construction.

    `store` lets a caller that already resolved one for its own log line pass it
    straight through: `Store.for_cwd` runs a git call, and a hook that already paid
    for it once inside a tight timeout budget should not pay for it again. `config`
    does the same for a caller that already loaded one through `hookmain.begin`.
    """
    cwd = Path(event.get("cwd") or Path.cwd())
    if store is None:
        store = Store.for_cwd(cwd=cwd, env=env)
    if config is None:
        config = SessionMemoryConfig.load(project_dir=env.get("CLAUDE_PROJECT_DIR"))
    root = VaultCLI.locate(env=env, configured=config.vault_root)
    if root is None:
        _log.info("sweep skipped: no vault")
        return
    # The sweep agent runs the vault CLI itself, and the CLI reads its root only
    # from the environment. Pin the root this process resolved, or a vault found
    # via `[vault] root` in the config is invisible to every command the agent runs.
    runner = ClaudeRunner(
        model=config.sweep_model,
        save_transcript=config.sweep_save_transcript,
        timeout=config.sweep_timeout,
        extra_env={ROOT_ENV: str(root)},
    )
    Sweep(store, config, runner, env=env, vault_root=root).trigger(event)
