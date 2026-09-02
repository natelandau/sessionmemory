"""Tests for the project registry."""

from __future__ import annotations

import pytest

from sessionmemory.lib import registry
from sessionmemory.lib.paths import SYSTEM_DIR
from sessionmemory.lib.registry import (
    REGISTRY_FILE,
    Project,
    RegistryError,
    find_by_path_prefix,
    find_by_remote,
    find_by_root,
    load,
    normalize_remote,
    save,
)


def _write_raw_registry(tmp_path, toml_text: str) -> None:
    """Write raw TOML straight to the registry path, bypassing `save`.

    Used to simulate a hand-edited `registry.toml` with a shape `save` would never
    produce, so `load`'s type validation can be exercised against it.
    """
    registry_dir = tmp_path / SYSTEM_DIR
    registry_dir.mkdir(parents=True, exist_ok=True)
    (registry_dir / REGISTRY_FILE).write_text(toml_text, encoding="utf-8")


@pytest.mark.parametrize(
    "url",
    [
        "ssh://git@gitea.example.org:2222/nate/gx.git",
        "git@gitea.example.org:nate/gx.git",
        "https://gitea.example.org/nate/gx.git",
        "https://gitea.example.org/nate/gx",
        "HTTPS://Gitea.Example.org/nate/GX.git",
    ],
)
def test_normalize_remote_collapses_equivalent_urls(url):
    """Reduce every spelling of one repository to the same key."""
    assert normalize_remote(url) == "gitea.example.org/nate/gx"


def test_normalize_remote_keeps_distinct_repositories_distinct():
    """Do not collapse two different repositories onto one key."""
    a = normalize_remote("git@github.com:nate/gx.git")
    b = normalize_remote("git@github.com:nate/ezbak.git")
    assert a != b


def test_save_then_load_round_trips(tmp_path):
    """Write the registry and read back an equal structure."""
    projects = {
        "gx": Project(
            slug="gx",
            remotes=("github.com/nate/gx",),
            root="/Users/nate/repos/gx",
        )
    }
    save(tmp_path, projects)
    assert load(tmp_path) == projects


def test_load_missing_registry_returns_empty(tmp_path):
    """Treat an absent registry as empty rather than an error."""
    assert load(tmp_path) == {}


def test_save_creates_the_system_directory(tmp_path):
    """Create _system/ on first write."""
    save(tmp_path, {})
    assert (tmp_path / "_system" / "registry.toml").is_file()


def test_find_by_remote_matches_any_remote(tmp_path):
    """Match a project when any one of its remotes matches any candidate."""
    projects = {
        "gx": Project(slug="gx", remotes=("github.com/nate/gx",), root="/r/gx"),
    }
    assert find_by_remote(projects, ["github.com/nate/gx"]).slug == "gx"
    assert find_by_remote(projects, ["github.com/nate/other"]) is None


def test_find_by_root_matches_exactly(tmp_path):
    """Match a project by its recorded repository root."""
    projects = {"gx": Project(slug="gx", remotes=(), root="/r/gx")}
    assert find_by_root(projects, "/r/gx").slug == "gx"
    assert find_by_root(projects, "/r/gx/sub") is None


def test_find_by_root_matches_through_a_symlink(tmp_path):
    """Match a stored root against a resolved query even when either uses a symlink.

    A caller may store or query the same repository through different symlinked
    spellings (`/tmp` and `/var` are symlinks on macOS), so both sides must be
    canonicalized before comparison rather than compared as raw strings.
    """
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real_dir)
    except OSError:
        pytest.skip("platform does not support symlinks")

    projects = {"gx": Project(slug="gx", remotes=(), root=str(link))}
    assert find_by_root(projects, str(real_dir)).slug == "gx"


def test_find_by_root_ignores_empty_query():
    """Never treat an empty query as a match for an empty stored root.

    `Path("").resolve()` returns the current working directory, so without this
    guard an empty query would match whatever directory the process happens to be
    running in.
    """
    projects = {"gx": Project(slug="gx", remotes=(), root="")}
    assert find_by_root(projects, "") is None


def test_find_by_root_skips_a_relative_root(tmp_path, monkeypatch):
    """Never measure a hand-edited relative root from wherever the process is running."""
    monkeypatch.chdir(tmp_path)
    projects = {"gx": Project(slug="gx", remotes=(), root="relative/gx")}
    assert find_by_root(projects, str(tmp_path / "relative" / "gx")) is None


def test_find_by_root_handles_a_root_that_no_longer_exists_on_disk(tmp_path):
    """Do not raise when a stored root has since been deleted."""
    projects = {
        "gx": Project(slug="gx", remotes=(), root=str(tmp_path / "gone")),
    }
    assert find_by_root(projects, str(tmp_path / "gone")).slug == "gx"
    assert find_by_root(projects, str(tmp_path / "elsewhere")) is None


def test_find_by_path_prefix_matches_a_root_and_its_descendants():
    """Match the root itself and any directory beneath it."""
    projects = {"gx": Project(slug="gx", remotes=(), root="/r/gx")}
    assert find_by_path_prefix(projects, "/r/gx").slug == "gx"
    assert find_by_path_prefix(projects, "/r/gx/a/b/c").slug == "gx"
    assert find_by_path_prefix(projects, "/r/other") is None
    assert find_by_path_prefix(projects, "/r/gx-adjacent") is None


