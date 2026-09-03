"""The end-of-session memory sweep: gate, detach, run, validate, commit.

The gate runs inline in the SessionEnd/PreCompact hook: it acquires a per-project
single-writer lock, resolves the transcript, windows it since the last compaction,
drops system/hook noise, and yields a SweepJob only when the session cleared both
floors in `_is_meaningful`. The heavy `claude -p` pass runs in a double-forked daemon
that outlives session teardown.

The agent may write anywhere inside this project's vault folder and nowhere else. A
tool-reported write outside it is reverted or quarantined. The agent keeps Bash for
the whole run, and a write made through it never appears in the tool-use stream, so
after the run every file under the project folder modified since the run started is
scrubbed for secrets as well. A Bash write outside the project folder is caught by
nothing here. Every boundary fails open so a sweep never wedges the session.
"""

from __future__ import annotations

import contextlib
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
from typing import TYPE_CHECKING, Any

from sessionhooks import transcript
from sessionhooks.config import SessionMemoryConfig
from sessionhooks.paths import is_within_root
from sessionhooks.runner import ClaudeRunner
from sessionhooks.safety import scrub
from sessionhooks.store import Store, git_safe_env
from sessionhooks.vaultcli import ROOT_ENV, VaultCLI

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sessionhooks.runner import Runner

STALE_AFTER = 300.0
_GIT_TIMEOUT = 10
# How much of a failed run's stderr sweep.log keeps; enough to name the error,
# short enough that a runaway traceback never dominates the log file.
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


