"""End-to-end tests for the three thin hook scripts (env-isolated subprocesses).

Every case sets XDG_STATE_HOME under tmp_path and overrides
CLAUDE_PROJECT_DIR so a script can never read the machine's real plugin state.
Sweep cases set SESSIONMEMORY_HEADLESS=1 or stay below the exchange threshold so no
real `claude` is ever spawned and no worker is ever forked.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import ClassVar

import pytest
import sessionstart  # ty: ignore[unresolved-import]
from sessionhooks.config import SessionMemoryConfig  # ty: ignore[unresolved-import]
from sessionhooks.store import Store  # ty: ignore[unresolved-import]
from sessionhooks.vaultcli import ROOT_ENV  # ty: ignore[unresolved-import]

from sessionmemory.lib import field, registry
from sessionmemory.lib.bootstrap import initialize
from tests._env import clean_environ

HOOKS = Path(__file__).resolve().parent.parent.parent / "hooks"

# Dropped so the developer's own environment can never change a case's intent:
# the recursion guard (a case that needs it sets it via env_overrides), and the
# vault root, without which a machine that exports one would have every hook
# test read and write the real vault. (clean_environ also drops the git location
# vars so a hook resolves the tmp project, not the checkout the suite runs from.)
_AMBIENT = frozenset({"SESSIONMEMORY_HEADLESS", ROOT_ENV})


def _run(
    stage: str, payload: dict, env_overrides: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Pipe a JSON payload through a real hook script with an isolated environment."""
    env = {**clean_environ(also_drop=_AMBIENT), **env_overrides}
    return subprocess.run(
        [str(HOOKS / f"{stage}.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=30,
    )


TITLE = "The X gotcha"


def _isolated_env(tmp_path: Path, proj: Path, *, vault: Path | None = None) -> dict[str, str]:
    # HOME is redirected because SessionMemoryConfig reads ~/.claude/sessionmemory.toml.
    # A developer who configures a vault root there would otherwise have every case
    # run against their real vault, registering pytest tmp dirs as projects in it.
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        "HOME": str(home),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "CLAUDE_PROJECT_DIR": str(proj),
        "SESSIONMEMORY_EMBEDDER": "stub",
    }
    if vault is not None:
        env[ROOT_ENV] = str(vault)
    return env


def _fake_vault(tmp_path: Path, proj: Path, *, title: str = TITLE) -> Path:
    """A real, initialized vault with `proj` registered and one learning page."""
    vault = tmp_path / "vault"
    initialize(vault)
    env = {**clean_environ(also_drop=_AMBIENT), ROOT_ENV: str(vault)}
    subprocess.run(
        [str(HOOKS.parent / "bin" / "sessionmemory"), "project", "--register", "--cwd", str(proj)],
        env=env,
        check=True,
        capture_output=True,
    )
    field.new_page(
        vault / "projects" / proj.name / "learnings",
        title=title,
        summary="s",
        body="",
        now="2026-01-01T00:00:00Z",
    )
    return vault


def _store(tmp_path: Path, proj: Path) -> Store:
    """The Store a hook would resolve for `proj` under the tmp XDG roots."""
    return Store.for_cwd(
        cwd=proj,
        env={
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "CLAUDE_PROJECT_DIR": str(proj),
        },
    )


def _seed_handoff(tmp_path: Path, proj: Path, text: str = "# Handoff\nthe baton") -> Store:
    """Seed a HANDOFF.md into the project's machine-local state and return the store."""
    store = _store(tmp_path, proj)
    store.state_dir.mkdir(parents=True, exist_ok=True)
    store.handoff_path.write_text(text, encoding="utf-8")
    return store


# ---------------------------------------------------------------------------
# SessionStart
# ---------------------------------------------------------------------------


def test_sessionstart_empty_store_exits_cleanly(tmp_path: Path) -> None:
    """Verify SessionStart on an empty store exits 0 and leaves the store empty."""
    # Given an isolated, empty store
    proj = tmp_path / "proj"
    proj.mkdir()
    # When SessionStart runs
    proc = _run(
        "sessionstart", {"cwd": str(proj), "source": "startup"}, _isolated_env(tmp_path, proj)
    )
    # Then it exits cleanly and, with no transcript and no repository, records nothing
    assert proc.returncode == 0, proc.stderr
    assert not _store(tmp_path, proj).state_dir.exists()


def test_sessionstart_injects_the_block_the_vault_renders(tmp_path: Path) -> None:
    """Verify SessionStart returns whatever the vault CLI rendered, verbatim."""
    # Given a reachable vault with memory for this project
    proj = tmp_path / "proj"
    proj.mkdir()
    vault = _fake_vault(tmp_path, proj)
    # When SessionStart runs
    payload = {"cwd": str(proj), "transcript_path": str(tmp_path / "t.jsonl"), "source": "startup"}
    proc = _run("sessionstart", payload, _isolated_env(tmp_path, proj, vault=vault))
    # Then the vault's block is the injected context
    assert proc.returncode == 0, proc.stderr
    context = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert TITLE in context


def test_sessionstart_without_a_vault_injects_the_hint(tmp_path: Path) -> None:
    """Verify an unreachable vault is reported to the session, never an error at session start."""
    # Given no vault root anywhere
    proj = tmp_path / "proj"
    proj.mkdir()
    # When SessionStart runs
    proc = _run(
        "sessionstart", {"cwd": str(proj), "source": "startup"}, _isolated_env(tmp_path, proj)
    )
    # Then it exits cleanly and the hint is the whole injected context
    assert proc.returncode == 0, proc.stderr
    context = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert context == sessionstart.NO_VAULT_HINT


def test_sessionstart_records_the_starting_commit(tmp_path: Path) -> None:
    """Verify the commit is captured at start; the sweep fires too late to read it."""
    # Given a project repository with one commit
    proj = tmp_path / "proj"
    proj.mkdir()
    git_env = clean_environ()
    for args in (["init", "-q"], ["config", "user.email", "t@e.com"], ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=proj, check=True, capture_output=True, env=git_env)
    (proj / "f.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=proj, check=True, capture_output=True, env=git_env)
    subprocess.run(
        ["git", "commit", "-qm", "init"], cwd=proj, check=True, capture_output=True, env=git_env
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=proj,
        capture_output=True,
        text=True,
        check=True,
        env=git_env,
    ).stdout.strip()
    # When SessionStart runs
    _run("sessionstart", {"cwd": str(proj), "source": "startup"}, _isolated_env(tmp_path, proj))
    # Then the starting commit is on record for the sweep
    assert _store(tmp_path, proj).read_base_commit() == head


def _repo_with_two_commits(proj: Path) -> tuple[str, str]:
    """Initialize a repository with two commits and return both hashes, oldest first."""
    git_env = clean_environ()
    for args in (["init", "-q"], ["config", "user.email", "t@e.com"], ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=proj, check=True, capture_output=True, env=git_env)
    commits: list[str] = []
    for n in ("one", "two"):
        (proj / f"{n}.txt").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=proj, check=True, capture_output=True, env=git_env)
        subprocess.run(
            ["git", "commit", "-qm", n], cwd=proj, check=True, capture_output=True, env=git_env
        )
        commits.append(
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=proj,
                capture_output=True,
                text=True,
                check=True,
                env=git_env,
            ).stdout.strip()
        )
    return commits[0], commits[1]


