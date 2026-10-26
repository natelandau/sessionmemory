"""The end-of-session memory sweep: gate, detach, run, validate, commit.

The gate runs inline in the SessionEnd/PreCompact hook: it resolves the transcript,
windows it since the last compaction or finished manual sweep, drops system/hook noise, and yields a SweepJob
only when the session clears every floor in `_shortfall`. The heavy `claude -p` pass
runs in a double-forked daemon that outlives session teardown. The daemon takes a
per-project `flock` before it runs, so a second sweep for the same project queues
behind the first rather than being dropped, and gives up after `queue_wait`.

The agent may write anywhere inside this project's vault folder and nowhere else.
The folder is hashed before and after the run, and `guard.WriteGuard` checks what
changed. A Bash write outside the project folder is caught by nothing here. Every
boundary fails open so a sweep never wedges the session.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sessionhooks import log, snapshot, transcript
from sessionhooks.config import SessionMemoryConfig
from sessionhooks.guard import Target, WriteGuard
from sessionhooks.runner import LOG_TITLE_ENV, ClaudeRunner
from sessionhooks.store import LOCK_NAME, Store, git_safe_env, state_root
from sessionhooks.vaultcli import ROOT_ENV, WORKER_CLI_TIMEOUT, VaultCLI

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from sessionhooks.runner import Runner, RunResult
    from sessionhooks.snapshot import Changes

_log = log.logger("sweep")

# A sweep queued behind another polls this often and gives up after `queue_wait`, so
# a worker wedged past its own timeout cannot hold a queue of sweeps forever.
QUEUE_POLL = 5.0
QUEUE_WAIT_FLOOR = 3600.0
# How many sweeps ahead of this one the wait covers, each allowed its timeout plus
# this much for the work around the model run.
QUEUED_SWEEPS = 4
SWEEP_OVERHEAD = 120
_GIT_TIMEOUT = 10
# How much of a failed run's stderr the log keeps; enough to name the error, short
# enough that a runaway traceback never dominates the file.
STDERR_TAIL_CHARS = 500
# The head of the result text names the error claude reports; the stderr tail names a crash.
RESULT_TEXT_CHARS = 300
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


class Lock:
    """A per-project single-worker lock held as a kernel `flock`; every operation fails open.

    Two invariants make it safe without any stale-lock recovery. The kernel drops
    the lock when the holding process exits, however it exits, so a crashed worker
    never wedges the next one. The file is never deleted: a worker that unlinked it
    while another waited would let a third lock a fresh file at the same path, and
    two workers would then each hold "the" lock.

    `acquire` returns False both when another process holds the lock and when the
    lock cannot be taken at all. `error` tells the two apart: it is empty after
    contention and names the failure otherwise, since waiting on a lock file that
    cannot be opened would never end.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.error = ""
        self._fd: int | None = None

    def acquire(self) -> bool:
        """Take the lock without blocking; False when held elsewhere or unavailable."""
        self.error = ""
        if self._fd is not None:
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            self.error = str(exc)
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        except OSError as exc:
            os.close(fd)
            self.error = str(exc)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        """Drop the lock if this object holds it; never raises and never unlinks the file."""
        fd, self._fd = self._fd, None
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)


def _held(path: Path) -> bool:
    """Report whether some process holds the lock at `path` right now.

    A lock that cannot be probed reads as free: the caller is deciding whether to
    commit, and a lock file nobody can open would otherwise block every commit.
    """
    if not path.exists():
        return False
    probe = Lock(path)
    if probe.acquire():
        probe.release()
        return False
    return not probe.error


def in_progress(store: Store) -> bool:
    """Report whether a sweep worker holds this project's lock right now."""
    return _held(store.lock_path)


def any_in_progress(env: Mapping[str, str], *, except_path: Path | None = None) -> bool:
    """Report whether any project's sweep worker on this machine holds its lock.

    A vault commit stages the whole vault, so committing while any sweep runs would
    capture that sweep's writes before it scrubs and shape-checks them. `except_path`
    lets a worker ask about every sweep but its own.
    """
    try:
        locks = sorted(state_root(env).glob(f"*/{LOCK_NAME}"))
    except OSError:
        return False
    excluded = except_path.resolve() if except_path is not None else None
    return any(_held(path) for path in locks if path.resolve() != excluded)


def queue_wait(config: SessionMemoryConfig) -> float:
    """How long a queued worker waits for the lock before giving up, in seconds.

    Long enough for several sweeps ahead of it to each run to their configured
    timeout, so raising the timeout cannot turn a queued sweep into a dropped one.
    """
    return max(QUEUE_WAIT_FLOOR, float(QUEUED_SWEEPS * (config.sweep_timeout + SWEEP_OVERHEAD)))


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


