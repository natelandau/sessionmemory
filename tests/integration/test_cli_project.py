"""Tests for the project command."""

from __future__ import annotations

import json
import subprocess

import pytest
from typer.testing import CliRunner

from sessionmemory.cli import app
from sessionmemory.lib import paths as paths_lib

runner = CliRunner()
REMOTE = "ssh://git@gitea.example.org:2222/nate/demo.git"


def _run(*args: str, cwd) -> None:
    """Run a git command, failing the test on a non-zero exit."""
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """Build a git repository with a remote."""
    root = tmp_path / "demo"
    root.mkdir()
    _run("init", "-q", ".", cwd=root)
    _run("commit", "-q", "--allow-empty", "-m", "init", cwd=root)
    _run("remote", "add", "origin", REMOTE, cwd=root)
    return root


def test_project_register_then_view(vault, repo):
    """Register a project, then resolve it by remote."""
    registered = runner.invoke(app, ["project", "--register", "--cwd", str(repo)])
    assert registered.exit_code == 0

    result = runner.invoke(app, ["project", "--cwd", str(repo), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["slug"] == "demo"
    assert payload["registered"] is True
    assert payload["is_worktree"] is False
    assert payload["paths"] == paths_lib.project_paths(vault, "demo")


def test_project_is_idempotent(vault, repo):
    """Refuse a second registration rather than creating a duplicate."""
    runner.invoke(app, ["project", "--register", "--cwd", str(repo)])
    second = runner.invoke(app, ["project", "--register", "--cwd", str(repo)])
    assert second.exit_code != 0
    # pp.error writes to stderr; .output is the combined stream a user would see.
    assert "already registered" in second.output


def test_project_accepts_an_explicit_slug(vault, repo):
    """Let the caller override the slug derived from the remote."""
    runner.invoke(app, ["project", "--register", "--cwd", str(repo), "--slug", "custom"])
    payload = json.loads(runner.invoke(app, ["project", "--cwd", str(repo), "--json"]).stdout)
    assert payload["slug"] == "custom"


def test_worktree_resolves_to_the_parent_project(vault, repo, tmp_path):
    """Resolve a worktree to the project its main checkout registered."""
    runner.invoke(app, ["project", "--register", "--cwd", str(repo)])

    worktree = tmp_path / "feature"
    _run("worktree", "add", "-q", str(worktree), "-b", "feature", cwd=repo)

    payload = json.loads(runner.invoke(app, ["project", "--cwd", str(worktree), "--json"]).stdout)
    assert payload["slug"] == "demo"
    assert payload["is_worktree"] is True


def test_project_prose_reports_registered_project(vault, repo):
    """Print a human-readable summary when --json is not requested."""
    runner.invoke(app, ["project", "--register", "--cwd", str(repo)])
    result = runner.invoke(app, ["project", "--cwd", str(repo)])
    assert result.exit_code == 0
    assert "demo" in result.output


def test_project_prose_reports_unregistered_directory(vault, repo):
    """Print a human-readable message when the directory is not registered."""
    result = runner.invoke(app, ["project", "--cwd", str(repo)])
    assert result.exit_code != 0
    assert "not a registered project" in result.output


def test_project_refuses_a_slug_already_in_use(vault, repo, tmp_path):
    """Refuse a colliding slug from an unrelated repository rather than overwrite it."""
    runner.invoke(app, ["project", "--register", "--cwd", str(repo)])

    other = tmp_path / "other"
    other.mkdir()
    _run("init", "-q", ".", cwd=other)
    _run("commit", "-q", "--allow-empty", "-m", "init", cwd=other)

    result = runner.invoke(app, ["project", "--register", "--cwd", str(other), "--slug", "demo"])
    assert result.exit_code != 0
    assert "already in use" in result.output


def test_project_falls_back_to_directory_name_without_a_remote(vault, tmp_path):
    """Derive the slug from the directory when there is no remote to read."""
    root = tmp_path / "no-remote-repo"
    root.mkdir()
    _run("init", "-q", ".", cwd=root)
    _run("commit", "-q", "--allow-empty", "-m", "init", cwd=root)

    result = runner.invoke(app, ["project", "--register", "--cwd", str(root)])
    assert result.exit_code == 0

    payload = json.loads(runner.invoke(app, ["project", "--cwd", str(root), "--json"]).stdout)
    assert payload["slug"] == "no-remote-repo"


def test_project_skips_a_remote_that_normalizes_to_no_name(vault, tmp_path):
    """Verify a remote that yields no name is skipped in favor of the next one that does."""
    # Given a repo whose first remote normalizes to "" (a bare "/" rsplits to no name)
    # and whose second remote yields a real one
    root = tmp_path / "demo"
    root.mkdir()
    _run("init", "-q", ".", cwd=root)
    _run("commit", "-q", "--allow-empty", "-m", "init", cwd=root)
    _run("remote", "add", "origin", "/", cwd=root)
    _run("remote", "add", "upstream", "https://example.org/team/other-repo.git", cwd=root)

    # When registering the project
    result = runner.invoke(app, ["project", "--register", "--cwd", str(root)])

    # Then registration succeeds and the slug is derived from the remote that yields a name
    assert result.exit_code == 0
    payload = json.loads(runner.invoke(app, ["project", "--cwd", str(root), "--json"]).stdout)
    assert payload["slug"] == "other-repo"


def test_project_register_and_view_a_directory_outside_git(vault, tmp_path):
    """Register a directory git does not know, keyed on the directory itself."""
    plain = tmp_path / "notes-dir"
    plain.mkdir()

    registered = runner.invoke(app, ["project", "--register", "--cwd", str(plain)])
    assert registered.exit_code == 0

    payload = json.loads(runner.invoke(app, ["project", "--cwd", str(plain), "--json"]).stdout)
    assert payload["slug"] == "notes-dir"
    assert payload["registered"] is True
    assert payload["repo_root"] is None


def test_project_from_a_subdirectory_of_a_directory_outside_git(vault, tmp_path):
    """Resolve from several levels down, since only the root is registered."""
    plain = tmp_path / "notes-dir"
    deep = plain / "a" / "b" / "c"
    deep.mkdir(parents=True)
    runner.invoke(app, ["project", "--register", "--cwd", str(plain)])

    payload = json.loads(runner.invoke(app, ["project", "--cwd", str(deep), "--json"]).stdout)
    assert payload["slug"] == "notes-dir"


def test_project_refuses_a_second_registration_of_the_same_directory(vault, tmp_path):
    """Refuse a directory outside git that is already registered."""
    plain = tmp_path / "notes-dir"
    plain.mkdir()
    runner.invoke(app, ["project", "--register", "--cwd", str(plain)])

    second = runner.invoke(app, ["project", "--register", "--cwd", str(plain)])
    assert second.exit_code != 0
    assert "already registered" in second.output


def test_the_deepest_registered_directory_wins(vault, tmp_path):
    """Resolve to the innermost registered project when two contain the directory."""
    outer = tmp_path / "outer"
    inner = outer / "nested" / "inner"
    inner.mkdir(parents=True)
    runner.invoke(app, ["project", "--register", "--cwd", str(outer)])
    runner.invoke(app, ["project", "--register", "--cwd", str(inner)])

    payload = json.loads(runner.invoke(app, ["project", "--cwd", str(inner), "--json"]).stdout)
    assert payload["slug"] == "inner"

    outer_payload = json.loads(
        runner.invoke(app, ["project", "--cwd", str(outer / "nested"), "--json"]).stdout
    )
    assert outer_payload["slug"] == "outer"


def test_a_repository_inside_a_registered_directory_resolves_as_itself(vault, tmp_path):
    """Keep git keys ahead of the prefix walk, so a nested repository keeps its own slug."""
    outer = tmp_path / "outer"
    nested = outer / "nested-repo"
    nested.mkdir(parents=True)
    _run("init", "-q", ".", cwd=nested)
    _run("commit", "-q", "--allow-empty", "-m", "init", cwd=nested)
    _run("remote", "add", "origin", "ssh://git@gitea.example.org/nate/nested.git", cwd=nested)

    runner.invoke(app, ["project", "--register", "--cwd", str(outer)])
    runner.invoke(app, ["project", "--register", "--cwd", str(nested)])

    payload = json.loads(runner.invoke(app, ["project", "--cwd", str(nested), "--json"]).stdout)
    assert payload["slug"] == "nested"


def test_an_unregistered_repository_is_never_absorbed_by_a_registered_ancestor(vault, tmp_path):
    """Report a fresh repository as unregistered, whatever registered directory contains it.

    A repository is its own project boundary. Absorbing it would file its notes under the
    parent directory's slug with nothing reported, and a slug is permanent once notes
    carry it.
    """
    outer = tmp_path / "outer"
    nested = outer / "nested-repo"
    nested.mkdir(parents=True)
    _run("init", "-q", ".", cwd=nested)
    _run("commit", "-q", "--allow-empty", "-m", "init", cwd=nested)

    runner.invoke(app, ["project", "--register", "--cwd", str(outer)])

    resolved = runner.invoke(app, ["project", "--cwd", str(nested), "--json"])
    assert resolved.exit_code != 0
    assert json.loads(resolved.stdout)["registered"] is False


def test_a_subdirectory_of_a_repository_is_never_absorbed_either(vault, tmp_path):
    """Keep the boundary at the repository, not at the directory the caller happens to be in."""
    outer = tmp_path / "outer"
    nested = outer / "nested-repo"
    deep = nested / "src" / "pkg"
    deep.mkdir(parents=True)
    _run("init", "-q", ".", cwd=nested)
    _run("commit", "-q", "--allow-empty", "-m", "init", cwd=nested)

    runner.invoke(app, ["project", "--register", "--cwd", str(outer)])

    resolved = runner.invoke(app, ["project", "--cwd", str(deep), "--json"])
    assert resolved.exit_code != 0
    assert json.loads(resolved.stdout)["registered"] is False


def test_project_refuses_a_bare_repository(vault, tmp_path):
    """Refuse a bare repository rather than register the directory that contains it."""
    container = tmp_path / "bareparent"
    container.mkdir()
    bare = container / "thing.git"
    _run("init", "--bare", "-q", str(bare), cwd=container)

    result = runner.invoke(app, ["project", "--register", "--cwd", str(bare)])
    assert result.exit_code != 0
    assert "bare repository" in result.output
    assert not (vault / "_system" / "registry.toml").exists()


def test_a_bare_repository_inside_a_registered_directory_reports_unregistered(vault, tmp_path):
    """Keep a bare repository outside the project its container registered."""
    outer = tmp_path / "outer"
    outer.mkdir()
    bare = outer / "bare.git"
    _run("init", "--bare", "-q", str(bare), cwd=outer)

    runner.invoke(app, ["project", "--register", "--cwd", str(outer)])

    resolved = runner.invoke(app, ["project", "--cwd", str(bare), "--json"])
    assert resolved.exit_code != 0
    assert json.loads(resolved.stdout)["registered"] is False


def test_project_refuses_a_path_that_does_not_exist(vault, tmp_path):
    """Refuse a mistyped --cwd instead of registering a directory nobody has."""
    result = runner.invoke(
        app, ["project", "--register", "--cwd", str(tmp_path / "does-not-exist-typo")]
    )
    assert result.exit_code != 0
    assert "not a directory" in result.output
    assert not (vault / "_system" / "registry.toml").exists()


def test_project_refuses_a_file(vault, tmp_path):
    """Refuse a file, which would otherwise root a project at something with no contents."""
    note = tmp_path / "afile.md"
    note.write_text("x", encoding="utf-8")

    result = runner.invoke(app, ["project", "--register", "--cwd", str(note)])
    assert result.exit_code != 0
    assert "not a directory" in result.output
    assert not (vault / "_system" / "registry.toml").exists()


def _hide_git(tmp_path, monkeypatch) -> None:
    """Empty PATH of every git, so every git call fails the way a missing binary does."""
    empty = tmp_path / "empty-bin"
    empty.mkdir(exist_ok=True)
    monkeypatch.setenv("PATH", str(empty))


def test_an_unregistered_repository_is_not_absorbed_when_git_cannot_answer(
    vault, tmp_path, monkeypatch
):
    """Keep the repository boundary when git cannot be asked where the boundary is.

    Empty git facts are not evidence of a plain directory, so treating them as such would
    hand the enclosing project's slug to any repository git failed to inspect.
    """
    outer = tmp_path / "outer"
    nested = outer / "nested-repo"
    nested.mkdir(parents=True)
    _run("init", "-q", ".", cwd=nested)
    _run("commit", "-q", "--allow-empty", "-m", "init", cwd=nested)
    runner.invoke(app, ["project", "--register", "--cwd", str(outer)])

    _hide_git(tmp_path, monkeypatch)

    resolved = runner.invoke(app, ["project", "--cwd", str(nested), "--json"])
    payload = json.loads(resolved.stdout)
    assert resolved.exit_code != 0
    assert payload["registered"] is False
    # A null root for a real repository is what proves git was unreachable here, so the
    # assertion above is about the gate rather than about git finding nothing.
    assert payload["repo_root"] is None


def test_project_refuses_when_git_cannot_answer(vault, tmp_path, monkeypatch):
    """Refuse rather than record a repository as a plain directory with no remote."""
    root = tmp_path / "some-repo"
    root.mkdir()
    _run("init", "-q", ".", cwd=root)

    _hide_git(tmp_path, monkeypatch)

    result = runner.invoke(app, ["project", "--register", "--cwd", str(root)])
    assert result.exit_code != 0
    assert "cannot tell whether" in result.output
    assert not (vault / "_system" / "registry.toml").exists()


def _write_malformed_registry(vault) -> None:
    """Write a registry.toml that parses as TOML but fails registry validation."""
    system_dir = vault / "_system"
    system_dir.mkdir(parents=True, exist_ok=True)
    (system_dir / "registry.toml").write_text(
        '[projects.demo]\nremotes = "not-a-list"\nroot = "x"\n'
    )


def test_project_reports_a_malformed_registry(vault, repo):
    """Report the offending file and exit non-zero instead of crashing."""
    _write_malformed_registry(vault)
    result = runner.invoke(app, ["project", "--cwd", str(repo)])
    assert result.exit_code != 0
    assert "registry.toml" in result.output
    assert "malformed" in result.output


def test_project_register_reports_a_malformed_registry(vault, repo):
    """Report the offending file and exit non-zero instead of crashing."""
    _write_malformed_registry(vault)
    result = runner.invoke(app, ["project", "--register", "--cwd", str(repo)])
    assert result.exit_code != 0
    assert "registry.toml" in result.output
    assert "malformed" in result.output


def _write_syntactically_invalid_registry(vault) -> None:
    """Write a registry.toml that is not valid TOML at all."""
    system_dir = vault / "_system"
    system_dir.mkdir(parents=True, exist_ok=True)
    (system_dir / "registry.toml").write_text("this is not [ valid toml")


def test_project_reports_a_syntactically_invalid_registry(vault, repo):
    """Report a clean message and exit non-zero rather than a raw traceback."""
    _write_syntactically_invalid_registry(vault)
    result = runner.invoke(app, ["project", "--cwd", str(repo)])
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "registry.toml" in result.output
    assert "malformed" in result.output


def test_project_register_reports_a_syntactically_invalid_registry(vault, repo):
    """Report a clean message and exit non-zero rather than a raw traceback."""
    _write_syntactically_invalid_registry(vault)
    result = runner.invoke(app, ["project", "--register", "--cwd", str(repo)])
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "registry.toml" in result.output
    assert "malformed" in result.output


def test_project_refuses_an_empty_slug(vault, repo):
    """Reject a blank --slug explicitly rather than silently deriving one instead."""
    result = runner.invoke(app, ["project", "--register", "--cwd", str(repo), "--slug", "  "])
    assert result.exit_code != 0
    assert "must not be empty" in result.output


def test_project_refuses_a_slug_that_would_escape_the_vault(vault, repo):
    """Reject a slug carrying path separators before it can name a directory."""
    result = runner.invoke(
        app, ["project", "--register", "--cwd", str(repo), "--slug", "../../pwned"]
    )
    assert result.exit_code != 0
    assert "lowercase letters" in result.output
    assert not (vault.parent / "pwned").exists()
    assert not (vault / "_system" / "registry.toml").exists()


def test_project_refuses_a_slug_that_is_not_kebab_case(vault, repo):
    """Reject rather than quietly rewrite a slug, and name the acceptable form."""
    result = runner.invoke(app, ["project", "--register", "--cwd", str(repo), "--slug", "My Repo"])
    assert result.exit_code != 0
    assert "--slug my-repo" in result.output


def test_project_refuses_a_slug_with_no_usable_characters(vault, repo):
    """Reject a slug that normalizes to nothing rather than suggesting an empty form."""
    result = runner.invoke(app, ["project", "--register", "--cwd", str(repo), "--slug", "???"])
    assert result.exit_code != 0
    assert "lowercase letters" in result.output


def test_project_warns_when_nesting_inside_another_project(vault, tmp_path):
    """Allow a nested registration but report it, since an accident looks like an intention."""
    outer = tmp_path / "outer"
    inner = outer / "nested" / "inner"
    inner.mkdir(parents=True)
    runner.invoke(app, ["project", "--register", "--cwd", str(outer)])

    result = runner.invoke(app, ["project", "--register", "--cwd", str(inner)])
    assert result.exit_code == 0
    # pp.warning writes to stderr; .output is the combined stream a user would see.
    assert "sits inside project 'outer'" in result.output
    assert "'inner' now wins" in result.output

    payload = json.loads(runner.invoke(app, ["project", "--cwd", str(inner), "--json"]).stdout)
    assert payload["slug"] == "inner"


def test_project_does_not_warn_outside_every_registered_project(vault, tmp_path):
    """Stay quiet for the ordinary case, so the warning keeps meaning something."""
    outer = tmp_path / "outer"
    outer.mkdir()
    runner.invoke(app, ["project", "--register", "--cwd", str(outer)])

    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    result = runner.invoke(app, ["project", "--register", "--cwd", str(unrelated)])
    assert result.exit_code == 0
    assert "sits inside project" not in result.output


def test_project_slugifies_a_remote_derived_slug(vault, tmp_path):
    """Hold a derived slug to the same form an explicit --slug must already have."""
    root = tmp_path / "checkout"
    root.mkdir()
    _run("init", "-q", ".", cwd=root)
    _run("commit", "-q", "--allow-empty", "-m", "init", cwd=root)
    _run("remote", "add", "origin", "git@github.com:n/My.Repo.git", cwd=root)

    result = runner.invoke(app, ["project", "--register", "--cwd", str(root)])
    assert result.exit_code == 0
    assert "my-repo" in result.output

    payload = json.loads(runner.invoke(app, ["project", "--cwd", str(root), "--json"]).stdout)
    assert payload["slug"] == "my-repo"


def test_project_slugifies_a_directory_derived_slug(vault, tmp_path):
    """Slugify a directory name, which is likelier than a repository name to need it."""
    root = tmp_path / "My Notes Dir"
    root.mkdir()

    result = runner.invoke(app, ["project", "--register", "--cwd", str(root)])
    assert result.exit_code == 0
    assert "my-notes-dir" in result.output

    payload = json.loads(runner.invoke(app, ["project", "--cwd", str(root), "--json"]).stdout)
    assert payload["slug"] == "my-notes-dir"


def test_project_reports_a_directory_name_that_yields_no_slug(vault, tmp_path):
    """Name --slug as the remedy rather than failing on an empty slug."""
    root = tmp_path / "???"
    root.mkdir()

    result = runner.invoke(app, ["project", "--register", "--cwd", str(root)])
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "--slug" in result.output
    assert not (vault / "_system" / "registry.toml").exists()


def test_project_json_names_the_project_directory(vault, repo):
    """Report the folder every project-scoped location lives under."""
    runner.invoke(app, ["project", "--register", "--cwd", str(repo)])

    result = runner.invoke(app, ["project", "--cwd", str(repo), "--json"])
    payload = json.loads(result.stdout)

    assert payload["project_dir"] == str(vault / "projects" / "demo")
    assert payload["paths"]["logs"].startswith(payload["project_dir"])


def test_project_json_has_no_project_directory_when_unregistered(vault, repo):
    """Report no project directory for a directory that has not been registered."""
    result = runner.invoke(app, ["project", "--cwd", str(repo), "--json"])

    assert json.loads(result.stdout)["project_dir"] is None


def test_project_json_keeps_the_payload_the_hook_reads(vault, repo):
    """Hold the shape `hooks/sessionhooks/vaultcli.py` parses, field for field."""
    runner.invoke(app, ["project", "--register", "--cwd", str(repo)])

    payload = json.loads(runner.invoke(app, ["project", "--cwd", str(repo), "--json"]).stdout)

    assert set(payload) == {
        "slug",
        "registered",
        "repo_root",
        "is_worktree",
        "project_dir",
        "paths",
    }
    assert payload["slug"] == "demo"
    assert payload["registered"] is True


def test_project_reports_unregistered_and_exits_non_zero(vault, repo):
    """Report unregistered and exit non-zero, so a caller can branch on the exit code."""
    result = runner.invoke(app, ["project", "--cwd", str(repo), "--json"])
    assert result.exit_code != 0
    assert json.loads(result.stdout)["registered"] is False


def test_project_refuses_slug_without_register(vault, repo):
    """A slug is permanent once notes carry it, so it is set once and never edited."""
    runner.invoke(app, ["project", "--register", "--cwd", str(repo)])

    result = runner.invoke(app, ["project", "--cwd", str(repo), "--slug", "renamed"])

    assert result.exit_code != 0
    assert "--slug applies only with --register" in result.output


def test_project_register_refuses_a_second_registration(vault, repo):
    """Guard the accidental case, which looks exactly like the deliberate one."""
    runner.invoke(app, ["project", "--register", "--cwd", str(repo)])

    result = runner.invoke(app, ["project", "--register", "--cwd", str(repo)])

    assert result.exit_code != 0
    assert "already registered" in result.output


def test_project_prose_reports_the_registered_root_not_the_directory_asked_about(vault, tmp_path):
    """Name the root the entry holds, since a directory outside git resolves by prefix."""
    plain = tmp_path / "notes-dir"
    deep = plain / "a" / "b"
    deep.mkdir(parents=True)
    runner.invoke(app, ["project", "--register", "--cwd", str(plain)])

    result = runner.invoke(app, ["project", "--cwd", str(deep)])

    assert result.exit_code == 0
    assert f"root: {plain}" in result.output


def test_project_register_creates_the_project_folder_and_its_fields(vault, repo):
    """Create the folder at registration, since the sweep runs inside it before any page exists."""
    result = runner.invoke(app, ["project", "--register", "--cwd", str(repo)])

    assert result.exit_code == 0
    assert paths_lib.project_dir(vault, "demo").is_dir()
    assert paths_lib.learnings_dir(vault, "demo").is_dir()
    assert paths_lib.logs_dir(vault, "demo").is_dir()
