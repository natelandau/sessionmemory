"""Tests for `sessionmemory init`.

`init` is the one command that must work before `SESSIONMEMORY_VAULT` names an
initialized vault, so most tests here pass the directory explicitly rather than relying
on the shared `vault` fixture. The last tests prove the flip side: once `require_vault`
is tightened, every other command refuses an uninitialized directory.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from sessionmemory.cli import app

runner = CliRunner()


def test_init_in_an_empty_directory_exits_zero_and_reports_what_it_created(tmp_path):
    """Report the files it created in a fresh directory."""
    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 0
    assert "vault.toml" in result.output
    assert (tmp_path / "_system" / "vault.toml").is_file()


def test_a_second_init_exits_zero_and_reports_everything_already_existed(tmp_path):
    """Re-running init is safe and reports the idempotent case."""
    runner.invoke(app, ["init", str(tmp_path)])
    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 0
    assert "already" in result.output


def test_init_in_a_non_empty_non_vault_directory_exits_one(tmp_path):
    """Refuse a directory holding unrelated files without --force."""
    (tmp_path / "some-file.txt").write_text("x", encoding="utf-8")
    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 1
    assert "--force" in result.output


def test_init_with_force_in_that_directory_exits_zero(tmp_path):
    """Accept a non-empty non-vault directory when --force is given."""
    (tmp_path / "some-file.txt").write_text("x", encoding="utf-8")
    result = runner.invoke(app, ["init", str(tmp_path), "--force"])
    assert result.exit_code == 0
    assert (tmp_path / "_system" / "vault.toml").is_file()


def test_init_without_a_directory_argument_uses_the_environment_variable(tmp_path, monkeypatch):
    """Read SESSIONMEMORY_VAULT directly rather than through require_vault.

    require_vault refuses an uninitialized directory, and init is the command that
    fixes that, so init must not route through it.
    """
    monkeypatch.setenv("SESSIONMEMORY_VAULT", str(tmp_path))
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    assert (tmp_path / "_system" / "vault.toml").is_file()


def test_init_without_a_directory_reports_a_clean_error_for_a_missing_vault_directory(
    tmp_path, monkeypatch
):
    """Report the missing directory instead of a traceback when init reads the environment."""
    monkeypatch.setenv("SESSIONMEMORY_VAULT", str(tmp_path / "nope"))
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "does not exist" in result.output


def test_init_creates_the_directory_when_it_does_not_exist(tmp_path):
    """Create the target directory itself, since init is the first command run."""
    target = tmp_path / "brand-new-vault"
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 0
    assert (target / "_system" / "vault.toml").is_file()


def test_every_other_command_refuses_an_uninitialized_vault(tmp_path, monkeypatch):
    """Refuse to run against a directory that exists but was never initialized."""
    monkeypatch.setenv("SESSIONMEMORY_VAULT", str(tmp_path))
    result = runner.invoke(app, ["project"])
    assert result.exit_code == 1
    assert "run: sessionmemory init" in result.output


def test_require_vault_still_uses_the_plain_message_for_an_empty_directory(tmp_path, monkeypatch):
    """Keep the safe, agent-actionable `run: sessionmemory init` for the ordinary bootstrap case."""
    monkeypatch.setenv("SESSIONMEMORY_VAULT", str(tmp_path))

    result = runner.invoke(app, ["project"])

    assert result.exit_code == 1
    assert "run: sessionmemory init" in result.output


def test_require_vault_names_force_without_the_run_imperative_for_a_non_empty_directory(
    tmp_path, monkeypatch
):
    """Describe the --force fix without phrasing it as an instruction for the caller to run.

    `sessionmemory init` is a human bootstrap command that takes a directory and runs once, and
    nothing should hand an agent a `run:` imperative that could scaffold a vault into
    whatever directory it happens to be pointed at. The message must still name
    `--force` so a human reading it knows what fixes the directory, but it must not
    carry the `run:` prefix that means "you, the caller, should do this next"
    elsewhere in this codebase, such as in `run: sessionmemory project --register`.
    """
    monkeypatch.setenv("SESSIONMEMORY_VAULT", str(tmp_path))
    (tmp_path / "a-note.md").write_text(
        "---\ntitle: pre-existing\n---\n\nBody.\n", encoding="utf-8"
    )

    result = runner.invoke(app, ["project"])

    assert result.exit_code == 1
    assert "--force" in result.output
    assert "run:" not in result.output


def test_force_recovers_a_directory_holding_a_note_and_no_marker(tmp_path, monkeypatch):
    """Walk the full arc: a human runs --force by hand, and the existing note survives.

    A directory a human has already been dropping notes into, with no `sessionmemory init`
    ever run, is exactly the population this migration step is for. This proves
    `--force` fixes it and that doing so preserves the existing note byte for byte.
    """
    monkeypatch.setenv("SESSIONMEMORY_VAULT", str(tmp_path))
    note = tmp_path / "a-note.md"
    original_content = "---\ntitle: pre-existing\n---\n\nBody.\n"
    note.write_text(original_content, encoding="utf-8")

    blocked = runner.invoke(app, ["project"])
    assert blocked.exit_code == 1

    result = runner.invoke(app, ["init", str(tmp_path), "--force"])

    assert result.exit_code == 0
    assert (tmp_path / "_system" / "vault.toml").is_file()
    assert note.read_text(encoding="utf-8") == original_content


def test_init_with_a_relative_path_records_an_absolute_vault(tmp_path, monkeypatch):
    """The recorded vault has to name the same directory from any working directory."""
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init", "myvault", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["vault"] == str((tmp_path / "myvault").resolve())
    assert (tmp_path / "myvault" / "_system" / "vault.toml").is_file()


def test_init_through_a_symlink_records_the_resolved_target(tmp_path):
    """Every other reader resolves symlinks, so the recorded vault must resolve too."""
    real = tmp_path / "real-vault"
    real.mkdir()
    link = tmp_path / "linked-vault"
    link.symlink_to(real)

    result = runner.invoke(app, ["init", str(link), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["vault"] == str(real.resolve())