def _render_template(path: Path, *, manual: bool = False, **variables: str) -> str:
    """Read a template file, keep the passages for one kind of run, and fill its placeholders.

    A `<!-- headless -->` block is for the detached worker and a `<!-- manual -->`
    block for a sweep run in the user's own session. The other kind's blocks are
    dropped and the kept blocks' marker lines removed, so the worker's prompt reads
    exactly as it would with no markers at all.
    """
    keep, drop = ("manual", "headless") if manual else ("headless", "manual")
    content = path.read_text(encoding="utf-8")
    content = re.sub(rf"<!-- {drop} -->\n.*?<!-- /{drop} -->\n", "", content, flags=re.DOTALL)
    content = re.sub(rf"<!-- /?{keep} -->\n", "", content)
    for key, value in variables.items():
        content = content.replace("{{" + key + "}}", value)
    return content


# Matches a leading `---`-delimited YAML block: the vault CLI's frontmatter shape.
_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n?", re.DOTALL)


def _strip_frontmatter(text: str) -> str:
    """Return `text` with a leading frontmatter block removed, body only."""
    return _FRONTMATTER_RE.sub("", text, count=1).lstrip("\n")


# YAML 1.1's double-quoted escapes, the full set PyYAML's emitter and reader know.
_DOUBLE_QUOTED_ESCAPES = {
    "0": "\x00",
    "a": "\x07",
    "b": "\x08",
    "t": "\t",
    "\t": "\t",
    "n": "\n",
    "v": "\x0b",
    "f": "\x0c",
    "r": "\r",
    "e": "\x1b",
    " ": " ",
    '"': '"',
    "/": "/",
    "\\": "\\",
    "N": "\x85",
    "_": "\xa0",
    "L": "\u2028",
    "P": "\u2029",
}
_ESCAPE_RE = re.compile(r"\\(x[0-9A-Fa-f]{2}|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|.)", re.DOTALL)
# Characters a one-line commit body cannot show: every control character, the two
# Unicode line separators, and the byte-order mark.
_UNSHOWABLE_RE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029\ufeff]")


def _decode_escape(match: re.Match[str]) -> str:
    """Decode one double-quoted escape, leaving an unknown or out-of-range one as written."""
    code = match[1]
    if code[0] in "xuU" and len(code) > 1:
        try:
            return chr(int(code[1:], 16))
        except ValueError:
            return match[0]
    return _DOUBLE_QUOTED_ESCAPES.get(code, match[0])


def _decode_double_quoted(value: str) -> str:
    """Decode the escapes in a double-quoted scalar's text, already folded and unquoted."""
    return _ESCAPE_RE.sub(_decode_escape, value)


def _escaped_break(line: str) -> bool:
    """Report whether a double-quoted line ends in an escaped line break: an odd backslash run."""
    return (len(line) - len(line.rstrip("\\"))) % 2 == 1


def _fold_lines(parts: list[str], *, double_quoted: bool) -> str:
    """Join a flow scalar's physical lines by YAML's line folding.

    A break between two lines reads as one space, and each empty line between them as
    one newline. In a double-quoted scalar a line ending in an escaped break joins the
    next with nothing, and an escaped space is what carries a space across it.
    """
    joined = ""
    blanks = 0
    escaped = False
    for index, raw in enumerate(parts):
        line = raw if index == 0 else raw.lstrip(" \t")
        if not line and index not in {0, len(parts) - 1}:
            blanks += 1
            continue
        if index:
            joined += "" if escaped else ("\n" * blanks or " ")
        blanks = 0
        escaped = double_quoted and _escaped_break(line)
        joined += line[:-1] if escaped else line.rstrip(" \t")
    return joined


def _fold_scalar(parts: list[str]) -> str:
    """Read a YAML scalar's physical lines as the one-line text a commit body shows.

    Handles the three styles `yaml.safe_dump` emits for a string: plain, single-quoted,
    and double-quoted with every escape. The decoded value then has each control
    character, line separator, and byte-order mark turned into a space and its
    whitespace collapsed, since the summary must stay on one line.
    """
    first = parts[0]
    if first.startswith('"'):
        value = _fold_lines(parts, double_quoted=True)
        value = _decode_double_quoted(value.removeprefix('"').removesuffix('"'))
    else:
        value = _fold_lines(parts, double_quoted=False)
        if first.startswith("'"):
            value = value.removeprefix("'").removesuffix("'").replace("''", "'")
    return " ".join(_UNSHOWABLE_RE.sub(" ", value).split())


