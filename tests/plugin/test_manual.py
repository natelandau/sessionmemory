"""Verify the manual sweep renders its prompt, then checks what the agent wrote."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from sessionhooks.config import SessionMemoryConfig  # ty: ignore[unresolved-import]
from sessionhooks.guard import Target  # ty: ignore[unresolved-import]
from sessionhooks.manual import (  # ty: ignore[unresolved-import]
    SESSION_ENV,
    ManualSweep,
    ManualSweepError,
    Started,
)
from sessionhooks.vaultcli import Lookup, VaultCLI  # ty: ignore[unresolved-import]

from sessionmemory.lib.bootstrap import initialize
from tests._env import clean_environ
from tests.plugin._store_factory import store_at

HOOKS = Path(__file__).resolve().parent.parent.parent / "hooks"
SECRET = "token = 'ghp_" + "a" * 36 + "'"
GOOD_LEARNING = (
    '---\ntitle: "T"\nuuid: u\nsummary: "S"\ncreated: "2026-10-04"\n'
    'updated: "2026-10-04"\n---\n\nBody\n'
)


class _FakeVault:
    """A reachable vault whose project lookup answers without a CLI subprocess."""

    def __init__(self, target: Target, *, root: Path) -> None:
        self.target = target
        self.root = root
        self.command = str(root / "bin" / "sessionmemory")

    def project_lookup(self, *, cwd: Path, env: dict, timeout: int) -> Lookup:
        return Lookup(
            {
                "project_dir": str(self.target.project_dir),
                "learnings": str(self.target.learnings_dir),
                "backlog": str(self.target.backlog_path),
                "logs": str(self.target.logs_dir),
            }
        )


@pytest.fixture
def target(tmp_path: Path) -> Target:
    """A project folder in a vault, with its fields created."""
    project = tmp_path / "vault" / "projects" / "proj"
    (project / "learnings").mkdir(parents=True)
    (project / "logs").mkdir()
    return Target(
        project_dir=project,
        learnings_dir=project / "learnings",
        backlog_path=project / "backlog.md",
        logs_dir=project / "logs",
    )


@pytest.fixture
def manual_factory(tmp_path: Path, target: Target):
    """Build a ManualSweep for one session over the `target` project."""

    def _make(session_id: str = "sess-1", *, vault: bool = True) -> ManualSweep:
        return ManualSweep(
            store=store_at(tmp_path),
            config=SessionMemoryConfig(),
            env={SESSION_ENV: session_id, "CLAUDE_CONFIG_DIR": str(tmp_path / "claude")},
            cwd=tmp_path,
            vault=_FakeVault(target, root=tmp_path / "vault") if vault else None,
        )

    return _make


def test_begin_requires_a_session_id(manual_factory) -> None:
    """Verify a manual sweep outside a Claude Code session refuses to start."""
    with pytest.raises(ManualSweepError, match=SESSION_ENV):
        manual_factory("").begin()


def test_begin_requires_a_vault_cli(manual_factory) -> None:
    """Verify a manual sweep with no vault CLI says so rather than rendering a prompt."""
    with pytest.raises(ManualSweepError, match="no CLI"):
        manual_factory(vault=False).begin()


def test_begin_renders_the_manual_prompt(manual_factory, target: Target) -> None:
    """Verify the prompt names the project folder, carries the log title, and has no transcript."""
    # When starting a manual sweep
    prompt = manual_factory("sess-42").begin()

    # Then the prompt is the manual variant with a complete log command
    assert str(target.project_dir) in prompt
    assert "<transcript>" not in prompt
    log_line = next(line for line in prompt.splitlines() if " log --session-id " in line)
    assert "sess-42" in log_line
    assert "--title" in log_line


def test_begin_finds_the_session_transcript(manual_factory, tmp_path: Path, target: Target) -> None:
    """Verify the log command names the session's own transcript."""
    # Given the session's transcript under the Claude config dir
    transcript = tmp_path / "claude" / "projects" / "-some-repo" / "sess-1.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({"type": "user", "timestamp": "2026-10-25T10:00:00Z"}) + "\n")

    # When starting a manual sweep
    prompt = manual_factory().begin()

    # Then the log command passes that transcript
    assert f"--transcript {transcript}" in prompt