def test_repo_with_two_commits_ignores_a_leaked_git_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify the tmp-repo builder targets its own project under a poisoned git env.

    A real `git commit` exports GIT_DIR (and often GIT_INDEX_FILE) into every
    subprocess its hooks spawn. If the builder forwarded that env to git
    unfiltered, `git add -A` in the tmp project would resolve against the
    outer repo's index instead of its own, staging the outer repo's tracked
    files as deleted.
    """
    # Given a decoy repo standing in for the outer repo running the hook
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    git_env = clean_environ()
    for args in (["init", "-q"], ["config", "user.email", "t@e.com"], ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=decoy, check=True, capture_output=True, env=git_env)
    (decoy / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=decoy, check=True, capture_output=True, env=git_env)
    subprocess.run(
        ["git", "commit", "-qm", "test: decoy has a tracked file"],
        cwd=decoy,
        check=True,
        capture_output=True,
        env=git_env,
    )

    # When the tmp-repo builder runs with the outer repo's git env leaked in
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(decoy / ".git" / "index"))
    proj = tmp_path / "proj"
    proj.mkdir()
    first, second = _repo_with_two_commits(proj)

    # Then the decoy repo is untouched: its tracked file is not staged as deleted
    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=decoy,
        capture_output=True,
        text=True,
        check=True,
        env=git_env,
    )
    assert status.stdout.strip() == ""
    # And the builder's own two commits landed in the tmp project, not the decoy
    assert first != second
    log = subprocess.run(
        ["git", "-C", str(proj), "log", "--oneline"],
        capture_output=True,
        text=True,
        check=True,
        env=git_env,
    )
    assert log.stdout.count("\n") == 2


@pytest.mark.parametrize("source", ["compact", "resume"])
def test_sessionstart_keeps_the_base_commit_on_a_continuation(source: str, tmp_path: Path) -> None:
    """Verify a compact or resume leaves the recorded start alone; the session is already logged."""
    # Given a session whose starting commit is on record and work committed since
    proj = tmp_path / "proj"
    proj.mkdir()
    first, second = _repo_with_two_commits(proj)
    store = _store(tmp_path, proj)
    store.state_dir.mkdir(parents=True, exist_ok=True)
    store.save_base_commit(first)
    # When SessionStart fires again for that same session
    proc = _run("sessionstart", {"cwd": str(proj), "source": source}, _isolated_env(tmp_path, proj))
    # Then the original start is kept, so the log still spans the whole session
    assert proc.returncode == 0, proc.stderr
    assert store.read_base_commit() == first
    assert store.read_base_commit() != second


def test_sessionstart_records_state_with_inject_disabled(tmp_path: Path) -> None:
    """Verify the sweep's state is captured even when memory injection is turned off."""
    # Given a project config turning inject off
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "sessionmemory.toml").write_text(
        "[inject]\nenabled = false\n", encoding="utf-8"
    )
    _repo_with_two_commits(proj)
    # When SessionStart runs
    payload = {"cwd": str(proj), "transcript_path": "/tmp/x/t.jsonl", "source": "startup"}  # noqa: S108
    proc = _run("sessionstart", payload, _isolated_env(tmp_path, proj))
    # Then the sweep still finds its transcript pointer and starting commit
    assert proc.returncode == 0, proc.stderr
    store = _store(tmp_path, proj)
    assert store.read_transcript_pointer() == "/tmp/x/t.jsonl"  # noqa: S108
    assert store.read_base_commit() != ""