def test_find_by_path_prefix_prefers_the_deepest_root():
    """Resolve a nested project as itself rather than as the one containing it."""
    projects = {
        "outer": Project(slug="outer", remotes=(), root="/r/outer"),
        "inner": Project(slug="inner", remotes=(), root="/r/outer/nested/inner"),
    }
    assert find_by_path_prefix(projects, "/r/outer/nested/inner/deep").slug == "inner"
    assert find_by_path_prefix(projects, "/r/outer/nested").slug == "outer"


def test_find_by_path_prefix_matches_through_a_symlink(tmp_path):
    """Canonicalize both sides, so a symlinked spelling of either still matches."""
    real_dir = tmp_path / "real"
    (real_dir / "sub").mkdir(parents=True)
    link = tmp_path / "link"
    try:
        link.symlink_to(real_dir)
    except OSError:
        pytest.skip("platform does not support symlinks")

    projects = {"gx": Project(slug="gx", remotes=(), root=str(link))}
    assert find_by_path_prefix(projects, str(real_dir / "sub")).slug == "gx"


def test_find_by_path_prefix_ignores_empty_values():
    """Never let an empty query or an empty stored root produce a match."""
    projects = {"gx": Project(slug="gx", remotes=(), root="")}
    assert find_by_path_prefix(projects, "") is None
    assert find_by_path_prefix(projects, "/r/gx") is None


def test_find_by_path_prefix_skips_a_relative_root(tmp_path, monkeypatch):
    """Never measure a hand-edited relative root from wherever the process is running."""
    monkeypatch.chdir(tmp_path)
    projects = {"gx": Project(slug="gx", remotes=(), root="relative/gx")}
    assert find_by_path_prefix(projects, str(tmp_path / "relative" / "gx" / "sub")) is None


def test_find_by_path_prefix_breaks_a_tie_on_slug_order():
    """Answer the same way every run when two entries record the same root."""
    projects = {
        "second": Project(slug="second", remotes=(), root="/r/shared"),
        "first": Project(slug="first", remotes=(), root="/r/shared"),
    }
    assert find_by_path_prefix(projects, "/r/shared/sub").slug == "first"


def test_load_rejects_non_table_top_level_projects(tmp_path):
    """Reject `projects` written as a scalar instead of a table."""
    _write_raw_registry(tmp_path, 'projects = "oops"\n')
    with pytest.raises(RegistryError, match="projects"):
        load(tmp_path)


def test_load_rejects_non_table_entry(tmp_path):
    """Reject an entry written as a scalar instead of a table."""
    _write_raw_registry(tmp_path, "[projects]\ngx = 1\n")
    with pytest.raises(RegistryError, match="gx"):
        load(tmp_path)


def test_load_rejects_remotes_that_are_not_a_list(tmp_path):
    """Reject a `remotes` field written as a bare string instead of a list."""
    _write_raw_registry(
        tmp_path,
        '[projects.gx]\nremotes = "github.com/nate/gx"\nroot = "/r/gx"\n',
    )
    with pytest.raises(RegistryError, match="remotes"):
        load(tmp_path)


def test_load_rejects_root_that_is_not_a_string(tmp_path):
    """Reject a `root` field written as a number instead of a string."""
    _write_raw_registry(tmp_path, "[projects.gx]\nroot = 1\n")
    with pytest.raises(RegistryError, match="root"):
        load(tmp_path)


def test_load_rejects_syntactically_invalid_toml(tmp_path):
    """Reject a hand-edited registry that is not valid TOML at all.

    A syntax error and a structural error are both a broken hand-edited file from
    the caller's point of view, so both raise the same `RegistryError`.
    """
    _write_raw_registry(tmp_path, "this is not [ valid toml")
    with pytest.raises(RegistryError, match="not valid TOML"):
        load(tmp_path)


def test_load_refuses_a_slug_that_is_not_slug_shaped(tmp_path):
    """Reject a registry key that would walk a project's files outside the vault."""
    _write_raw_registry(
        tmp_path,
        '[projects."../../escaped"]\nremotes = []\nroot = "/r"\n',
    )
    with pytest.raises(RegistryError, match="not a valid project slug"):
        load(tmp_path)


def test_load_refuses_a_slug_that_cannot_be_slugified(tmp_path):
    """Reject a registry key that slugifies to nothing, not just one that slugifies differently.

    `ids.slugify` raises ValueError for a title with no usable characters, a
    different failure mode from the key that merely maps to a different slug
    (covered above); both must be refused the same way.
    """
    # Given a registry key with no characters that survive slugification
    _write_raw_registry(
        tmp_path,
        '[projects."!!!"]\nremotes = []\nroot = "/r"\n',
    )

    # When loading the registry / Then it is refused the same way a mismatched slug is
    with pytest.raises(RegistryError, match="not a valid project slug"):
        load(tmp_path)


def test_load_ignores_a_legacy_tags_key(tmp_path):
    """Verify a registry written by the old CLI still loads."""
    (tmp_path / "_system").mkdir()
    (tmp_path / "_system" / "registry.toml").write_text(
        '[projects.demo]\nremotes = ["example.org/n/demo"]\nroot = "/r"\ntags = ["pytest"]\n',
        encoding="utf-8",
    )

    assert registry.load(tmp_path)["demo"].root == "/r"