def test_finish_without_begin_is_refused(manual_factory) -> None:
    """Verify a check with no sweep started fails, so it cannot pass for a finished sweep."""
    with pytest.raises(ManualSweepError, match="no manual sweep in progress"):
        manual_factory().finish()


def test_finish_refuses_another_sessions_record(manual_factory) -> None:
    """Verify a record left by another session does not count for this one."""
    # Given a sweep started by a different session
    manual_factory("sess-other").begin()

    # When this session checks / Then it is refused
    with pytest.raises(ManualSweepError, match="no manual sweep in progress"):
        manual_factory("sess-1").finish()


def test_finish_scrubs_a_secret_written_after_begin(manual_factory, target: Target) -> None:
    """Verify a page the agent wrote with a secret in it is redacted."""
    # Given a started sweep, then a learning written with a secret
    manual_factory().begin()
    leaked = target.learnings_dir / "leak.md"
    leaked.write_text(GOOD_LEARNING + SECRET, encoding="utf-8")

    # When checking
    counts, notes = manual_factory().finish()

    # Then the secret is gone and the write is counted
    assert counts == "1 created, 0 modified, 0 deleted"
    assert any(note.startswith("secret-redacted") for note in notes)
    assert "ghp_" not in leaked.read_text(encoding="utf-8")


def test_finish_quarantines_a_malformed_learning(manual_factory, target: Target) -> None:
    """Verify a new learning without the page shape is set aside."""
    # Given a started sweep, then a learning with no title or summary
    manual_factory().begin()
    bad = target.learnings_dir / "bad-page.md"
    bad.write_text("---\nname: x\n---\nBody\n", encoding="utf-8")

    # When checking
    _, notes = manual_factory().finish()

    # Then the page is quarantined
    assert not bad.exists()
    assert any(note.startswith(f"malformed-quarantined: {bad.resolve()}") for note in notes)


def test_finish_restores_local_from_before_begin(manual_factory, target: Target) -> None:
    """Verify an edited file under local/ is put back from bytes captured in the earlier process."""
    # Given a person's skill file under local/, a started sweep, then an edit to it
    local_md = target.project_dir / "local" / "claude" / "skills" / "a" / "SKILL.md"
    local_md.parent.mkdir(parents=True)
    local_md.write_text("private notes\n", encoding="utf-8")
    manual_factory().begin()
    local_md.write_text("edited by the sweep\n", encoding="utf-8")

    # When checking
    _, notes = manual_factory().finish()

    # Then the person's content is back
    assert local_md.read_text(encoding="utf-8") == "private notes\n"
    assert any(note.startswith("malformed-restored") for note in notes)


def test_finish_removes_its_record(manual_factory) -> None:
    """Verify a second check after a finished one is refused."""
    # Given a sweep started and checked once
    manual_factory().begin()
    manual_factory().finish()

    # When checking again / Then it is refused
    with pytest.raises(ManualSweepError):
        manual_factory().finish()


