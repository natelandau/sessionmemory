"""The backstops that run after a sweep writes into a project's vault folder.

A sweep may write anywhere inside its own project's folder and nowhere else. These
checks run on what it changed, whoever ran it: the detached worker after its headless
run, and the manual sweep's check script after the main agent's. Containment reverts a
tool-reported write outside the folder. The secret scrub redacts every file whose hash
changed, which catches a Bash write the tool-use stream never reports. The shape checks
set aside a page its folder cannot hold, and every file in the person's `local/` folder is
put back however it was changed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from sessionhooks import log, shape
from sessionhooks.paths import is_within_root
from sessionhooks.safety import scrub
from sessionhooks.store import git_safe_env

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sessionhooks.snapshot import Changes

_log = log.logger("guard")

_GIT_TIMEOUT = 10


# Regenerable trees and foreign repositories a skill folder may hold. Their bytes are
# not captured, since a manual sweep writes the capture into its state file.
_UNCAPTURED_DIRS = frozenset({"node_modules", ".venv", "venv", ".git"})


def _local_files(project_dir: Path) -> list[Path]:
    """List the regular files under `local/`, and the CLAUDE.local.md still at the top."""
    found: list[Path] = []
    local = project_dir / shape.LOCAL_DIR
    walked = local.walk(on_error=_log_unreadable) if local.is_dir() else ()
    for folder, dirnames, filenames in walked:
        dirnames[:] = [name for name in dirnames if name not in _UNCAPTURED_DIRS]
        found.extend(folder / name for name in filenames)
    candidates = [project_dir / shape.LOCAL_MD, *sorted(found)]
    files: list[Path] = []
    for path in candidates:
        try:
            if path.is_file() and not path.is_symlink() and not _is_local_debris(path):
                files.append(path)
        except OSError:
            continue
    return files


def _log_unreadable(exc: OSError) -> None:
    _log.error("local folder unreadable before the run: %s", exc)


def _is_local_debris(path: Path) -> bool:
    """Report whether a file under `local/` is debris the vault's `.gitignore` drops.

    Only what that file drops counts: other debris, such as a sync conflict copy, is
    committed and may hold the person's only copy of an edit.
    """
    return path.name == ".DS_Store" or "__pycache__" in path.parts


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


class WriteGuard:
    """Undo what a sweep may not leave behind in one project's vault folder.

    Built per run: `capture_local` runs before the sweep writes, and
    `validate_writes` then `enforce_shapes` run on the snapshot diff after it.
    """

    def __init__(
        self,
        *,
        state_dir: Path,
        vault_root: Path | None,
        local_before: Mapping[Path, bytes] | None = None,
    ) -> None:
        self.state_dir = state_dir
        # Without a vault root nothing can be restored from HEAD, so every undo
        # falls back to quarantine.
        self.vault_root = vault_root
        # The `local/` files as they stood before the sweep wrote, so a restore returns
        # the person's uncommitted edits rather than whatever HEAD holds. A guard built
        # in another process than the capture takes them here.
        self._local_before: dict[Path, bytes] = dict(local_before or {})

    @property
    def local_before(self) -> dict[Path, bytes]:
        """What `capture_local` recorded, for a caller that checks in another process."""
        return dict(self._local_before)

    def capture_local(self, target: Target) -> None:
        """Remember every file under `local/` as bytes before the run; an unreadable one is not kept.

        The CLAUDE.local.md still at the project folder's top, before the CLI moves it into
        `local/`, is captured too.
        """
        self._local_before.clear()
        for path in _local_files(target.project_dir):
            try:
                self._local_before[path.resolve()] = path.read_bytes()
            except OSError:
                _log.exception("local file unreadable before the run: %s", path)

    def validate_writes(
        self, changed_files: list[str], target: Target, changes: Changes
    ) -> list[str]:
        """Enforce containment on tool-reported writes and scrub every file the run changed.

        Bash never reports a path, so the second pass scrubs every file under the project
        folder whose content hash changed during the run, and names each deletion. A
        relative tool-reported path is anchored to the project folder.
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

    def enforce_shapes(self, changes: Changes, target: Target) -> list[str]:
        """Set aside each written page that lacks the shape its folder requires.

        A new file that fails is quarantined. A modified learning that fails is
        restored from HEAD, so a good page the model rewrote into another format is
        not lost. Everything under `local/` belongs to the person: a modified file is
        restored with its displaced content saved aside, and a deleted one is restored
        too. No other edit is checked, since a log or backlog edit keeps its
        name and has no frontmatter to break. A learning that cannot be read is
        quarantined whether new or modified: its content is unknown, so HEAD is no
        safer a guess, and left in place it would fail `git add`. Debris a sync tool,
        editor, or OS drops is left alone, as the CLI ignores it too.
        """
        notes: list[str] = []
        learnings = target.learnings_dir.resolve()
        logs = target.logs_dir.resolve()
        judged = [(path, True) for path in changes.created]
        judged += [(path, False) for path in changes.modified]
        for path, is_new in judged:
            if not path.exists() or shape.is_debris(path.name):
                continue
            parent = path.parent
            if parent == learnings:
                problem = shape.learning_problem(path)
                if path in changes.unreadable or problem == shape.UNREADABLE:
                    notes.append(self._quarantine_unreadable(path))
                    continue
            elif parent == logs and is_new:
                problem = shape.log_problem(path)
            elif is_new:
                problem = shape.nested_problem(path, (learnings, logs)) or shape.stray_problem(
                    path, target.project_dir
                )
            else:
                continue
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
        notes.extend(self._protect_local(changes, target))
        return notes

    def _quarantine(self, path: Path) -> None:
        """Move `path` into this project's quarantine directory rather than deleting it.

        Nothing outside the write roots is snapshotted before a run, so a write
        git cannot restore from HEAD might still be a note an earlier, not-yet-
        committed sweep created, or content that predates this run entirely.
        Moving it aside keeps it recoverable instead of guessing it was safe to
        erase. The random prefix keeps two quarantined files of the same name
        from colliding.
        """
        shutil.move(str(path), str(self._quarantine_dest(path)))

    def _quarantine_dest(self, path: Path) -> Path:
        """A fresh name in this project's quarantine directory for a file named like `path`."""
        quarantine_dir = self.state_dir / "quarantine"
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        return quarantine_dir / f"{uuid.uuid4().hex}-{path.name}"

    @staticmethod
    def _write_atomic(path: Path, data: bytes) -> None:
        """Replace `path` with `data` through a temp file in its directory, so a crash leaves one whole copy."""
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp.write_bytes(data)
            temp.replace(path)
        except OSError:
            temp.unlink(missing_ok=True)
            raise

    def _restore_local(self, path: Path, problem: str) -> str:
        """Return a file under `local/` to its pre-run content, keeping what it displaces.

        The edit may be the person's own, made while the sweep ran, so the content is
        copied aside before the restore. The pre-run bytes win over HEAD, since the
        person may not have committed their latest edit. Only a file with no pre-run
        bytes falls back to HEAD, and one HEAD does not hold yet stays in place: it may
        be a concurrent `local adopt` or move in another session, and moving it would
        leave every checkout's link dangling.
        """
        saved = self._quarantine_dest(path)
        shutil.copy2(path, saved)
        before = self._local_before.get(path.resolve())
        try:
            if before is not None:
                self._write_atomic(path, before)
                restored = True
            else:
                restored = self._git_restore(path)
        except (OSError, subprocess.SubprocessError) as exc:
            return f"malformed-unremovable: {path} ({problem}; {exc}; copy saved to {saved})"
        if restored:
            return f"malformed-restored: {path} ({problem}; displaced copy saved to {saved})"
        saved.unlink(missing_ok=True)
        return f"local-kept: {path} ({problem}; not in HEAD, left in place)"

    def _protect_local(self, changes: Changes, target: Target) -> list[str]:
        """Undo the run's writes, edits, and deletions under the person's `local/` folder.

        A deleted file is restored too, since a checkout's link points at it.
        """
        notes = [
            self._local_note(path, problem)
            for path in changes.touched()
            if path.exists()
            and not _is_local_debris(path)
            and (problem := shape.local_problem(path, target.project_dir)) is not None
        ]
        for path in changes.deleted:
            problem = shape.local_problem(path, target.project_dir)
            if problem is None or _is_local_debris(path):
                continue
            before = self._local_before.get(path.resolve())
            try:
                if before is not None:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    self._write_atomic(path, before)
                    restored = True
                else:
                    restored = self._git_restore(path)
            except (OSError, subprocess.SubprocessError) as exc:
                notes.append(f"deleted-unrestorable: {path} ({problem}; {exc})")
                continue
            if restored:
                notes.append(f"deleted-restored: {path} ({problem})")
            else:
                notes.append(f"deleted-unrestorable: {path} ({problem}; not in HEAD)")
        return notes

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
        if self.vault_root is None:
            return False
        restore = subprocess.run(  # noqa: S603
            ["git", "-C", str(self.vault_root), "checkout", "HEAD", "--", str(path)],  # noqa: S607
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

    def _local_note(self, path: Path, problem: str) -> str:
        """Restore a changed file under `local/` and phrase the outcome as a note; never raises."""
        try:
            return self._restore_local(path, problem)
        except (OSError, subprocess.SubprocessError) as exc:
            return f"malformed-unremovable: {path} ({problem}; {exc})"

    def _quarantine_unreadable(self, path: Path) -> str:
        """Move an unreadable page aside and phrase the outcome as a note; never raises."""
        try:
            self._quarantine(path)
        except OSError as exc:
            return f"unreadable-unremovable: {path} ({exc})"
        return f"unreadable-quarantined: {path}"

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
        """Apply containment and the secret scrub to one path, once.

        A file under `local/` is never scrubbed: the local guard owns it, and a redaction
        would rewrite the person's bytes, including the copy saved aside before a restore.
        """
        resolved = path.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        if not any(is_within_root(path, root) for root in target.roots):
            notes.append(self._revert_and_note(path, raw))
            return
        if shape.local_problem(path, target.project_dir) is not None:
            return
        note = self._scrub_path(path, raw)
        if note is not None:
            notes.append(note)