def test_sessionstart_skips_state_when_the_sweep_is_disabled(tmp_path: Path) -> None:
    """Verify a disabled sweep records nothing; the state exists only to feed it."""
    # Given a project config turning the sweep off
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "sessionmemory.toml").write_text(
        "[sweep]\nenabled = false\n", encoding="utf-8"
    )
    # When SessionStart runs
    payload = {"cwd": str(proj), "transcript_path": "/tmp/x/t.jsonl", "source": "startup"}  # noqa: S108
    proc = _run("sessionstart", payload, _isolated_env(tmp_path, proj))
    # Then nothing was written for a sweep that will never run
    assert proc.returncode == 0, proc.stderr
    assert _store(tmp_path, proj).read_transcript_pointer() == ""


def test_sessionstart_saves_transcript_pointer(tmp_path: Path) -> None:
    """Verify SessionStart persists the transcript path to the state dir for the sweep."""
    # Given an isolated store and a transcript path on the event
    proj = tmp_path / "proj"
    proj.mkdir()
    # When SessionStart runs
    _run(
        "sessionstart",
        {"cwd": str(proj), "transcript_path": "/tmp/x/t.jsonl", "source": "startup"},  # noqa: S108
        _isolated_env(tmp_path, proj),
    )
    # Then the pointer file holds that path
    store = Store.for_cwd(
        cwd=proj, env={"XDG_STATE_HOME": str(tmp_path / "state"), "CLAUDE_PROJECT_DIR": str(proj)}
    )
    assert store.read_transcript_pointer() == "/tmp/x/t.jsonl"  # noqa: S108


def test_sessionstart_commits_the_vault(tmp_path: Path) -> None:
    """Verify a dirty vault is committed on session start."""
    # Given a registered vault holding one uncommitted page, under git
    proj = tmp_path / "proj"
    proj.mkdir()
    vault = _fake_vault(tmp_path, proj)
    subprocess.run(["git", "init", "-q", "."], cwd=vault, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.org"], cwd=vault, check=True, capture_output=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=vault, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "init"],
        cwd=vault,
        check=True,
        capture_output=True,
    )
    env = _isolated_env(tmp_path, proj, vault=vault)
    # When SessionStart runs
    _run("sessionstart", {"cwd": str(proj), "source": "startup"}, env)
    # Then the outstanding page landed in a checkpoint commit
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=vault, capture_output=True, text=True, check=True
    )
    assert "checkpoint" in log.stdout


def test_sessionstart_disabled_inject_exits_silently(tmp_path: Path) -> None:
    """Verify a config with inject disabled suppresses injection even on a seeded store."""
    # Given a seeded store and a project config turning inject off
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "sessionmemory.toml").write_text(
        "[inject]\nenabled = false\n", encoding="utf-8"
    )
    vault = _fake_vault(tmp_path, proj)
    # When SessionStart runs
    proc = _run(
        "sessionstart",
        {"cwd": str(proj), "source": "startup"},
        _isolated_env(tmp_path, proj, vault=vault),
    )
    # Then nothing is injected
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""


@pytest.fixture
def hook_payload(tmp_path: Path) -> dict:
    """A minimal startup payload for a fresh, non-git project directory."""
    proj = tmp_path / "proj"
    proj.mkdir()
    return {"cwd": str(proj), "source": "startup"}


@pytest.fixture
def registered_vault(tmp_path: Path, hook_payload: dict) -> Path:
    """A vault with the payload's project already registered."""
    return _fake_vault(tmp_path, Path(hook_payload["cwd"]))


@pytest.fixture
def unregistered_vault(tmp_path: Path, hook_payload: dict) -> Path:
    """An initialized vault that has never registered the payload's project."""
    root = tmp_path / "vault-root"
    initialize(root)
    return root


@pytest.fixture
def run_sessionstart(tmp_path: Path, request: pytest.FixtureRequest):
    """Run SessionStart against whichever vault fixture the test also requested."""

    def _run_it(payload: dict) -> dict:
        proj = Path(payload["cwd"])
        vault = None
        for name in ("registered_vault", "unregistered_vault"):
            if name in request.fixturenames:
                vault = request.getfixturevalue(name)
                break
        proc = _run("sessionstart", payload, _isolated_env(tmp_path, proj, vault=vault))
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    return _run_it