@pytest.mark.parametrize("key", ["local", "local_md"])
def test_started_load_reads_the_local_capture_under_either_key(
    tmp_path: Path, target: Target, key: str
) -> None:
    """Verify a record holding its capture under `local` or the older `local_md` loads it."""
    # Given a record whose capture sits under `key`
    path = tmp_path / "manual-sweep.json"
    captured = target.project_dir / "local" / "CLAUDE.local.md"
    payload = {
        "session_id": "s",
        "target": {
            "project_dir": str(target.project_dir),
            "learnings": str(target.learnings_dir),
            "backlog": str(target.backlog_path),
            "logs": str(target.logs_dir),
        },
        "before": {},
        key: {str(captured): "bm90ZXM="},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    # When loading it
    started = Started.load(path)

    # Then the capture is read back
    assert started is not None
    assert started.local == {captured: b"notes"}


def test_begin_skips_regenerable_and_foreign_trees_under_local(
    manual_factory, target: Target, tmp_path: Path
) -> None:
    """Verify the capture leaves out files inside node_modules, .venv, venv, and .git."""
    # Given a skill folder holding a real file beside regenerable and foreign trees
    skill = target.project_dir / "local" / "claude" / "skills" / "a"
    for folder in ("node_modules/pkg", ".venv/lib", "venv/bin", ".git/objects", "scripts"):
        (skill / folder).mkdir(parents=True)
        (skill / folder / "f.txt").write_text("x", encoding="utf-8")

    # When starting a manual sweep
    manual_factory().begin()

    # Then only the skill's own file is in the recorded capture
    started = Started.load(manual_factory().state_path)
    assert started is not None
    assert set(started.local) == {(skill / "scripts" / "f.txt").resolve()}


@pytest.mark.parametrize("content", ["", "not json", '{"session_id": "s"}', "[]"])
def test_started_load_rejects_a_damaged_record(tmp_path: Path, content: str) -> None:
    """Verify a missing, corrupt, or incomplete record reads as no record."""
    path = tmp_path / "manual-sweep.json"
    if content:
        path.write_text(content, encoding="utf-8")

    assert Started.load(path) is None


# ---------------------------------------------------------------------------
# sweep-prompt.py and sweep-check.py, end to end against a real vault
# ---------------------------------------------------------------------------


def _script_env(tmp_path: Path, vault: Path, session_id: str) -> dict[str, str]:
    """An environment that reaches only the scratch vault, never a developer's own."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return {
        **clean_environ(),
        "HOME": str(home),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "SESSIONMEMORY_VAULT": str(vault),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
        SESSION_ENV: session_id,
    }


def _run_script(name: str, *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(HOOKS / name)],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=120,
    )


@pytest.fixture
def registered(tmp_path: Path) -> tuple[Path, Path]:
    """A git repository registered as a project in a real, initialized vault."""
    proj = tmp_path / "repo"
    proj.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True, env=clean_environ())
    vault = tmp_path / "vault"
    initialize(vault)
    env = {**clean_environ(), "SESSIONMEMORY_VAULT": str(vault)}
    assert VaultCLI(root=vault).output(
        ["project", "register", "--cwd", str(proj)], cwd=proj, env=env
    )
    return proj, vault


def test_scripts_check_what_the_agent_wrote(tmp_path: Path, registered: tuple[Path, Path]) -> None:
    """Verify the prompt script starts a sweep and the check script scrubs what followed."""
    proj, vault = registered
    env = _script_env(tmp_path, vault, "sess-e2e")

    # When the prompt script runs
    started = _run_script("sweep-prompt.py", cwd=proj, env=env)

    # Then it prints the manual prompt
    assert started.returncode == 0, started.stderr
    assert "--session-id sess-e2e" in started.stdout

    # When a learning with a secret is written and the check script runs
    out = VaultCLI(root=vault).output(["path", "learnings", "--cwd", str(proj)], cwd=proj, env=env)
    learnings = Path((out or "").strip())
    learnings.mkdir(parents=True, exist_ok=True)
    leaked = learnings / "leak.md"
    leaked.write_text(GOOD_LEARNING + SECRET, encoding="utf-8")
    checked = _run_script("sweep-check.py", cwd=proj, env=env)

    # Then it succeeds and the secret is redacted
    assert checked.returncode == 0, checked.stderr
    assert "sweep checked: 1 created" in checked.stdout
    assert "ghp_" not in leaked.read_text(encoding="utf-8")


@pytest.mark.parametrize("script", ["sweep-prompt.py", "sweep-check.py"])
def test_scripts_exit_nonzero_outside_a_session(
    tmp_path: Path, registered: tuple[Path, Path], script: str
) -> None:
    """Verify both scripts fail with a reason when no session id is set."""
    proj, vault = registered
    env = _script_env(tmp_path, vault, "")

    proc = _run_script(script, cwd=proj, env=env)

    assert proc.returncode == 1
    assert SESSION_ENV in proc.stderr
