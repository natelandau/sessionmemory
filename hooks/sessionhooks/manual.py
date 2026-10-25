"""A sweep run by hand in the user's own session, in two halves around the agent's writes.

`ManualSweep.begin` renders the same prompt the detached worker runs and records the
project folder as it stands. The main agent then writes, and `ManualSweep.finish` runs
the worker's backstops on whatever changed since. The two halves are separate processes
with the agent's turn between them, so the record crosses as a state file in the
project's machine-local state directory.

A successful `finish` is also what makes the session's transcript show a finished
manual sweep, which `transcript.window_since_boundary` reads so the automatic sweep
covers only the work after it.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sessionhooks import log, snapshot, transcript
from sessionhooks.guard import Target, WriteGuard
from sessionhooks.sweep import Sweep, SweepJob

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sessionhooks.config import SessionMemoryConfig
    from sessionhooks.store import Store
    from sessionhooks.vaultcli import VaultCLI

_log = log.logger("manual")

STATE_NAME = "manual-sweep.json"
# Claude Code sets this in every Bash tool call's environment.
SESSION_ENV = "CLAUDE_CODE_SESSION_ID"


class ManualSweepError(Exception):
    """A manual sweep cannot start or be checked; the message says why."""


@dataclass(frozen=True, slots=True)
class Started:
    """What `begin` recorded for `finish` to compare against."""

    session_id: str
    target: Target
    before: dict[Path, str]
    local_md: dict[Path, bytes]

    def save(self, path: Path) -> None:
        """Write the record as JSON, creating the state directory when missing."""
        payload = {
            "session_id": self.session_id,
            "target": {
                "project_dir": str(self.target.project_dir),
                "learnings": str(self.target.learnings_dir),
                "backlog": str(self.target.backlog_path),
                "logs": str(self.target.logs_dir),
            },
            "before": {str(p): digest for p, digest in self.before.items()},
            "local_md": {str(p): base64.b64encode(b).decode() for p, b in self.local_md.items()},
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Started | None:
        """Read a record `save` wrote, or None when it is missing or unreadable."""
        try:
            payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            target = Target.from_paths(payload["target"])
            if target is None:
                return None
            return cls(
                session_id=str(payload["session_id"]),
                target=target,
                before={Path(p): str(d) for p, d in payload["before"].items()},
                local_md={Path(p): base64.b64decode(b) for p, b in payload["local_md"].items()},
            )
        except (OSError, ValueError, KeyError, TypeError, AttributeError, binascii.Error):
            return None


class ManualSweep:
    """One session's manual sweep: render the prompt, then check what the agent wrote."""

    def __init__(
        self,
        *,
        store: Store,
        config: SessionMemoryConfig,
        env: Mapping[str, str],
        cwd: Path,
        vault: VaultCLI | None = None,
        vault_root: Path | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self.env = env
        self.cwd = cwd
        # `begin` runs the CLI and needs `vault`; `finish` only restores from git and
        # needs the root alone, which costs no version handshake to find.
        self.vault = vault
        self.vault_root = vault.root if vault else vault_root

    @property
    def state_path(self) -> Path:
        """Where `begin` leaves its record for `finish`."""
        return self.store.state_dir / STATE_NAME

    def _session_id(self) -> str:
        session_id = self.env.get(SESSION_ENV, "").strip()
        if not session_id:
            msg = f"no session id: {SESSION_ENV} is unset, so this is not a Claude Code session"
            raise ManualSweepError(msg)
        return session_id

    def begin(self) -> str:
        """Render this session's sweep prompt and record the project folder before any write.

        Raises:
            ManualSweepError: When there is no session id, no vault CLI, or no project.
        """
        session_id = self._session_id()
        transcript_path = transcript.find_transcript(session_id, env=self.env)
        entries = transcript.read_entries(transcript_path) if transcript_path else []
        job = SweepJob(
            window=[],
            cwd=str(self.cwd),
            session_id=session_id,
            transcript_path=transcript_path,
            session_url=transcript.session_url(entries),
            started=transcript.session_start(entries),
        )
        sweep = Sweep(self.store, self.config, vault=self.vault, env=self.env)
        composed = sweep.compose(job, manual=True)
        if isinstance(composed, str):
            raise ManualSweepError(composed)
        target, prompt = composed
        guard = WriteGuard(state_dir=self.store.state_dir, vault_root=self.vault_root)
        guard.capture_local_md(target)
        Started(
            session_id=session_id,
            target=target,
            before=snapshot.take(target.project_dir),
            local_md=guard.local_md_before,
        ).save(self.state_path)
        _log.info("manual sweep started")
        return prompt

    def finish(self) -> tuple[str, list[str]]:
        """Run the worker's backstops on what changed since `begin`; the counts and the notes.

        Removes the record once checked, so a second `finish` cannot pass for a sweep
        that never started.

        Raises:
            ManualSweepError: When this session has no manual sweep in progress.
        """
        session_id = self._session_id()
        started = Started.load(self.state_path)
        if started is None or started.session_id != session_id:
            msg = "no manual sweep in progress for this session: run sweep-prompt.py first"
            raise ManualSweepError(msg)
        target = started.target
        guard = WriteGuard(
            state_dir=self.store.state_dir,
            vault_root=self.vault_root,
            local_md_before=started.local_md,
        )
        changes = snapshot.diff(started.before, snapshot.take(target.project_dir))
        notes = guard.validate_writes([], target, changes)
        notes.extend(guard.enforce_shapes(changes, target))
        self.state_path.unlink(missing_ok=True)
        suffix = f" (notes: {'; '.join(notes)})" if notes else ""
        _log.info("manual sweep checked: %s%s", changes.describe(), suffix)
        return changes.describe(), notes