def _git_init(proj: Path) -> None:
    """Turn `proj` into an empty git working tree, which is all registration needs."""
    subprocess.run(
        ["git", "init", "-q"], cwd=proj, check=True, capture_output=True, env=clean_environ()
    )


def test_an_unregistered_repository_is_registered_at_session_start(
    unregistered_vault: Path, hook_payload: dict, run_sessionstart
) -> None:
    """Verify a git working tree the vault has never seen is registered before injection."""
    # Given an unregistered git repository and a reachable, initialized vault
    proj = Path(hook_payload["cwd"])
    _git_init(proj)
    # When SessionStart runs
    output = run_sessionstart(hook_payload)
    # Then the vault holds the repository under its derived slug
    entry = registry.load(unregistered_vault)[proj.name]
    assert entry.root == str(proj.resolve())
    # And the session is told the slug, then given the guidance, never the hint
    context = output["hookSpecificOutput"]["additionalContext"]
    assert f"'{proj.name}'" in context
    assert "## Using this vault" in context
    assert "sessionmemory project --register" not in context


def test_a_directory_outside_git_is_told_how_to_register(
    unregistered_vault: Path, hook_payload: dict, run_sessionstart
) -> None:
    """Verify a plain directory is never registered on its own, only told the command."""
    # Given an unregistered directory that is not a git working tree
    # (fixtures above)
    # When SessionStart runs
    output = run_sessionstart(hook_payload)
    # Then nothing was registered, and the injected context names the command that would
    assert registry.load(unregistered_vault) == {}
    assert "sessionmemory project --register" in output["hookSpecificOutput"]["additionalContext"]


def test_a_registration_the_cli_refuses_falls_back_to_the_hint(
    unregistered_vault: Path, hook_payload: dict, run_sessionstart, tmp_path: Path
) -> None:
    """Verify a refused registration leaves the person the command, not a silent start."""
    # Given a repository whose derived slug is already taken by another root
    proj = Path(hook_payload["cwd"])
    _git_init(proj)
    taken = tmp_path / "elsewhere"
    taken.mkdir()
    subprocess.run(
        [
            str(HOOKS.parent / "bin" / "sessionmemory"),
            "project",
            "--register",
            "--cwd",
            str(taken),
            "--slug",
            proj.name,
        ],
        env={**clean_environ(also_drop=_AMBIENT), ROOT_ENV: str(unregistered_vault)},
        check=True,
        capture_output=True,
    )
    # When SessionStart runs
    output = run_sessionstart(hook_payload)
    # Then the registry is unchanged and the hint is injected
    assert registry.load(unregistered_vault)[proj.name].root == str(taken.resolve())
    assert "sessionmemory project --register" in output["hookSpecificOutput"]["additionalContext"]


def test_a_registered_project_gets_no_hint(
    registered_vault: Path, hook_payload: dict, run_sessionstart
) -> None:
    """Verify a registered project's memory block never carries the registration hint."""
    # Given a project already registered with the vault
    # (fixtures above)
    # When SessionStart runs
    output = run_sessionstart(hook_payload)
    # Then no registration hint is present
    assert (
        "sessionmemory project --register" not in output["hookSpecificOutput"]["additionalContext"]
    )


def _fake_vault_cli(
    *,
    memory: str,
    resolution: dict | None,
    registers_as: str | None = None,
) -> type:
    """A VaultCLI stand-in whose resolve, register, and inject outcomes are fixed.

    The real vault always renders something once a project is registered, so
    it cannot exercise the branches where inject comes back empty on a
    registered project, or where the CLI cannot answer at all. This fake
    decouples the three so each branch in `_memory_block` gets a direct test.
    """

    class _FakeVaultCLI:
        cli = Path("/fake/bin/sessionmemory")
        command = str(cli)
        registered_cwds: ClassVar[list[Path]] = []

        @classmethod
        def discover(
            cls,
            *,
            env: dict,  # noqa: ARG003
            configured: str | None = None,  # noqa: ARG003
        ) -> _FakeVaultCLI:
            return cls()

        def resolve(self, *, cwd: Path, env: dict) -> dict | None:
            return resolution

        def register(self, *, cwd: Path, env: dict) -> str | None:
            self.registered_cwds.append(cwd)
            return registers_as

        def inject(self, *, cwd: Path, env: dict) -> str:
            return memory

    return _FakeVaultCLI


def test_memory_block_none_when_inject_is_disabled(tmp_path: Path) -> None:
    """Verify a disabled inject config short-circuits before any vault is discovered."""
    # Given inject disabled
    cfg = SessionMemoryConfig(inject_enabled=False)
    # When the memory block is built
    result = sessionstart._memory_block(cfg, cwd=tmp_path)
    # Then there is nothing to say
    assert result is None


