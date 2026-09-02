"""Machine-local session state: stable key derivation, XDG roots, and IO.

Durable memory lives in the vault, reached through
`sessionhooks.vaultcli`. What stays here is everything that describes a session on
this machine rather than knowledge worth keeping: the sweep lock, the pointer to
the transcript, the log of what each sweep did, the commit a session started from,
and the consume-once handoff.

None of it belongs in the vault. A lock committed to git reads as held on every
other machine, a transcript path is meaningless on any other, and a handoff is a
baton that exists for minutes. The vault's committer would preserve all of it
forever.

The per-project directory name is the project ROOT path (resolved so every
worktree and branch of a repo share one store), dash-encoded.
"""

from __future__ import annotations

import contextlib
import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

PLUGIN_NS = "sessionmemory"
HANDOFF_NAME = "HANDOFF.md"
LOCK_NAME = "sweep.lock"
TRANSCRIPT_POINTER_NAME = "transcript-path"
LOG_NAME = "sweep.log"
BASE_COMMIT_NAME = "base-commit"

_GIT_TIMEOUT = 5

# Prefix for every env var git uses to locate or override a repository. An
# inherited one pins `git` to whatever repo it names instead of `cwd`, so a hook
# would resolve the wrong project key and read and write another project's
# memory. Git exports these when it runs a hook and when the shell sits in a
# linked worktree. The rule is a prefix rather than a fixed set because the
# GIT_CONFIG_KEY_<n> / GIT_CONFIG_VALUE_<n> family is indexed and cannot be
# enumerated, and a config override such as core.worktree relocates a repository
# exactly as GIT_DIR does. That makes it broader than "location" -- vars that
# only affect behavior go too -- because there is no rule that keeps those and
# still covers the indexed family.
GIT_VAR_PREFIX = "GIT_"


def git_safe_env(env: Mapping[str, str]) -> dict[str, str]:
    """Copy `env` without the git vars so git resolves from `cwd`, not an ambient GIT_DIR."""
    return {k: v for k, v in env.items() if not k.startswith(GIT_VAR_PREFIX)}


def encode_project_key(path: Path) -> str:
    """Encode an absolute path into one flat directory name.

    The leading slash is dropped, then each remaining `/` becomes `-`; a path
    segment that begins with `.` (a hidden directory) has its leading dot turned
    into a dash, yielding a double dash at that boundary. Interior dots are kept.

    Dropping the leading slash (rather than encoding it to a leading `-`) keeps
    the key from starting with a dash, which shells and CLI tools would otherwise
    parse as an option flag (e.g. `rm -rf -Users-...`).
    """
    parts = [part for part in str(path).split("/") if part]
    encoded = ["-" + part[1:] if part.startswith(".") else part for part in parts]
    return "-".join(encoded)


def project_root(*, cwd: Path, env: Mapping[str, str]) -> Path:
    """Resolve the stable project root for `cwd`.

    Order: the git common dir's parent (shared across all worktrees/branches),
    else `CLAUDE_PROJECT_DIR`, else `cwd`. Always returns a resolved absolute
    path. Never raises -- git failures fall through to the env/cwd fallbacks.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],  # noqa: S607
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            check=False,
            env=git_safe_env(env),
        )
        out = proc.stdout.strip()
        if proc.returncode == 0 and out:
            common = Path(out)
            return (common.parent if common.name == ".git" else common).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    configured = env.get("CLAUDE_PROJECT_DIR")
    if configured:
        return Path(configured).resolve()
    return cwd.resolve()


def head_commit(*, cwd: Path, env: Mapping[str, str]) -> str:
    """The commit checked out at `cwd`, or '' when there is no repository or no commit."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            check=False,
            env=git_safe_env(env),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _xdg_root(env: Mapping[str, str], var: str, default_rel: str) -> Path:
    """Resolve an XDG base dir from `var`, falling back to ~/`default_rel`."""
    base = env.get(var)
    root = Path(base) if base else Path.home() / default_rel
    return root / PLUGIN_NS


def _state_dir(key: str, *, env: Mapping[str, str]) -> Path:
    """Return the ephemeral state dir for a project key (hashed, not created)."""
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]  # noqa: S324
    return _xdg_root(env, "XDG_STATE_HOME", ".local/state") / digest


@dataclass(frozen=True, slots=True)
class Store:
    """The resolved session-state location for one project: key + ephemeral root."""

    key: str
    state_dir: Path

    @classmethod
    def for_cwd(cls, *, cwd: Path, env: Mapping[str, str]) -> Store:
        """Resolve the store for `cwd`: project root -> key -> XDG state dir."""
        key = encode_project_key(project_root(cwd=cwd, env=env))
        return cls(key=key, state_dir=_state_dir(key, env=env))

    @property
    def handoff_path(self) -> Path:
        """The consume-once session handoff.

        Machine-local rather than in the vault: it is a baton that lives for
        minutes, and the vault's commit job would put every one of them into
        permanent history on every machine.
        """
        return self.state_dir / HANDOFF_NAME

    @property
    def base_commit_path(self) -> Path:
        """The commit this project was on when the session started."""
        return self.state_dir / BASE_COMMIT_NAME

    @property
    def lock_path(self) -> Path:
        """The single-writer sweep lock."""
        return self.state_dir / LOCK_NAME

    @property
    def transcript_pointer_path(self) -> Path:
        """The saved transcript path so the sweep finds it after /clear."""
        return self.state_dir / TRANSCRIPT_POINTER_NAME

    @property
    def log_path(self) -> Path:
        """The append-only sweep activity log."""
        return self.state_dir / LOG_NAME

    def save_transcript_pointer(self, transcript_path: str) -> None:
        """Best-effort persist the transcript path for the sweep; never raises."""
        if not transcript_path:
            return
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self.transcript_pointer_path.write_text(transcript_path, encoding="utf-8")
        except OSError:
            pass

    def read_transcript_pointer(self) -> str:
        """Return the saved transcript path, or '' when none is stored."""
        try:
            return self.transcript_pointer_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def read_handoff(self) -> str | None:
        """Return the consume-once handoff contents, or None when there is nothing to carry.

        Fails open: a missing, unreadable, empty, or non-UTF-8 HANDOFF.md (a
        hand-edited or pasted-in file) reads as None rather than raising, so one
        bad file never aborts the surrounding session-start injection.
        """
        try:
            text = self.handoff_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        return text or None

    def delete_handoff(self) -> None:
        """Best-effort remove the handoff after it has been injected; never raises.

        Called only once the inject is emitted, so a delete the OS rejects leaves
        the baton in place for the next attempt rather than dropping it silently.
        """
        with contextlib.suppress(OSError):
            self.handoff_path.unlink()

    def save_base_commit(self, commit: str) -> None:
        """Record where the session started so its log can report the whole span.

        Written when a session opens and not refreshed while it runs. The sweep
        fires after work has happened, so reading HEAD then would report only
        what came after the first fire.
        """
        if not commit:
            return
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self.base_commit_path.write_text(commit, encoding="utf-8")
        except OSError:
            pass

    def read_base_commit(self) -> str:
        """The recorded starting commit, or '' when none was stored."""
        try:
            return self.base_commit_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return ""