def _log_summary(path: Path) -> str:
    """The `summary:` of a log's frontmatter, folded onto one line and unquoted, or ''.

    The CLI wraps a long summary onto indented continuation lines, so every line after
    `summary:` up to the next column-0 key belongs to it.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    block = _FRONTMATTER_RE.match(text)
    if not block:
        return ""
    # Only a newline ends a line here: a quoted scalar may hold a raw U+2028 or U+0085,
    # which `str.splitlines` would also split on.
    lines = block.group(0).split("\n")[1:]
    for index, line in enumerate(lines):
        if not line.startswith("summary:"):
            continue
        parts = [line.removeprefix("summary:").strip()]
        for continuation in lines[index + 1 :]:
            if continuation and not continuation[:1].isspace():
                break
            parts.append(continuation.strip(" \t"))
        while len(parts) > 1 and not parts[-1]:
            parts.pop()
        return _fold_scalar(parts) if parts[0] else ""
    return ""


class Sweep:
    """The boundary-capture pipeline: gate -> detach -> run -> validate."""

    def __init__(
        self,
        store: Store,
        config: SessionMemoryConfig,
        runner: Runner | None = None,
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
            window = transcript.window_since_boundary(entries)
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
        """Wait for the project's lock, returning False once `queue_wait` has passed.

        A lock that fails for any reason but contention ends the wait at once, since
        polling cannot fix a lock file that cannot be opened.

        Counts polls rather than reading a clock, so the wait is the sum of the
        sleeps asked for and a test can drive it without real time passing.
        """
        if lock.acquire():
            return True
        if lock.error:
            _log.warning("sweep skipped: lock unavailable: %s", lock.error)
            return False
        _log.info("sweep queued: another sweep holds the lock")
        polls = 0
        limit = queue_wait(self.config)
        while polls * QUEUE_POLL < limit:
            self.sleep(QUEUE_POLL)
            polls += 1
            if lock.acquire():
                _log.info("sweep dequeued after about %.0fs", polls * QUEUE_POLL)
                return True
            if lock.error:
                _log.warning("sweep skipped: lock unavailable: %s", lock.error)
                return False
        _log.warning("sweep skipped: lock still held after %.0fs", polls * QUEUE_POLL)
        return False

    def _prepare(self, job: SweepJob, *, title: str = "") -> tuple[Target, str] | str:
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
        return target, self._log_command(job, title=title)

    def compose(self, job: SweepJob, *, manual: bool = False) -> tuple[Target, str] | str:
        """Resolve where this sweep writes and render its prompt, or say why it cannot run.

        `manual` renders for a sweep run in the user's own session: the template's
        manual passages, no transcript, since that agent saw the conversation, and the
        log's title on the command line, since no environment carries it there.
        """
        prepared = self._prepare(job, title=_log_moment(job.started) if manual else "")
        if isinstance(prepared, str):
            return prepared
        target, log_command = prepared
        # A project registered on another machine has no folder until its first
        # page is written, and the model runs inside the folder.
        target.project_dir.mkdir(parents=True, exist_ok=True)
        prompt = _render_template(
            PROMPT_PATH,
            manual=manual,
            transcript="" if manual else _transcript_text(job.window),
            existing_memory=self._existing_memory(target),
            git_context=_git_context(job.cwd),
            capture_criteria=CRITERIA_PATH.read_text(encoding="utf-8"),
            vault_cli=self.vault.command if self.vault else "",
            repo=job.cwd,
            project_dir=str(target.project_dir),
            log_command=log_command,
            nothing_sentinel=NOTHING_TO_RECORD,
            changes=self._changes(job),
            existing_log=self._existing_log(target, job.session_id),
        )
        return target, prompt

    def _log_command(self, job: SweepJob, *, title: str = "") -> str:
        """Build the `sessionmemory log` command line, complete except for the body.

        The body is the one thing the model composes rather than runs, because the
        summary does not exist until it writes one, and it arrives on stdin through
        `--body-file -` so its quoting cannot be mangled. Everything that decides
        placement, filename, and frontmatter stays resolved here. The worker leaves the
        title off the line and hands it over in the environment, because a model asked
        to type it rewrites it; a manual sweep has no such channel, so `title` puts it
        on the line.
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
        if title:
            args += ["--title", title]
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

    def _find_log(self, target: Target, session_id: str) -> tuple[Path, str] | None:
        """This session's log path and full text, or None when it has none yet.

        The matching `session_id:` line is matched literally rather than parsed as
        YAML: the hooks carry no dependencies, and the CLI is the only writer of
        this field.
        """
        if not session_id:
            return None
        try:
            candidates = sorted(target.logs_dir.glob("*.md"))
        except OSError:
            return None
        needle = f"session_id: {session_id}"
        for path in candidates:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if needle in text:
                return path, text
        return None

    def _existing_log(self, target: Target, session_id: str) -> str:
        """This session's log body, with no frontmatter, or '' when it has none yet.

        `sessionmemory log` replaces the body on every call, so a second sweep of one
        session has to resend everything the first sweep wrote. The frontmatter block
        is stripped before returning, since this text is interpolated straight into a
        template the model is told to carry forward into the new body, and frontmatter
        carried into a note's body corrupts it on the next `sessionmemory log`.
        """
        found = self._find_log(target, session_id)
        return _strip_frontmatter(found[1]) if found else ""

    def _run_job(self, job: SweepJob) -> list[str]:
        """Take the lock, build the prompt, run the agent, validate its writes, log.

        Waits behind any sweep already running for this project, and returns no
        notes when that wait runs out. Releases the lock it took and never raises
        (it runs in a detached worker with no one to catch it). Returns the
        remediation notes.
        """
        if self.runner is None:
            _log.info("sweep skipped: no runner")
            return []
        lock = Lock(self.store.lock_path)
        log.bind(step="sweep", session=job.session_id)
        if not self._await_lock(lock):
            return []
        try:
            if self.vault is None and self.vault_root is not None:
                self.vault = VaultCLI.for_root(
                    self.vault_root, env=self.env, version_timeout=WORKER_CLI_TIMEOUT
                )
            composed = self.compose(job)
            if isinstance(composed, str):
                _log.info("sweep skipped: %s", composed)
                return []
            target, prompt = composed
            started_at = time.time()
            guard = WriteGuard(
                state_dir=self.store.state_dir,
                vault_root=self.vault.root if self.vault else None,
            )
            guard.capture_local(target)
            before = snapshot.take(target.project_dir)
            result = self.runner.run(
                prompt,
                cwd=str(target.project_dir),
                extra_env={LOG_TITLE_ENV: _log_moment(job.started)},
            )
            changes = snapshot.diff(before, snapshot.take(target.project_dir))
            _log.debug("claude ran %.1fs", time.time() - started_at)
            notes = guard.validate_writes(result.changed_files, target, changes)
            notes.extend(guard.enforce_shapes(changes, target))
            # The sentinel also appears in the transcript the agent summarizes, so
            # an actual write outranks it: a run that wrote something did record.
            if NOTHING_TO_RECORD in result.text and not changes.touched() and not changes.deleted:
                notes.append("nothing to record")
            found = self._find_log(target, job.session_id)
            note = _log_summary(found[0]) if found else ""
            commit_clause = self._commit(note, own_lock=lock.path)
            self._log_result(result, changes=changes, commit=commit_clause, notes=notes)
        except Exception:  # noqa: BLE001 - the detached worker must never raise
            _log.exception("sweep worker failed")
            return []
        else:
            return [*notes, commit_clause]
        finally:
            lock.release()

    def _commit(self, note: str, *, own_lock: Path) -> str:
        """Commit the vault unless another sweep is still writing; the outcome for the log.

        Skipping loses nothing: when two workers finish at once both may skip, and the
        next hook commits what they left.
        """
        if self.vault is None:
            return "commit skipped: no vault"
        if any_in_progress(self.env, except_path=own_lock):
            return "commit skipped: another sweep in progress"
        return self.vault.commit(env=self.env, note=note).describe()

    def _log_result(
        self, result: RunResult, *, changes: Changes, commit: str, notes: list[str]
    ) -> None:
        """One line for the run: what it wrote and how the commit went, or why it failed."""
        # Validation, shape checks, and the commit run whether or not claude succeeded.
        outcome = f"{changes.describe()}, {commit}"
        suffix = f" (notes: {'; '.join(notes)})" if notes else ""
        if result.success:
            _log.info("sweep finished: ok, %s%s", outcome, suffix)
            return
        cause = " ".join((result.text or "").split())[:RESULT_TEXT_CHARS]
        tail = " ".join((result.stderr or "").split())[-STDERR_TAIL_CHARS:]
        parts = [part for part in (cause, f"stderr: {tail}" if tail else "") if part]
        _log.error(
            "sweep failed: claude exited %s (%s), %s%s",
            result.exit_code,
            "; ".join(parts) or "no output",
            outcome,
            suffix,
        )

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