def test_memory_block_hints_when_no_vault_is_discovered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify no vault reachable yields a hint naming both ways to configure one."""
    # Given a VaultCLI.discover that finds nothing
    monkeypatch.setattr(sessionstart.VaultCLI, "discover", classmethod(lambda cls, **_kwargs: None))
    # When the memory block is built
    result = sessionstart._memory_block(SessionMemoryConfig(), cwd=tmp_path)
    # Then the session is told nothing will be recorded and how to point the hooks at a vault
    assert result == sessionstart.NO_VAULT_HINT
    assert "SESSIONMEMORY_VAULT" in result
    assert "[vault]" in result
    assert "~/.claude/sessionmemory.toml" in result


def test_memory_block_none_when_inject_is_off_and_no_vault_is_discovered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify the no-vault hint follows the inject toggle like every other block."""
    # Given injection turned off and no vault reachable
    monkeypatch.setattr(sessionstart.VaultCLI, "discover", classmethod(lambda cls, **_kwargs: None))
    # When the memory block is built
    result = sessionstart._memory_block(SessionMemoryConfig(inject_enabled=False), cwd=tmp_path)
    # Then there is nothing to say
    assert result is None


def test_memory_block_omits_the_hint_for_a_registered_project_with_nothing_to_say(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify a registered project whose inject comes back empty gets no hint."""
    # Given a vault that reports the project registered but has nothing to inject
    fake_cli = _fake_vault_cli(memory="", resolution={"registered": True, "repo_root": "/r"})
    monkeypatch.setattr(sessionstart, "VaultCLI", fake_cli)
    # When the memory block is built
    result = sessionstart._memory_block(SessionMemoryConfig(), cwd=tmp_path)
    # Then there is nothing to say, not a wrongful registration hint
    assert result is None
    assert fake_cli.registered_cwds == []


def test_memory_block_hints_for_an_unregistered_directory_outside_git(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify a plain directory is told the command rather than registered."""
    # Given a vault that reports the directory unregistered and outside any repository
    fake_cli = _fake_vault_cli(memory="", resolution={"registered": False, "repo_root": None})
    monkeypatch.setattr(sessionstart, "VaultCLI", fake_cli)
    # When the memory block is built
    result = sessionstart._memory_block(SessionMemoryConfig(), cwd=tmp_path)
    # Then the registration hint names the absolute CLI path, not a bare `sessionmemory`
    # that a plugin-only install has nowhere on PATH to resolve, and nothing was registered
    assert result == sessionstart._unregistered_hint(fake_cli())
    assert str(fake_cli.cli) in result
    assert fake_cli.registered_cwds == []


def test_memory_block_hints_when_the_cli_cannot_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify an unreadable `project` answer is treated as unregistered, never as a repository."""
    # Given a vault whose CLI could not resolve the directory at all
    fake_cli = _fake_vault_cli(memory="", resolution=None)
    monkeypatch.setattr(sessionstart, "VaultCLI", fake_cli)
    # When the memory block is built
    result = sessionstart._memory_block(SessionMemoryConfig(), cwd=tmp_path)
    # Then the hint is given and no registration was attempted
    assert result == sessionstart._unregistered_hint(fake_cli())
    assert fake_cli.registered_cwds == []


def test_memory_block_registers_a_repository_and_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify an unregistered repository is registered for `cwd` and the slug is reported."""
    # Given a vault that reports an unregistered git working tree
    fake_cli = _fake_vault_cli(
        memory="## Guides", resolution={"registered": False, "repo_root": "/r"}, registers_as="r"
    )
    monkeypatch.setattr(sessionstart, "VaultCLI", fake_cli)
    # When the memory block is built
    result = sessionstart._memory_block(SessionMemoryConfig(), cwd=tmp_path)
    # Then the directory was registered, and the block leads with the slug before the memory
    assert fake_cli.registered_cwds == [tmp_path]
    assert result is not None
    assert result.index("'r'") < result.index("## Guides")
    assert result.endswith(sessionstart.SKILL_POINTER)


def test_memory_block_reports_a_registration_even_when_inject_is_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify the registration is reported on its own when the vault renders nothing after it."""
    # Given a registration that succeeds and an inject that comes back empty
    fake_cli = _fake_vault_cli(
        memory="", resolution={"registered": False, "repo_root": "/r"}, registers_as="r"
    )
    monkeypatch.setattr(sessionstart, "VaultCLI", fake_cli)
    # When the memory block is built
    result = sessionstart._memory_block(SessionMemoryConfig(), cwd=tmp_path)
    # Then the block is the registration line alone
    assert result == sessionstart._registered_note("r")


def test_memory_block_hints_when_registration_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify a refused registration falls back to naming the command."""
    # Given a repository the CLI refuses to register
    fake_cli = _fake_vault_cli(
        memory="", resolution={"registered": False, "repo_root": "/r"}, registers_as=None
    )
    monkeypatch.setattr(sessionstart, "VaultCLI", fake_cli)
    # When the memory block is built
    result = sessionstart._memory_block(SessionMemoryConfig(), cwd=tmp_path)
    # Then the hint is the whole answer
    assert result == sessionstart._unregistered_hint(fake_cli())


# ---------------------------------------------------------------------------
# SessionStart - handoff consumption
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source", ["compact", "clear", "startup"])
def test_sessionstart_consumes_handoff_on_fresh_start(source: str, tmp_path: Path) -> None:
    """Verify a handoff is injected and then deleted on compact/clear/startup."""
    # Given a project with a pending handoff
    proj = tmp_path / "proj"
    proj.mkdir()
    store = _seed_handoff(tmp_path, proj)
    # When SessionStart runs from a fresh-context source
    proc = _run("sessionstart", {"cwd": str(proj), "source": source}, _isolated_env(tmp_path, proj))
    # Then the baton is injected and the file is consumed
    assert proc.returncode == 0, proc.stderr
    context = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "the baton" in context
    assert not store.handoff_path.exists()


def test_sessionstart_consumes_handoff_on_unknown_source(tmp_path: Path) -> None:
    """Verify any non-resume source consumes the handoff (denylist, not allowlist)."""
    # Given a pending handoff and a source string the hook does not enumerate
    proj = tmp_path / "proj"
    proj.mkdir()
    store = _seed_handoff(tmp_path, proj)
    # When SessionStart runs from an unknown future start source
    proc = _run(
        "sessionstart",
        {"cwd": str(proj), "source": "some-new-source"},
        _isolated_env(tmp_path, proj),
    )
    # Then the baton is still injected and consumed rather than stranded
    assert proc.returncode == 0, proc.stderr
    context = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "the baton" in context
    assert not store.handoff_path.exists()


def test_sessionstart_skips_handoff_on_resume(tmp_path: Path) -> None:
    """Verify resume neither injects nor deletes the handoff (same session may own it)."""
    # Given a project with a pending handoff
    proj = tmp_path / "proj"
    proj.mkdir()
    store = _seed_handoff(tmp_path, proj)
    # When SessionStart runs from a resume
    proc = _run(
        "sessionstart", {"cwd": str(proj), "source": "resume"}, _isolated_env(tmp_path, proj)
    )
    # Then the handoff is neither injected nor consumed
    assert proc.returncode == 0, proc.stderr
    assert "the baton" not in proc.stdout
    assert store.handoff_path.exists()


def test_sessionstart_consumes_handoff_when_inject_disabled(tmp_path: Path) -> None:
    """Verify the handoff is carried even when memory injection is disabled."""
    # Given a pending handoff and a project config turning inject off
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "sessionmemory.toml").write_text(
        "[inject]\nenabled = false\n", encoding="utf-8"
    )
    store = _seed_handoff(tmp_path, proj)
    # When SessionStart runs
    proc = _run(
        "sessionstart", {"cwd": str(proj), "source": "startup"}, _isolated_env(tmp_path, proj)
    )
    # Then the explicit user artifact is still injected and consumed
    assert proc.returncode == 0, proc.stderr
    context = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "the baton" in context
    assert not store.handoff_path.exists()