@dataclass(frozen=True, slots=True)
class Target:
    """Where in the vault one project's sweep may write."""

    project_dir: Path
    learnings_dir: Path
    backlog_path: Path
    logs_dir: Path

    @classmethod
    def from_paths(cls, payload: dict[str, str]) -> Target | None:
        """Build a target from a flattened `sessionmemory project` payload, or None when incomplete."""
        needed = ("project_dir", "learnings", "backlog", "logs")
        if not all(payload.get(k) for k in needed):
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
    """Atomic single-writer lock with stale recovery; every operation fails open."""

    def __init__(self, path: Path, *, stale_after: float = STALE_AFTER) -> None:
        self.path = path
        self.stale_after = stale_after

    def _try_create(self) -> int | None:
        """Open the lock with O_CREAT|O_EXCL; return the fd or None if it exists/fails."""
        try:
            return os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError:
            return None

    def acquire(self, *, now: float) -> bool:
        """Atomically claim the lock, stealing one older than `stale_after`.

        Returns True on success. A malformed or empty stored timestamp reads as
        stale (0.0) and is stolen. An `os.write` failure leaves an empty lock file
        that reads as stale next attempt and returns False. Never raises.
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
        fd = self._try_create()
        if fd is None:
            try:
                stored = float(self.path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                stored = 0.0
            if now - stored <= self.stale_after:
                return False  # held and fresh
            try:
                self.path.unlink()
            except OSError:
                return False
            fd = self._try_create()
            if fd is None:
                return False
        try:
            try:
                os.write(fd, str(now).encode("utf-8"))
            finally:
                os.close(fd)
        except OSError:
            return False
        return True

    def release(self) -> None:
        """Best-effort remove the lock; never raises."""
        with contextlib.suppress(OSError):
            self.path.unlink(missing_ok=True)


def in_progress(store: Store, *, now: float, stale_after: float = STALE_AFTER) -> bool:
    """Report whether a sweep worker holds a fresh lock for this project's store."""
    try:
        stored = float(store.lock_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return now - stored <= stale_after


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


def _today() -> str:
    """Today's date as YYYY-MM-DD in local time, for a log's title."""
    return datetime.now().astimezone().strftime("%Y-%m-%d")


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
    ) -> None:
        self.store = store
        self.config = config
        self.runner = runner
        self.vault = vault
        # The env run_sweep resolved the vault/store from, not a fresh read of
        # os.environ: a vault found via `[vault] root` in the config is invisible
        # to the environment a second, independent read would see.
        self.env = env if env is not None else os.environ

    def trigger(self, event: dict[str, Any]) -> None:
        """Gate the sweep and, if worthwhile, spawn the detached worker. Never raises."""
        with contextlib.suppress(Exception):
            job = self._gate(event, now=time.time())
            if job is not None:
                self._spawn_detached(job)

    def _is_meaningful(self, window: list[dict[str, Any]]) -> bool:
        """Report whether a session said enough to be worth any memory at all.

        Governs the whole sweep, not just the log: a session that fails here
        writes no learning, no backlog item, and no log, and never spawns the
        model pass. Two floors on the human's own messages, because a single
        instruction answered at length clears a combined message count.
        """
        meaningful = transcript.meaningful_messages(window)
        if len(meaningful) < self.config.min_exchanges:
            return False
        substance = transcript.user_substance(meaningful)
        return (
            substance.messages >= self.config.min_user_messages
            and substance.characters >= self.config.min_user_chars
        )

    def _gate(self, event: dict[str, Any], *, now: float) -> SweepJob | None:
        """Acquire the lock and return a SweepJob, or None when not worth sweeping.

        Never raises and never leaks the lock: on any post-acquire failure or a
        below-threshold window it releases the lock and returns None. On success
        the lock stays held for `_run_job` to release.
        """
        lock = Lock(self.store.lock_path)
        if not lock.acquire(now=now):
            return None
        try:
            transcript_path = event.get("transcript_path") or self.store.read_transcript_pointer()
            entries = transcript.read_entries(transcript_path) if transcript_path else []
            window = transcript.window_since_compact(entries)
            if not self._is_meaningful(window):
                lock.release()
                return None
            session_id = Path(transcript_path).stem if transcript_path else ""
            return SweepJob(
                window=window,
                cwd=str(event.get("cwd") or Path.cwd()),
                session_id=session_id,
            )
        except Exception:  # noqa: BLE001 - gate must never raise or leak the lock
            lock.release()
            return None

    def _prepare(self, job: SweepJob) -> tuple[Target, str] | None:
        """Resolve where to write and compose this session's log command, or None to skip.

        The command is built here rather than run here: a session the model
        judges empty must leave no log behind, and a file scaffolded before the
        model runs cannot be un-created without deleting a note. Every argument
        is resolved so the model only runs the command, never composes one, and
        the CLI still owns the filename, the frontmatter, and `## Changes`.
        """
        if self.vault is None:
            return None
        repo = Path(job.cwd)
        env = dict(self.env)
        paths = self.vault.project_paths(cwd=repo, env=env)
        if paths is None:
            return None  # unregistered, or the CLI could not answer
        target = Target.from_paths(paths)
        if target is None:
            return None
        return target, self._log_command(job, slug=paths.get("slug", "session"))

    def _log_command(self, job: SweepJob, *, slug: str) -> str:
        """Build the `sessionmemory log` command line, complete except for the body.

        The body is the one thing the model composes rather than runs, because the
        summary does not exist until it writes one, and it arrives on stdin through
        `--body-file -` so its quoting cannot be mangled. Everything that decides
        placement, filename, and frontmatter stays resolved here.
        """
        args = [
            self.vault.command if self.vault else "",
            "log",
            "--session-id",
            job.session_id,
            "--title",
            f"{slug} {_today()}",
            "--cwd",
            job.cwd,
        ]
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
        """Build the prompt, run the agent, validate its writes, log, free the lock.

        ALWAYS releases the lock and never raises (it runs in a detached worker
        with no one to catch it). Returns the remediation notes.
        """
        lock = Lock(self.store.lock_path)
        try:
            prepared = self._prepare(job)
            if prepared is None:
                self._log_run(session=job.session_id, ok=False, changed=[], notes=["no-vault"])
                return []
            target, log_command = prepared
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
            result = self.runner.run(prompt, cwd=str(target.project_dir))
            notes = self._validate_writes(result.changed_files, target, started_at=started_at)
            # The sentinel also appears in the transcript the agent summarizes, so
            # an actual write outranks it: a run that wrote something did record.
            if NOTHING_TO_RECORD in result.text and not result.changed_files:
                notes.append("nothing-to-record")
            sha = self.vault.commit(env=self.env) if self.vault else None
            notes.append(f"committed: {sha}" if sha else "commit-skipped")
            self._log_run(
                session=job.session_id,
                ok=result.success,
                changed=result.changed_files,
                notes=notes,
                exit_code=result.exit_code,
                stderr=result.stderr,
            )
        except Exception:  # noqa: BLE001 - the detached worker must never raise
            return []
        else:
            return notes
        finally:
            lock.release()

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
        if self.vault is not None:
            restore = subprocess.run(  # noqa: S603
                ["git", "-C", str(self.vault.root), "checkout", "HEAD", "--", str(path)],  # noqa: S607
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT,
                check=False,
                env=git_safe_env(os.environ),
            )
            if restore.returncode == 0:
                return "escaped-reverted"
        if not path.exists():
            return "escaped"
        self._quarantine(path)
        return "escaped-quarantined"

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
        still carrying a symlink) both read the same in `sweep.log`.
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

    def _touched_since(self, directory: Path, started_at: float) -> list[Path]:
        """Every file under `directory` modified at or after `started_at`; never raises."""
        try:
            return sorted(
                p for p in directory.rglob("*") if p.is_file() and p.stat().st_mtime >= started_at
            )
        except OSError:
            return []

    def _validate_writes(
        self, changed_files: list[str], target: Target, *, started_at: float
    ) -> list[str]:
        """Enforce containment on tool-reported writes and scrub every file the run touched.

        Bash never reports a path, so the second pass scrubs by mtime: anything under the
        project folder written since the run began. A relative tool-reported path is
        anchored to the directory the agent ran in.
        """
        notes: list[str] = []
        seen: set[Path] = set()
        for raw in changed_files:
            path = Path(raw)
            if not path.is_absolute():
                path = target.project_dir / path
            self._judge_write(path, raw, target, seen=seen, notes=notes)
        for path in self._touched_since(target.project_dir, started_at):
            self._judge_write(path, str(path), target, seen=seen, notes=notes)
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

    def _log_run(
        self,
        *,
        session: str,
        ok: bool,
        changed: list[str],
        notes: list[str],
        exit_code: int | None = None,
        stderr: str = "",
    ) -> None:
        """Append one line recording what this session's sweep did; best-effort.

        The session id is here because this is the only record that answers "was
        that session ever swept, and did it work". Nothing reads the line back:
        it is a diagnostic for a human asking why some conversation left no
        memory behind, so it carries the outcome as well as the writes. The exit
        code, and a tail of stderr when the run failed, are what tell a timeout
        apart from a missing `claude` binary here; both otherwise look identical
        as ok=False with no writes.
        """
        try:
            self.store.state_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).isoformat()
            line = (
                f"{stamp} session={session or 'unknown'} ok={ok} exit_code={exit_code} "
                f"changed={changed} notes={notes}"
            )
            if not ok and stderr:
                line += f" stderr={stderr[-STDERR_TAIL_CHARS:]!r}"
            with self.store.log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

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

        The gate holds the lock on behalf of the worker, and only `_run_job`
        releases it. A daemon that never reaches `_run_job` must therefore
        release it here, or the next session's sweep is refused until the lock
        goes stale.
        """
        try:
            pid = os.fork()
        except OSError:
            Lock(self.store.lock_path).release()
            return  # cannot fork; skip the sweep rather than block the hook
        if pid > 0:
            with contextlib.suppress(OSError):
                os.waitpid(pid, 0)  # reap the intermediate child (grandchild reparents to init)
            return
        # intermediate child
        try:
            os.setsid()
            pid2 = os.fork()
        except OSError:
            Lock(self.store.lock_path).release()
            os._exit(0)
        if pid2 > 0:
            os._exit(0)
        # grandchild = the daemon
        self._redirect_stdio()
        with contextlib.suppress(BaseException):
            self._run_job(job)
        os._exit(0)


def run_sweep(event: dict[str, Any], *, env: Mapping[str, str]) -> None:
    """Build the store/config/runner from the event and trigger the sweep. Never raises.

    The single wiring point both `sessionend.py` and `precompact.py` call, so the
    thin scripts don't duplicate construction.
    """
    cwd = Path(event.get("cwd") or Path.cwd())
    store = Store.for_cwd(cwd=cwd, env=env)
    config = SessionMemoryConfig.load(project_dir=env.get("CLAUDE_PROJECT_DIR"))
    vault = VaultCLI.discover(env=env, configured=config.vault_root)
    # The sweep agent runs the vault CLI itself, and the CLI reads its root only
    # from the environment. Pin the root this process resolved, or a vault found
    # via `[vault] root` in the config is invisible to every command the agent runs.
    runner = ClaudeRunner(
        model=config.sweep_model,
        save_transcript=config.sweep_save_transcript,
        extra_env={ROOT_ENV: str(vault.root)} if vault is not None else None,
    )
    Sweep(store, config, runner, vault, env=env).trigger(event)