def test_sessionstart_handoff_precedes_memory(tmp_path: Path) -> None:
    """Verify the handoff block is emitted ahead of the memory block."""
    # Given a project with both a handoff and vault memory
    proj = tmp_path / "proj"
    proj.mkdir()
    _seed_handoff(tmp_path, proj)
    vault = _fake_vault(tmp_path, proj)
    # When SessionStart runs
    proc = _run(
        "sessionstart",
        {"cwd": str(proj), "source": "startup"},
        _isolated_env(tmp_path, proj, vault=vault),
    )
    # Then both appear, handoff first
    assert proc.returncode == 0, proc.stderr
    context = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert context.index("the baton") < context.index(TITLE)


def test_sessionstart_keeps_handoff_when_emit_fails(tmp_path: Path) -> None:
    """Verify a failed inject leaves the handoff in place (delete only after a clean emit)."""
    # Given a pending handoff and a stdout that cannot be written (read end of a pipe)
    proj = tmp_path / "proj"
    proj.mkdir()
    store = _seed_handoff(tmp_path, proj)
    read_fd, write_fd = os.pipe()
    base = clean_environ(also_drop=_AMBIENT)
    try:
        # When SessionStart tries to emit to an unwritable fd, the flush raises
        proc = subprocess.run(
            [str(HOOKS / "sessionstart.py")],
            input=json.dumps({"cwd": str(proj), "source": "startup"}),
            stdout=read_fd,  # read end is not writable -> os.write fails with EBADF
            stderr=subprocess.PIPE,
            text=True,
            env={**base, **_isolated_env(tmp_path, proj)},
            check=False,
            timeout=30,
        )
    finally:
        os.close(read_fd)
        os.close(write_fd)
    # Then the hook still fails open (exit 0) and the baton survives for a retry
    assert proc.returncode == 0, proc.stderr
    assert store.handoff_path.exists()


# ---------------------------------------------------------------------------
# SessionEnd / PreCompact (sweep) - never spawns a real worker
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stage", ["sessionend", "precompact"])
def test_sweep_headless_guard_short_circuits(stage: str, tmp_path: Path) -> None:
    """Verify the headless guard makes the sweep scripts exit 0 silently with no side effects."""
    # Given the headless guard set
    proj = tmp_path / "proj"
    proj.mkdir()
    env = {**_isolated_env(tmp_path, proj), "SESSIONMEMORY_HEADLESS": "1"}
    # When the sweep script runs
    proc = _run(stage, {"cwd": str(proj)}, env)
    # Then it exits cleanly, emits nothing, and writes no sweep.log
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""
    assert not list((tmp_path / "state").rglob("sweep.log"))


@pytest.mark.parametrize("stage", ["sessionend", "precompact"])
def test_sweep_below_threshold_does_not_spawn(stage: str, tmp_path: Path) -> None:
    """Verify a below-threshold transcript exits 0 without spawning a worker."""
    # Given no transcript (0 meaningful exchanges) and no headless guard
    proj = tmp_path / "proj"
    proj.mkdir()
    # When the sweep script runs
    proc = _run(stage, {"cwd": str(proj), "transcript_path": ""}, _isolated_env(tmp_path, proj))
    # Then it exits cleanly and no sweep.log is written (gate returned None)
    assert proc.returncode == 0, proc.stderr
    assert not list((tmp_path / "state").rglob("sweep.log"))


@pytest.mark.parametrize("stage", ["sessionend", "precompact"])
def test_sweep_disabled_exits_without_gating(stage: str, tmp_path: Path) -> None:
    """Verify a config with sweep disabled exits 0 before any gating."""
    # Given a project config turning the sweep off
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "sessionmemory.toml").write_text(
        "[sweep]\nenabled = false\n", encoding="utf-8"
    )
    # When the sweep script runs
    proc = _run(stage, {"cwd": str(proj), "transcript_path": ""}, _isolated_env(tmp_path, proj))
    # Then it exits cleanly with no side effects
    assert proc.returncode == 0, proc.stderr
    assert not list((tmp_path / "state").rglob("sweep.log"))


def test_sessionend_commits_the_vault(tmp_path: Path) -> None:
    """Verify a dirty vault is committed on session end, even when no sweep fires."""
    # Given a registered vault holding one uncommitted page, under git
    proj = tmp_path / "proj"
    proj.mkdir()
    vault = _fake_vault(tmp_path, proj)
    subprocess.run(["git", "init", "-q", "."], cwd=vault, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.org"], cwd=vault, check=True, capture_output=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=vault, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "init"],
        cwd=vault,
        check=True,
        capture_output=True,
    )
    env = _isolated_env(tmp_path, proj, vault=vault)
    # When SessionEnd runs with a below-threshold transcript, so no sweep spawns
    proc = _run("sessionend", {"cwd": str(proj), "transcript_path": ""}, env)
    # Then the outstanding page still landed in a checkpoint commit
    assert proc.returncode == 0, proc.stderr
    assert not list((tmp_path / "state").rglob("sweep.log"))
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=vault, capture_output=True, text=True, check=True
    )
    assert "checkpoint" in log.stdout


def test_sessionend_commits_when_the_sweep_is_disabled(tmp_path: Path) -> None:
    """Verify the checkpoint still lands when the sweep itself is turned off."""
    # Given a registered vault holding one uncommitted page, under git, with the
    # sweep disabled for this project
    proj = tmp_path / "proj"
    proj.mkdir()
    vault = _fake_vault(tmp_path, proj)
    subprocess.run(["git", "init", "-q", "."], cwd=vault, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.org"], cwd=vault, check=True, capture_output=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=vault, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "init"],
        cwd=vault,
        check=True,
        capture_output=True,
    )
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "sessionmemory.toml").write_text(
        "[sweep]\nenabled = false\n", encoding="utf-8"
    )
    env = _isolated_env(tmp_path, proj, vault=vault)
    # When SessionEnd runs with the sweep disabled
    proc = _run("sessionend", {"cwd": str(proj), "transcript_path": ""}, env)
    # Then no sweep ran, but the outstanding page still landed in a checkpoint commit
    assert proc.returncode == 0, proc.stderr
    assert not list((tmp_path / "state").rglob("sweep.log"))
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=vault, capture_output=True, text=True, check=True
    )
    assert "checkpoint" in log.stdout


def test_sessionend_skips_the_commit_while_a_sweep_worker_holds_a_fresh_lock(
    tmp_path: Path,
) -> None:
    """Verify SessionEnd defers to a sweep worker still writing rather than racing it."""
    # Given a registered vault holding one uncommitted page, under git, and a
    # fresh sweep-worker lock for this project
    proj = tmp_path / "proj"
    proj.mkdir()
    vault = _fake_vault(tmp_path, proj)
    subprocess.run(["git", "init", "-q", "."], cwd=vault, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.org"], cwd=vault, check=True, capture_output=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=vault, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "init"],
        cwd=vault,
        check=True,
        capture_output=True,
    )
    store = _store(tmp_path, proj)
    store.state_dir.mkdir(parents=True, exist_ok=True)
    store.lock_path.write_text(str(time.time()), encoding="utf-8")
    env = _isolated_env(tmp_path, proj, vault=vault)
    # When SessionEnd runs with a below-threshold transcript, so no sweep spawns
    proc = _run("sessionend", {"cwd": str(proj), "transcript_path": ""}, env)
    # Then the commit is skipped, leaving only the vault's initial commit
    assert proc.returncode == 0, proc.stderr
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=vault, capture_output=True, text=True, check=True
    )
    assert len(log.stdout.strip().splitlines()) == 1
    assert "checkpoint" not in log.stdout


def test_memory_block_names_the_skill_that_carries_the_rest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify a session with memory is also told where the full command surface is."""
    # Given a vault with something to inject
    monkeypatch.setattr(
        sessionstart,
        "VaultCLI",
        _fake_vault_cli(memory="## Guides", resolution={"registered": True}),
    )

    # When the memory block is built
    result = sessionstart._memory_block(SessionMemoryConfig(), cwd=tmp_path)

    # Then the block carries the vault's own text and the pointer this layer adds
    assert result is not None
    assert result.startswith("## Guides")
    assert "cli" in result


def test_memory_block_omits_the_skill_pointer_when_there_is_no_memory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify the guide pointer never rides along with the registration hint.

    `SKILL_POINTER` is added only once a project actually has memory to point at;
    an unregistered project has nothing for it to describe, so it must not appear
    alongside the registration hint.
    """
    # Given an unregistered project, whose one useful instruction is how to register
    monkeypatch.setattr(
        sessionstart,
        "VaultCLI",
        _fake_vault_cli(memory="", resolution={"registered": False, "repo_root": None}),
    )

    # When the memory block is built
    result = sessionstart._memory_block(SessionMemoryConfig(), cwd=tmp_path)

    # Then it names registering and nothing else
    assert result is not None
    assert "register" in result
    assert sessionstart.SKILL_POINTER not in result


# ---------------------------------------------------------------------------
# _write_all / _record_session_state (module-private, exercised directly)
# ---------------------------------------------------------------------------


def test_write_all_writes_the_whole_payload_across_short_writes(tmp_path: Path) -> None:
    """Verify a payload larger than one os.write can accept is written in full.

    A background thread drains the pipe concurrently, since a write larger
    than the pipe's buffer would otherwise block `_write_all` forever waiting
    for a reader that only starts once the write returns.
    """
    # Given a pipe and a payload larger than its buffer, with a background
    # thread draining the read end concurrently
    read_fd, write_fd = os.pipe()
    payload = b"x" * 5_000_000  # comfortably larger than a pipe's buffer
    received = bytearray()

    def _drain() -> None:
        while chunk := os.read(read_fd, 65536):
            received.extend(chunk)

    reader = threading.Thread(target=_drain)
    reader.start()

    # When writing the whole payload
    try:
        sessionstart._write_all(write_fd, payload)
    finally:
        os.close(write_fd)
    reader.join(timeout=10)
    os.close(read_fd)

    # Then every byte arrived on the other end
    assert not reader.is_alive()
    assert bytes(received) == payload


def test_record_session_state_saves_the_transcript_pointer(tmp_path: Path) -> None:
    """Verify a fresh session's transcript path is recorded for the sweep to find."""
    # Given a store and a payload carrying a transcript path
    store = Store(key="k", state_dir=tmp_path / "state")
    payload = {"transcript_path": "/tmp/x/session.jsonl", "source": "startup"}  # noqa: S108

    # When recording the session state
    sessionstart._record_session_state(store, payload=payload, cwd=tmp_path)

    # Then the transcript path is saved for the sweep to find
    assert store.read_transcript_pointer() == "/tmp/x/session.jsonl"  # noqa: S108


def test_record_session_state_records_the_base_commit_on_a_fresh_start(tmp_path: Path) -> None:
    """Verify a non-continuation start records HEAD as the session's starting commit."""
    # Given a repo with a commit, and a payload whose source is not a continuation
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-qm", "x"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    store = Store(key="k", state_dir=tmp_path / "state")

    # When recording the session state
    sessionstart._record_session_state(store, payload={"source": "startup"}, cwd=tmp_path)

    # Then the starting commit is recorded
    assert store.read_base_commit() != ""


def test_record_session_state_skips_the_base_commit_on_a_continuation(tmp_path: Path) -> None:
    """Verify a resumed session never overwrites the commit the original start recorded."""
    # Given a repo with a commit and a store already recording an earlier base commit
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-qm", "x"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    store = Store(key="k", state_dir=tmp_path / "state")
    store.save_base_commit("original-commit")

    # When recording session state for a "resume" source
    sessionstart._record_session_state(store, payload={"source": "resume"}, cwd=tmp_path)

    # Then the original base commit is left untouched
    assert store.read_base_commit() == "original-commit"
