"""Tests for the library that links local entries to the vault."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from sessionmemory.lib import localdeclined, localmanifest, localmd, localtree, paths
from sessionmemory.lib.localmd import LocalMdError, State

NAME = paths.LOCAL_MD_FILE


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path) -> Path:
    """Build a git repository with one empty commit."""
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "-q", ".", cwd=root)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=root)
    return root


@pytest.fixture
def co(vault, repo) -> localmd.Checkout:
    """Return the checkout for a project slugged demo, with no vault copy yet."""
    return localmd.checkout(vault, "demo", repo)


@pytest.fixture
def md(co) -> localmd.Entry:
    """Return the checkout's CLAUDE.local.md entry."""
    return localmd.entry(co, NAME)


def _vault_copy(md: localmd.Entry, text: str = "# local\n") -> None:
    md.target.parent.mkdir(parents=True, exist_ok=True)
    md.target.write_text(text)


def _track(co: localmd.Checkout, md: localmd.Entry, text: str = "committed\n") -> None:
    md.link.write_text(text)
    _git("add", NAME, cwd=co.worktree)
    _git("commit", "-q", "-m", "track", cwd=co.worktree)


def _link(co: localmd.Checkout, md: localmd.Entry) -> localmd.LinkResult:
    return localmd.link(co, md, tracked=md.name in localmd.tracked_names(co, [md]))


def _adopt(co: localmd.Checkout, md: localmd.Entry) -> localmd.AdoptResult:
    return localmd.adopt(co, md, tracked=md.name in localmd.tracked_names(co, [md]))


def _exclude(co: localmd.Checkout) -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--git-path", "info/exclude"],
        cwd=co.worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return co.worktree / out


def test_checkout_uses_worktree_top_level(vault, repo, tmp_path):
    """Verify the link sits at the work tree's top level, including in a linked worktree."""
    sub = repo / "sub" / "dir"
    sub.mkdir(parents=True)

    assert localmd.entry(localmd.checkout(vault, "demo", sub), NAME).link == repo.resolve() / NAME

    linked = tmp_path / "linked"
    _git("worktree", "add", "-q", str(linked), "-b", "other", cwd=repo)

    linked_co = localmd.checkout(vault, "demo", linked)
    assert localmd.entry(linked_co, NAME).link == linked.resolve() / NAME


def test_checkout_outside_git_raises(vault, tmp_path):
    """Verify a directory outside any work tree is refused."""
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(LocalMdError):
        localmd.checkout(vault, "demo", plain)


def test_state_none(co, md):
    """Verify no link and no vault copy reads none."""
    assert localmd.state(md) is State.NONE


def test_state_not_linked(co, md):
    """Verify a vault copy with no file in the checkout reads not linked."""
    _vault_copy(md)

    assert localmd.state(md) is State.NOT_LINKED


def test_state_unadopted(co, md):
    """Verify a regular file with no vault copy reads unadopted."""
    md.link.write_text("mine\n")

    assert localmd.state(md) is State.UNADOPTED


def test_state_diverged(co, md):
    """Verify a regular file beside a vault copy reads diverged."""
    _vault_copy(md)
    md.link.write_text("mine\n")

    assert localmd.state(md) is State.DIVERGED


def test_state_linked(co, md):
    """Verify a symlink to the vault copy reads linked."""
    _vault_copy(md)
    md.link.symlink_to(md.target)

    assert localmd.state(md) is State.LINKED


def test_state_linked_through_symlinked_parent(co, md, tmp_path):
    """Verify a target reached through a symlinked directory still reads linked."""
    _vault_copy(md)
    alias = tmp_path / "alias"
    alias.symlink_to(md.target.parent)
    md.link.symlink_to(alias / NAME)

    assert localmd.state(md) is State.LINKED


def test_state_dangling(co, md, tmp_path):
    """Verify a symlink to nothing reads dangling."""
    md.link.symlink_to(tmp_path / "missing")

    assert localmd.state(md) is State.DANGLING


def test_state_foreign_link(co, md, tmp_path):
    """Verify a symlink to some other file reads foreign link."""
    _vault_copy(md)
    other = tmp_path / "other.md"
    other.write_text("x\n")
    md.link.symlink_to(other)

    assert localmd.state(md) is State.FOREIGN_LINK


def test_contents_match(co, md):
    """Verify contents compare only when both are regular files."""
    assert localmd.contents_match(md) is None

    _vault_copy(md, "same\n")
    md.link.write_text("same\n")
    assert localmd.contents_match(md) is True

    md.link.write_text("different\n")
    assert localmd.contents_match(md) is False

    md.link.unlink()
    md.link.symlink_to(md.target)
    assert localmd.contents_match(md) is None


def test_link_creates_absolute_symlink_and_ignores_it(co, md):
    """Verify link places an absolute symlink and hides it from git."""
    _vault_copy(md)

    result = _link(co, md)

    assert result.status == "created"
    assert md.link.readlink().is_absolute()
    assert md.link.readlink() == md.target
    assert localmd.ignored(co, md)


def test_link_twice_is_present_and_one_exclude_entry(co, md):
    """Verify a second link changes nothing and writes one exclude entry."""
    _vault_copy(md)
    _link(co, md)

    again = _link(co, md)

    assert again.status == "present"
    assert _exclude(co).read_text().splitlines().count(NAME) == 1


def test_link_replaces_dangling_symlink(co, md, tmp_path):
    """Verify a dangling symlink is replaced by the link."""
    _vault_copy(md)
    md.link.symlink_to(tmp_path / "missing")

    result = _link(co, md)

    assert result.status == "created"
    assert localmd.state(md) is State.LINKED


def test_link_refuses_regular_file_as_conflict(co, md):
    """Verify a regular file beside a vault copy is left untouched."""
    _vault_copy(md)
    md.link.write_text("mine\n")

    result = _link(co, md)

    assert result.status == "conflict"
    assert result.state is State.DIVERGED
    assert md.link.read_text() == "mine\n"
    assert not md.link.is_symlink()


def test_link_without_vault_copy_is_none(co, md, vault):
    """Verify nothing is linked when the vault has no copy."""
    assert localmd.link_all(co) == []
    with pytest.raises(LocalMdError, match="vault holds no"):
        _link(co, md)

    assert not md.link.exists()
    assert not md.link.is_symlink()


def test_link_refuses_tracked_file(co, md):
    """Verify a committed CLAUDE.local.md is never replaced, and reads as tracked."""
    _vault_copy(md)
    _track(co, md)
    md.link.unlink()

    result = _link(co, md)

    assert result.status == "tracked"
    assert not md.link.exists()
    assert not md.link.is_symlink()


def test_link_unadopted_file_is_none_and_untouched(co, md, vault):
    """Verify a regular file with no vault copy is never linked over."""
    _vault_copy(md)
    md.target.unlink()
    md.link.write_text("mine\n")

    assert localmd.link_all(co) == []
    assert md.link.read_text() == "mine\n"
    assert not md.link.is_symlink()


def test_link_foreign_link_is_conflict(co, md, tmp_path):
    """Verify a symlink to some other file is left pointing there."""
    _vault_copy(md)
    other = tmp_path / "other.md"
    other.write_text("x\n")
    md.link.symlink_to(other)

    result = _link(co, md)

    assert result.status == "conflict"
    assert result.state is State.FOREIGN_LINK
    assert md.link.readlink() == other


def test_link_tracked_file_without_vault_copy_is_none(co, md, vault):
    """Verify a tracked file is not an error while the vault holds no copy."""
    _track(co, md)

    assert localmd.link_all(co) == []
    assert md.link.read_text() == "committed\n"


def test_adopt_moves_file_and_links(co, md):
    """Verify adopt copies the bytes into the vault, links, and ignores the link."""
    md.link.write_bytes(b"# mine\n")

    result = _adopt(co, md)

    assert result.already_linked is False
    assert result.target == md.target
    assert md.target.read_bytes() == b"# mine\n"
    assert localmd.state(md) is State.LINKED
    assert localmd.ignored(co, md)


def test_adopt_twice_reports_already_linked(co, md):
    """Verify a second adopt changes nothing."""
    md.link.write_text("mine\n")
    _adopt(co, md)

    again = _adopt(co, md)

    assert again.already_linked is True
    assert md.target.read_text() == "mine\n"


def test_adopt_identical_vault_copy_just_links(co, md):
    """Verify an equal vault copy is kept and the file is replaced by the link."""
    _vault_copy(md, "same\n")
    md.link.write_text("same\n")

    result = _adopt(co, md)

    assert result.already_linked is False
    assert localmd.state(md) is State.LINKED
    assert md.target.read_text() == "same\n"


def test_adopt_refuses_differing_vault_copy(co, md):
    """Verify differing copies are both left alone and both named."""
    _vault_copy(md, "vault\n")
    md.link.write_text("mine\n")

    with pytest.raises(LocalMdError) as caught:
        _adopt(co, md)

    text = " ".join([caught.value.message, *caught.value.details])
    assert str(md.link) in text
    assert str(md.target) in text
    assert md.link.read_text() == "mine\n"
    assert not md.link.is_symlink()
    assert md.target.read_text() == "vault\n"


def test_adopt_refuses_missing_file(co, md):
    """Verify adopting with nothing to adopt names the work tree."""
    with pytest.raises(LocalMdError, match=r"no CLAUDE\.local\.md in"):
        _adopt(co, md)


def test_adopt_refuses_dangling_and_not_linked(co, md, tmp_path):
    """Verify a dangling link and a lone vault copy are both refused."""
    md.link.symlink_to(tmp_path / "missing")
    with pytest.raises(LocalMdError, match=r"no CLAUDE\.local\.md in"):
        _adopt(co, md)

    md.link.unlink()
    _vault_copy(md)
    with pytest.raises(LocalMdError, match=r"no CLAUDE\.local\.md in"):
        _adopt(co, md)


def test_adopt_refuses_foreign_link(co, md, tmp_path):
    """Verify a link to another file is refused and names where it points."""
    other = tmp_path / "other.md"
    other.write_text("x\n")
    md.link.symlink_to(other)

    with pytest.raises(LocalMdError) as caught:
        _adopt(co, md)

    assert str(other) in " ".join([caught.value.message, *caught.value.details])
    assert md.link.readlink() == other


def test_adopt_refuses_tracked_file(co, md):
    """Verify a committed CLAUDE.local.md is never adopted."""
    _track(co, md)

    with pytest.raises(LocalMdError, match="tracked"):
        _adopt(co, md)

    assert not md.target.exists()
    assert not md.link.is_symlink()


def test_adopt_refuses_non_utf8(co, md):
    """Verify bytes that are not UTF-8 are refused with nothing changed."""
    md.link.write_bytes(b"\xff\xfe")

    with pytest.raises(LocalMdError):
        _adopt(co, md)

    assert not md.target.exists()
    assert not md.link.is_symlink()
    assert md.link.read_bytes() == b"\xff\xfe"


def test_adopt_empty_file(co, md):
    """Verify an empty file is adopted like any other."""
    md.link.write_bytes(b"")

    _adopt(co, md)

    assert md.target.read_bytes() == b""
    assert localmd.state(md) is State.LINKED


def test_adopt_reports_relative_imports(co, md):
    """Verify relative imports are reported so the reader can move their targets."""
    md.link.write_text("see @./sibling.md and @/abs.md\n")

    assert _adopt(co, md).relative_imports == ["./sibling.md"]


def test_ensure_ignored_creates_info_dir(co, md):
    """Verify the exclude file is created when .git/info is missing."""
    info = co.worktree / ".git" / "info"
    if info.exists():
        for child in info.iterdir():
            child.unlink()
        info.rmdir()

    assert localmd.ensure_ignored(co, md) is True
    assert localmd.ignored(co, md)


def test_ensure_ignored_adds_newline_before_entry(co, md):
    """Verify an exclude file without a trailing newline keeps its last line."""
    exclude = _exclude(co)
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("foo")

    localmd.ensure_ignored(co, md)

    assert exclude.read_text() == f"foo\n{NAME}\n"


def test_ensure_ignored_skips_when_gitignore_covers_it(co, md):
    """Verify an existing ignore rule means no exclude write."""
    (co.worktree / ".gitignore").write_text(f"{NAME}\n")

    assert localmd.ensure_ignored(co, md) is False
    exclude = _exclude(co)
    assert not exclude.exists() or NAME not in exclude.read_text()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("- @./a.md", ["./a.md"]),
        ("@docs/x.md", ["docs/x.md"]),
        ("@~/x.md", []),
        ("@/abs/x.md", []),
        ("mail me@example.com", []),
        ("`@inline.md`", []),
        ("```\n@fenced.md\n```", []),
        ("see @a.md, then @b.md.", ["a.md", "b.md"]),
        ("(see @docs/a.md)", ["docs/a.md"]),
        ("[@x.md]", []),
        ("@a.md; @b.md:", ["a.md", "b.md"]),
        ("    @indented-code.md", []),
        ("\t@tabbed-code.md", []),
        ("`code`@after.md", ["after.md"]),
        ("@)", []),
        ("- item\n    - nested @nested.md", ["nested.md"]),
        ("    1. nested @numbered.md", ["numbered.md"]),
        ("~~~\n```\n@still-fenced.md\n~~~\n@after.md", ["after.md"]),
    ],
)
def test_relative_imports(text, expected):
    """Verify only relative @path tokens outside code are imports."""
    assert localmd.relative_imports(text) == expected


def test_ignored_raises_when_git_fails_outside_a_repository(tmp_path):
    """Verify a git fatal error is an error rather than "not ignored"."""
    plain = tmp_path / "plain"
    plain.mkdir()
    co = localmd.Checkout(worktree=plain, vault=tmp_path, slug="demo", local=tmp_path / "local")

    with pytest.raises(LocalMdError):
        localmd.ignored(co, localmd.entry(co, NAME))


def test_link_present_adds_a_missing_ignore_entry(co, md):
    """Verify linking an already linked checkout still makes git ignore the link."""
    _vault_copy(md)
    md.link.symlink_to(md.target)

    result = _link(co, md)

    assert result.status == "present"
    assert localmd.ignored(co, md)


def test_adopt_already_linked_adds_a_missing_ignore_entry(co, md):
    """Verify adopting an already linked checkout still makes git ignore the link."""
    _vault_copy(md)
    md.link.symlink_to(md.target)

    result = _adopt(co, md)

    assert result.already_linked is True
    assert localmd.ignored(co, md)


def test_link_creates_without_a_temporary_name(co, md, monkeypatch):
    """Verify linking into an empty path places the symlink directly, with no rename."""
    _vault_copy(md)

    def no_replace(*_args) -> None:
        msg = "place_link must not run when nothing is at the link path"
        raise AssertionError(msg)

    monkeypatch.setattr(localtree, "place_link", no_replace)

    assert _link(co, md).status == "created"
    assert localmd.state(md) is State.LINKED
    assert not list(co.worktree.glob(".CLAUDE.local.md.*.tmp"))


def test_link_reports_a_file_that_appears_before_the_link(co, md, monkeypatch):
    """Verify a file written after the state was read is never overwritten."""
    _vault_copy(md)
    real_ensure = localmd.ensure_ignored

    def racing_ensure(checkout: localmd.Checkout, entry: localmd.Entry) -> bool:
        entry.link.write_text("mine\n")
        return real_ensure(checkout, entry)

    monkeypatch.setattr(localmd, "ensure_ignored", racing_ensure)

    result = _link(co, md)

    assert result.status == "conflict"
    assert result.state is State.DIVERGED
    assert md.link.read_text() == "mine\n"


def test_adopt_removes_a_copy_that_does_not_read_back(co, md, monkeypatch):
    """Verify a vault copy that fails its read-back is removed and the original kept."""
    md.link.write_text("mine\n")

    def bad_copy(_source: Path, dest: Path) -> bool:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("corrupt\n")
        return True

    monkeypatch.setattr(localtree, "copy_file", bad_copy)

    with pytest.raises(LocalMdError, match="does not match"):
        _adopt(co, md)

    assert not md.target.exists()
    assert md.link.read_text() == "mine\n"
    assert not md.link.is_symlink()


def test_ensure_ignored_does_not_repeat_an_entry_a_gitignore_negates(co, md):
    """Verify an exclude entry overridden by a `.gitignore` negation is written once."""
    (co.worktree / ".gitignore").write_text(f"!{NAME}\n")

    assert localmd.ensure_ignored(co, md) is True
    assert localmd.ensure_ignored(co, md) is False

    assert _exclude(co).read_text().count(NAME) == 1


def test_adopt_refuses_a_directory(co, md):
    """Verify a directory at the link path is refused rather than read as a file."""
    md.link.mkdir()

    with pytest.raises(localmd.LocalMdError, match="not a regular file"):
        _adopt(co, md)


def test_git_calls_ignore_an_ambient_git_dir(co, md, tmp_path, monkeypatch):
    """Verify GIT_DIR and GIT_INDEX_FILE from a running hook do not retarget the checkout."""
    other = tmp_path / "other"
    other.mkdir()
    _git("init", "-q", ".", cwd=other)
    (other / NAME).write_text("x", encoding="utf-8")
    _git("add", "-f", NAME, cwd=other)
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other / ".git" / "index"))

    assert localmd.tracked_names(co, [md]) == set()
    assert localmd.ensure_ignored(co, md) is True

    monkeypatch.delenv("GIT_DIR")
    monkeypatch.delenv("GIT_INDEX_FILE")
    assert localmd.ignored(co, md) is True
    assert NAME not in (other / ".git" / "info" / "exclude").read_text(encoding="utf-8")


def _legacy(vault: Path, text: str = "# old\n") -> Path:
    old = paths.legacy_local_md_path(vault, "demo")
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text(text, encoding="utf-8")
    return old


def test_migrate_legacy_moves_old_copy(vault):
    """Verify the old copy moves under local/ and the vault ignores gain the re-include lines."""
    old = _legacy(vault)

    assert localmd.migrate_legacy(vault, "demo") is None

    new = paths.local_dir(vault, "demo") / NAME
    assert not old.exists()
    assert new.read_text(encoding="utf-8") == "# old\n"
    assert "!projects/*/local/**" in (vault / ".gitignore").read_text(encoding="utf-8")


def test_migrate_legacy_dedupes_identical(vault):
    """Verify an identical old copy is removed."""
    old = _legacy(vault)
    new = paths.local_dir(vault, "demo") / NAME
    new.parent.mkdir(parents=True)
    new.write_text("# old\n", encoding="utf-8")

    assert localmd.migrate_legacy(vault, "demo") is None
    assert not old.exists()
    assert new.read_text(encoding="utf-8") == "# old\n"


def test_migrate_legacy_reports_differing_copies(vault):
    """Verify differing copies are left alone and the old path is returned."""
    old = _legacy(vault, "# old\n")
    new = paths.local_dir(vault, "demo") / NAME
    new.parent.mkdir(parents=True)
    new.write_text("# new\n", encoding="utf-8")

    assert localmd.migrate_legacy(vault, "demo") == old
    assert old.read_text(encoding="utf-8") == "# old\n"
    assert new.read_text(encoding="utf-8") == "# new\n"


def test_migrate_legacy_noop_without_old_copy(vault):
    """Verify nothing is written when there is no old copy."""
    (vault / ".gitignore").unlink()

    assert localmd.migrate_legacy(vault, "demo") is None
    assert not (vault / ".gitignore").exists()


def _vault_skill(co: localmd.Checkout, name: str = "a", text: str = "# skill\n") -> Path:
    folder = co.local / "claude" / "skills" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(text)
    return folder


def _checkout_skill(co: localmd.Checkout, name: str = "a", text: str = "# skill\n") -> Path:
    folder = co.worktree / ".claude" / "skills" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(text)
    return folder


def _names(entries: list[localmd.Entry]) -> list[str]:
    return [e.name for e in entries]


def test_entries_unions_vault_and_checkout(co):
    """Verify entries come from both sides, skip hidden names, and sort by kind."""
    _vault_skill(co, "a")
    rules = co.worktree / ".claude" / "rules"
    rules.mkdir(parents=True)
    (rules / "b.md").write_text("rule\n")
    (co.worktree / ".claude" / "skills" / ".hidden").mkdir(parents=True)

    assert _names(localmd.entries(co)) == ["skills/a", "rules/b.md"]


def test_entries_sorts_local_md_first_then_names(co, md):
    """Verify CLAUDE.local.md leads, then skills, then rules, each by name."""
    _vault_copy(md)
    _vault_skill(co, "z")
    _checkout_skill(co, "b")
    rules = co.local / "claude" / "rules"
    rules.mkdir(parents=True)
    (rules / "a.md").write_text("x\n")
    (rules / ".DS_Store").write_text("x\n")

    assert _names(localmd.entries(co)) == [NAME, "skills/b", "skills/z", "rules/a.md"]


def test_entry_paths_per_kind(co):
    """Verify each kind maps to its checkout path and its vault path."""
    skill = localmd.entry(co, "skills/a")
    assert skill.kind == "skill"
    assert skill.link == co.worktree / ".claude" / "skills" / "a"
    assert skill.target == co.local / "claude" / "skills" / "a"

    rule = localmd.entry(co, "rules/b.md")
    assert rule.kind == "rule"
    assert rule.link == co.worktree / ".claude" / "rules" / "b.md"
    assert rule.target == co.local / "claude" / "rules" / "b.md"

    md = localmd.entry(co, NAME)
    assert md.kind == "local-md"
    assert md.link == co.worktree / NAME
    assert md.target == co.local / NAME


@pytest.mark.parametrize(
    "name", ["skills/", "skills/.x", "rules/..", "", "claude.local.md", "../x", ".git/config"]
)
def test_entry_rejects_bad_names(co, name):
    """Verify a name that is no entry's is refused."""
    with pytest.raises(LocalMdError):
        localmd.entry(co, name)


def test_state_diverged_folder_match_and_mismatch(co):
    """Verify a folder beside a vault folder compares by relative paths and bytes."""
    skill = localmd.entry(co, "skills/a")
    _vault_skill(co, "a")
    local = _checkout_skill(co, "a")

    assert localmd.state(skill) is State.DIVERGED
    assert localmd.contents_match(skill) is True

    (local / "SKILL.md").write_text("changed\n")
    assert localmd.contents_match(skill) is False

    (local / "SKILL.md").write_text("# skill\n")
    (local / "extra.md").write_text("x\n")
    assert localmd.contents_match(skill) is False


def test_state_skill_folder_unadopted_and_not_linked(co):
    """Verify a skill on one side only reads unadopted or not linked."""
    skill = localmd.entry(co, "skills/a")
    _checkout_skill(co, "a")
    assert localmd.state(skill) is State.UNADOPTED

    other = localmd.entry(co, "skills/b")
    _vault_skill(co, "b")
    assert localmd.state(other) is State.NOT_LINKED


def test_tracked_names_one_git_call(co, monkeypatch):
    """Verify one git call covers every entry, and a file under a folder marks it tracked."""
    tracked_file = _checkout_skill(co, "a") / "SKILL.md"
    _git("add", str(tracked_file.relative_to(co.worktree)), cwd=co.worktree)
    calls: list[tuple[str, ...]] = []
    real_git = localmd._git

    def counting(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return real_git(cwd, *args)

    monkeypatch.setattr(localmd, "_git", counting)
    entries = [localmd.entry(co, n) for n in (NAME, "skills/a", "rules/b.md")]

    assert localmd.tracked_names(co, entries) == {"skills/a"}
    assert len(calls) == 1


def test_tracked_names_does_not_match_a_name_prefix(co):
    """Verify a tracked `skills/ab` does not mark `skills/a` tracked."""
    tracked_file = _checkout_skill(co, "ab") / "SKILL.md"
    _git("add", str(tracked_file.relative_to(co.worktree)), cwd=co.worktree)
    entries = [localmd.entry(co, "skills/a"), localmd.entry(co, "skills/ab")]

    assert localmd.tracked_names(co, entries) == {"skills/ab"}


def test_ensure_ignored_anchors_skill_lines(co, md):
    """Verify skills get an anchored exclude line and CLAUDE.local.md keeps its bare one."""
    skill = localmd.entry(co, "skills/a")

    assert localmd.ensure_ignored(co, skill) is True
    assert localmd.ensure_ignored(co, md) is True

    lines = _exclude(co).read_text().splitlines()
    assert "/.claude/skills/a" in lines
    assert NAME in lines
    assert localmd.ignored(co, skill)
    assert localmd.ensure_ignored(co, skill) is False


def test_link_all_links_skills_and_rules(co, vault):
    """Verify every vault-side entry is linked and ignored."""
    _vault_skill(co, "a")
    rules = co.local / "claude" / "rules"
    rules.mkdir(parents=True)
    (rules / "b.md").write_text("rule\n")

    results = localmd.link_all(co)

    assert [(r.entry.name, r.status) for r in results] == [
        ("skills/a", "created"),
        ("rules/b.md", "created"),
    ]
    for result in results:
        assert localmd.state(result.entry) is State.LINKED
        assert localmd.ignored(co, result.entry)


def test_link_creates_missing_parent_folder(co):
    """Verify linking a skill creates `.claude/skills/` when it is missing."""
    _vault_skill(co, "a")
    skill = localmd.entry(co, "skills/a")
    assert not (co.worktree / ".claude").exists()

    assert localmd.link(co, skill, tracked=False).status == "created"
    assert skill.link.readlink() == skill.target


def test_link_tracked_entry_is_tracked_status_and_untouched(co, md, vault):
    """Verify tracked entries are reported as tracked and left as they are."""
    _vault_skill(co, "a")
    _vault_copy(md, "vault\n")
    tracked_file = _checkout_skill(co, "a", "committed\n") / "SKILL.md"
    _git("add", str(tracked_file.relative_to(co.worktree)), cwd=co.worktree)
    _track(co, md)

    results = localmd.link_all(co)

    assert [(r.entry.name, r.status) for r in results] == [
        (NAME, "tracked"),
        ("skills/a", "tracked"),
    ]
    assert md.link.read_text() == "committed\n"
    assert not md.link.is_symlink()
    assert tracked_file.read_text() == "committed\n"
    assert not tracked_file.parent.is_symlink()


def test_link_all_removes_dangling_link_into_local(co, vault):
    """Verify a dangling link into the project's local folder is removed."""
    _vault_skill(co, "a")
    gone = co.worktree / ".claude" / "skills" / "gone"
    gone.parent.mkdir(parents=True)
    gone.symlink_to(co.local / "claude" / "skills" / "gone")

    results = localmd.link_all(co)

    assert ("skills/gone", "removed") in [(r.entry.name, r.status) for r in results]
    assert not gone.is_symlink()


def test_link_all_keeps_dangling_link_elsewhere(co, vault, tmp_path):
    """Verify a dangling link pointing outside the local folder is left alone."""
    elsewhere = co.worktree / ".claude" / "rules" / "x.md"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.symlink_to(tmp_path / "missing.md")

    assert localmd.link_all(co) == []
    assert elsewhere.is_symlink()


def test_link_all_reports_legacy_conflict_and_links_others(co, md, vault):
    """Verify differing legacy and new copies are a conflict while other entries link."""
    _legacy(vault, "# old\n")
    _vault_copy(md, "# new\n")
    _vault_skill(co, "a")

    results = localmd.link_all(co)

    assert [(r.entry.name, r.status) for r in results] == [
        (NAME, "conflict"),
        ("skills/a", "created"),
    ]
    legacy = paths.legacy_local_md_path(vault, "demo")
    assert results[0].reason is not None
    assert str(legacy) in results[0].reason
    assert str(md.target) in results[0].reason
    assert results[0].remedy == localmd.KEEP_ONE
    assert results[1].reason is None
    assert not md.link.exists()
    assert not md.link.is_symlink()


def test_link_all_migrates_legacy_copy_and_links_it(co, md, vault):
    """Verify a lone legacy copy moves under local/ and is linked."""
    old = _legacy(vault)

    results = localmd.link_all(co)

    assert [(r.entry.name, r.status) for r in results] == [(NAME, "created")]
    assert not old.exists()
    assert md.link.read_text(encoding="utf-8") == "# old\n"


def _skill_tree(co: localmd.Checkout, name: str = "a") -> Path:
    folder = _checkout_skill(co, name)
    scripts = folder / "scripts"
    (scripts / "nested").mkdir(parents=True)
    run = scripts / "run.sh"
    run.write_text("#!/bin/sh\necho hi\n")
    run.chmod(0o755)
    (scripts / "nested" / "blob.bin").write_bytes(bytes(range(256)))
    return folder


def test_adopt_skill_folder_copies_tree_and_exec_bit(co, vault):
    """Verify a skill folder is copied whole with its exec bit and replaced by a link."""
    (vault / ".gitignore").unlink()
    folder = _skill_tree(co)
    before = {p.relative_to(folder): p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    skill = localmd.entry(co, "skills/a")

    result = localmd.adopt(co, skill, tracked=False)

    assert result.target == skill.target
    assert result.already_linked is False
    assert skill.link.is_symlink()
    assert skill.link.readlink() == skill.target
    target = skill.target
    after = {p.relative_to(target): p.read_bytes() for p in target.rglob("*") if p.is_file()}
    assert after == before
    assert os.access(target / "scripts" / "run.sh", os.X_OK)
    assert localmd.state(skill) is State.LINKED
    assert not [p for p in folder.parent.iterdir() if p.name.startswith(".")]
    assert not [p for p in target.parent.iterdir() if p.name.startswith(".")]
    assert "!projects/*/local/**" in (vault / ".gitignore").read_text(encoding="utf-8")


def _assert_original_intact(co: localmd.Checkout, folder: Path) -> None:
    assert folder.is_dir()
    assert not folder.is_symlink()
    assert (folder / "scripts" / "run.sh").read_text() == "#!/bin/sh\necho hi\n"
    assert not (co.local / "claude" / "skills" / "a").exists()
    assert not [p for p in folder.parent.iterdir() if p.name.startswith(".")]
    skills = co.local / "claude" / "skills"
    assert not skills.exists() or not list(skills.iterdir())


def test_adopt_folder_failure_leaves_original(co, monkeypatch):
    """Verify a failed compare or a failed link swap leaves the original folder in place."""
    folder = _skill_tree(co)
    skill = localmd.entry(co, "skills/a")

    with monkeypatch.context() as patch:
        patch.setattr(localtree, "same_tree", lambda _a, _b: False)
        with pytest.raises(LocalMdError, match="does not match"):
            localmd.adopt(co, skill, tracked=False)
    _assert_original_intact(co, folder)

    def broken_place(_link: Path, _target: Path) -> None:
        msg = "disk full"
        raise OSError(msg)

    with monkeypatch.context() as patch:
        patch.setattr(localtree, "place_link", broken_place)
        with pytest.raises(LocalMdError, match="disk full"):
            localmd.adopt(co, skill, tracked=False)
    _assert_original_intact(co, folder)

    def broken_copy(_source: Path, _dest: Path) -> bool:
        msg = "copy failed"
        raise shutil.Error(msg)

    with monkeypatch.context() as patch:
        patch.setattr(localtree, "copy_tree", broken_copy)
        with pytest.raises(LocalMdError) as caught:
            localmd.adopt(co, skill, tracked=False)
    assert "left untouched" in " ".join(caught.value.details)
    _assert_original_intact(co, folder)

    def broken_aside(_path: Path) -> Path:
        msg = "busy"
        raise OSError(msg)

    with monkeypatch.context() as patch:
        patch.setattr(localtree, "set_aside", broken_aside)
        with pytest.raises(LocalMdError) as caught:
            localmd.adopt(co, skill, tracked=False)
    assert "left untouched" in " ".join(caught.value.details)
    _assert_original_intact(co, folder)


def test_adopt_diverged_folder_failure_keeps_the_vault_copy(co, monkeypatch):
    """Verify a failed swap on an identical vault folder keeps both copies."""
    vault_copy = _vault_skill(co, "a")
    folder = _checkout_skill(co, "a")
    skill = localmd.entry(co, "skills/a")

    def broken_place(_link: Path, _target: Path) -> None:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr(localtree, "place_link", broken_place)
    with pytest.raises(LocalMdError, match="disk full"):
        localmd.adopt(co, skill, tracked=False)

    assert (vault_copy / "SKILL.md").read_text() == "# skill\n"
    assert (folder / "SKILL.md").read_text() == "# skill\n"
    assert not folder.is_symlink()


def test_adopt_names_the_set_aside_folder_when_restore_fails(co, monkeypatch):
    """Verify a set-aside folder that cannot be renamed back is named in the error."""
    folder = _skill_tree(co)
    skill = localmd.entry(co, "skills/a")
    real_aside = localtree.set_aside
    seen: list[Path] = []

    def aside_then_block(path: Path) -> Path:
        aside = real_aside(path)
        seen.append(aside)
        path.mkdir()
        (path / "blocker").write_text("x")
        return aside

    def broken_place(_link: Path, _target: Path) -> None:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr(localtree, "set_aside", aside_then_block)
    monkeypatch.setattr(localtree, "place_link", broken_place)
    with pytest.raises(LocalMdError) as caught:
        localmd.adopt(co, skill, tracked=False)

    assert str(seen[0]) in " ".join([caught.value.message, *caught.value.details])
    assert (seen[0] / "scripts" / "run.sh").is_file()
    assert folder.is_dir()


def test_adopt_refuses_folder_with_symlink(co, tmp_path):
    """Verify a folder holding a symlink is refused with the symlink named."""
    folder = _checkout_skill(co)
    inner = folder / "ref.md"
    inner.symlink_to(tmp_path / "anything.md")
    skill = localmd.entry(co, "skills/a")

    with pytest.raises(LocalMdError) as caught:
        localmd.adopt(co, skill, tracked=False)

    assert str(inner) in " ".join([caught.value.message, *caught.value.details])
    assert not skill.target.exists()
    assert not folder.is_symlink()


def test_adopt_diverged_matching_folder_just_links(co):
    """Verify an identical vault folder is kept and the checkout folder becomes a link."""
    _vault_skill(co, "a")
    _checkout_skill(co, "a")
    skill = localmd.entry(co, "skills/a")

    localmd.adopt(co, skill, tracked=False)

    assert localmd.state(skill) is State.LINKED


def test_adopt_refuses_tracked_skill(co):
    """Verify a skill git tracks is never adopted."""
    _checkout_skill(co, "a")
    skill = localmd.entry(co, "skills/a")

    with pytest.raises(LocalMdError, match="tracked"):
        localmd.adopt(co, skill, tracked=True)

    assert not skill.target.exists()


def test_adopt_rule_reports_relative_imports(co):
    """Verify a rule's relative imports are reported."""
    rules = co.worktree / ".claude" / "rules"
    rules.mkdir(parents=True)
    (rules / "r.md").write_text("see @./x.md and @/abs.md\n")
    rule = localmd.entry(co, "rules/r.md")

    result = localmd.adopt(co, rule, tracked=False)

    assert result.relative_imports == ["./x.md"]
    assert localmd.state(rule) is State.LINKED


def test_adopt_skill_skips_import_scan(co):
    """Verify a skill's files are not scanned for imports."""
    _checkout_skill(co, "a", "see @./x.md\n")

    result = localmd.adopt(co, localmd.entry(co, "skills/a"), tracked=False)

    assert result.relative_imports == []


def test_candidates_excludes_tracked_linked_foreign(co, tmp_path):
    """Verify only untracked unadopted entries and matching diverged ones are offered."""
    _checkout_skill(co, "a")
    _vault_skill(co, "b")
    localmd.link(co, localmd.entry(co, "skills/b"), tracked=False)
    foreign = co.worktree / ".claude" / "skills" / "c"
    (tmp_path / "elsewhere").mkdir()
    foreign.symlink_to(tmp_path / "elsewhere")
    tracked_file = _checkout_skill(co, "d") / "SKILL.md"
    _git("add", str(tracked_file.relative_to(co.worktree)), cwd=co.worktree)
    _vault_skill(co, "m")
    _checkout_skill(co, "m")
    _vault_skill(co, "n", "vault\n")
    _checkout_skill(co, "n", "mine\n")

    assert _names(localmd.candidates(co)) == ["skills/a", "skills/m"]


def test_checkout_records_the_vault_root(vault, repo):
    """Verify the checkout carries the vault root it was located against."""
    co = localmd.checkout(vault, "demo", repo)

    assert co.vault == vault
    assert co.local == paths.local_dir(vault, "demo")


@pytest.mark.parametrize("symlinked", [".claude", ".claude/skills"])
def test_link_all_refuses_entries_behind_a_symlinked_folder(co, md, vault, tmp_path, symlinked):
    """Verify a symlinked `.claude` or kind folder conflicts its entries while others link."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (co.worktree / symlinked).parent.mkdir(parents=True, exist_ok=True)
    (co.worktree / symlinked).symlink_to(elsewhere)
    _vault_copy(md)
    _vault_skill(co, "a")

    results = localmd.link_all(co)

    assert [(r.entry.name, r.status) for r in results] == [
        (NAME, "created"),
        ("skills/a", "conflict"),
    ]
    assert localmd.state(md) is State.LINKED
    assert list(elsewhere.rglob("*")) == []


def test_candidates_skip_entries_behind_a_symlinked_folder(co, tmp_path):
    """Verify skills reached through a symlinked `.claude` are never offered."""
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "skills" / "b").mkdir(parents=True)
    (co.worktree / ".claude").symlink_to(elsewhere)

    assert localmd.candidates(co) == []


def test_adopt_refuses_entry_behind_a_symlinked_folder(co, tmp_path):
    """Verify adopt never moves a folder reached through a symlinked `.claude`."""
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "skills" / "b").mkdir(parents=True)
    (co.worktree / ".claude").symlink_to(elsewhere)
    skill = localmd.entry(co, "skills/b")

    with pytest.raises(LocalMdError, match="symlink"):
        localmd.adopt(co, skill, tracked=False)

    assert (elsewhere / "skills" / "b").is_dir()
    assert not skill.target.exists()


def test_remove_dangling_skips_a_symlinked_kind_folder(co, vault, tmp_path):
    """Verify no link is deleted through a symlinked kind folder."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    gone = elsewhere / "gone"
    gone.symlink_to(co.local / "claude" / "skills" / "gone")
    (co.worktree / ".claude").mkdir()
    (co.worktree / ".claude" / "skills").symlink_to(elsewhere)

    assert localmd.link_all(co) == []
    assert gone.is_symlink()


def test_link_all_isolates_a_malformed_vault_entry(co, vault):
    """Verify a stray file among vault skills is a conflict and the rest still link."""
    skills = co.local / "claude" / "skills"
    skills.mkdir(parents=True)
    (skills / "README.md").write_text("stray\n")
    _vault_skill(co, "good")
    gone = co.worktree / ".claude" / "skills" / "gone"
    gone.parent.mkdir(parents=True)
    gone.symlink_to(skills / "gone")

    results = localmd.link_all(co)

    assert [(r.entry.name, r.status) for r in results] == [
        ("skills/README.md", "conflict"),
        ("skills/good", "created"),
        ("skills/gone", "removed"),
    ]
    assert not gone.is_symlink()


def test_link_all_conflicts_entries_under_a_file_kind_folder(co, md, vault):
    """Verify a regular file at `.claude/skills` conflicts skills and writes no exclude line."""
    (co.worktree / ".claude").mkdir()
    (co.worktree / ".claude" / "skills").write_text("not a folder\n")
    _vault_copy(md)
    _vault_skill(co, "a")

    results = localmd.link_all(co)

    assert [(r.entry.name, r.status) for r in results] == [
        (NAME, "created"),
        ("skills/a", "conflict"),
    ]
    assert "/.claude/skills/a" not in _exclude(co).read_text()
    assert localmd.candidates(co) == []


def test_adopt_refused_file_writes_nothing_to_the_vault(co, md, vault):
    """Verify a file refused as non-UTF-8 leaves the vault's gitignore unwritten."""
    (vault / ".gitignore").unlink()
    md.link.write_bytes(b"\xff\xfe")

    with pytest.raises(LocalMdError, match="UTF-8"):
        _adopt(co, md)

    assert not (vault / ".gitignore").exists()


def test_candidates_skip_an_unreadable_entry(co, monkeypatch):
    """Verify an entry whose contents cannot be read is not offered and does not raise."""
    _vault_skill(co, "m")
    _checkout_skill(co, "m")
    _checkout_skill(co, "a")

    def unreadable(_item: localmd.Entry) -> bool:
        msg = "permission denied"
        raise PermissionError(msg)

    monkeypatch.setattr(localmd, "contents_match", unreadable)

    assert _names(localmd.candidates(co)) == ["skills/a"]


def test_checkout_records_the_slug(vault, repo):
    """Verify the checkout carries the slug it was located for."""
    assert localmd.checkout(vault, "demo", repo).slug == "demo"


def test_link_all_isolates_an_entry_whose_state_cannot_be_read(co, monkeypatch):
    """Verify a failing link whose state also fails is a conflict, not an escape."""
    _vault_skill(co, "a")

    def broken(*_args: object, **_kwargs: object) -> localmd.LinkResult:
        msg = "io error"
        raise OSError(msg)

    monkeypatch.setattr(localmd, "link", broken)
    monkeypatch.setattr(localmd, "state", broken)

    results = localmd.link_all(co)

    assert [(r.entry.name, r.status, r.state) for r in results] == [
        ("skills/a", "conflict", State.NONE)
    ]


def test_link_writes_no_exclude_line_when_the_folder_cannot_be_made(co, monkeypatch):
    """Verify a kind folder that cannot be created leaves no exclude line behind."""
    _vault_skill(co, "a")
    skills = co.worktree / ".claude" / "skills"
    real_mkdir = Path.mkdir

    def failing_mkdir(self: Path, *args: object, **kwargs: object) -> None:
        if self == skills:
            msg = "read-only"
            raise OSError(msg)
        real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", failing_mkdir)

    results = localmd.link_all(co)

    assert [(r.entry.name, r.status) for r in results] == [("skills/a", "conflict")]
    exclude = _exclude(co)
    assert not exclude.exists() or "/.claude/skills/a" not in exclude.read_text()


def test_a_file_among_checkout_skills_is_not_an_entry(co):
    """Verify a plain file in `.claude/skills/` is never read as a skill to adopt."""
    skills = co.worktree / ".claude" / "skills"
    skills.mkdir(parents=True)
    (skills / "README.md").write_text("notes\n")
    _checkout_skill(co, "a")

    assert _names(localmd.entries(co)) == ["skills/a"]
    assert _names(localmd.candidates(co)) == ["skills/a"]


def test_a_folder_named_claude_local_md_is_not_an_entry(co, md):
    """Verify a folder at the CLAUDE.local.md path is not offered as the file."""
    md.link.mkdir()

    assert localmd.entries(co) == []


def test_a_wrong_shape_vault_entry_is_misshapen_not_an_entry(co):
    """Verify a file among vault skills is kept out of the entries and named as misshapen."""
    skills = co.local / "claude" / "skills"
    skills.mkdir(parents=True)
    (skills / "README.md").write_text("stray\n")
    _vault_skill(co, "good")

    assert _names(localmd.vault_entries(co)) == ["skills/good"]
    assert _names(localmd.entries(co)) == ["skills/good"]
    assert _names(localmd.misshapen(co)) == ["skills/README.md"]


def test_link_all_explains_a_wrong_shape_vault_entry(co):
    """Verify the conflict for a misshapen vault entry says to fix it in the vault."""
    skills = co.local / "claude" / "skills"
    skills.mkdir(parents=True)
    (skills / "README.md").write_text("stray\n")

    [result] = localmd.link_all(co)

    assert result.status == "conflict"
    assert result.reason == f"the vault copy at {skills / 'README.md'} is not a folder"
    assert result.remedy is not None
    assert "in the vault" in result.remedy
    assert "adopt" not in result.remedy


def test_link_reason_for_a_differing_copy_says_copy_over_the_vault(co, md):
    """Verify a checkout copy that differs from the vault copy is told to keep one."""
    _vault_copy(md, "vault\n")
    md.link.write_text("mine\n")

    result = _link(co, md)

    assert result.reason == f"{md.link} differs from the vault copy at {md.target}"
    assert result.remedy == (
        "Copy the newer one over the vault copy, delete the checkout copy, and run local sync."
    )


def test_link_reason_for_an_identical_copy_names_adopt(co, md):
    """Verify an identical checkout copy is pointed at adopt, which finishes the link."""
    _vault_copy(md, "same\n")
    md.link.write_text("same\n")

    result = _link(co, md)

    assert result.status == "conflict"
    assert result.remedy is not None
    assert f"local adopt {NAME}" in result.remedy


def test_link_reason_for_a_foreign_link_names_its_target(co, md, tmp_path):
    """Verify a symlink pointing elsewhere is named with where it points."""
    _vault_copy(md)
    other = tmp_path / "other.md"
    other.write_text("x\n")
    md.link.symlink_to(other)

    result = _link(co, md)

    assert result.reason == f"{md.link} is a symlink to {other.resolve()}, not to the vault copy"
    assert result.remedy is not None
    assert "local status" in result.remedy


def test_link_reason_for_a_blocked_folder_names_the_folder(co, tmp_path):
    """Verify an entry behind a symlinked `.claude` is told to fix that folder."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (co.worktree / ".claude").symlink_to(elsewhere)
    _vault_skill(co, "a")

    [result] = localmd.link_all(co)

    assert result.status == "conflict"
    assert result.reason is not None
    assert str(co.worktree / ".claude" / "skills") in result.reason
    assert result.remedy is not None
    assert "adopt" not in result.remedy


def test_link_all_reports_an_unreadable_vault_as_a_local_error(co, monkeypatch):
    """Verify a filesystem failure reading the vault becomes a reported error."""

    def broken(_co: localmd.Checkout, **_kwargs: object) -> set[str]:
        msg = "permission denied"
        raise PermissionError(msg)

    monkeypatch.setattr(localmd, "_vault_names", broken)

    with pytest.raises(LocalMdError, match="permission denied"):
        localmd.link_all(co)


def test_link_all_reports_a_failed_legacy_move_as_a_local_error(co, monkeypatch):
    """Verify an old-path copy that cannot be moved becomes a reported error."""

    def broken(_vault: Path, _slug: str) -> Path | None:
        msg = "read-only"
        raise OSError(msg)

    monkeypatch.setattr(localmd, "migrate_legacy", broken)

    with pytest.raises(LocalMdError, match="read-only"):
        localmd.link_all(co)


def test_exclude_line_escapes_glob_characters(co):
    """Verify a skill named like a glob is ignored by itself and nothing else."""
    _vault_skill(co, "a[b]")
    _checkout_skill(co, "ab")

    localmd.link_all(co)

    assert "/.claude/skills/a\\[b\\]" in _exclude(co).read_text().splitlines()
    assert localmd.ignored(co, localmd.entry(co, "skills/a[b]"))
    assert not localmd.ignored(co, localmd.entry(co, "skills/ab"))


@pytest.mark.parametrize(
    ("name", "line"),
    [
        ("a*b", "/.claude/rules/a\\*b"),
        ("a?b", "/.claude/rules/a\\?b"),
        ("a\\b", "/.claude/rules/a\\\\b"),
        ("tail ", "/.claude/rules/tail\\ "),
    ],
)
def test_exclude_line_escapes_each_metacharacter(co, name, line):
    """Verify every gitignore metacharacter in a name is written literally."""
    rule = localmd.entry(co, f"rules/{name}")
    rule.target.parent.mkdir(parents=True)
    rule.target.write_text("x\n")

    localmd.link_all(co)

    assert line in _exclude(co).read_text().splitlines()
    assert localmd.ignored(co, rule)


def test_adopt_rule_folder_reports_relative_imports_in_its_markdown(co):
    """Verify a rule folder's markdown files are scanned, and its other files are not."""
    folder = co.worktree / ".claude" / "rules" / "team"
    folder.mkdir(parents=True)
    (folder / "a.md").write_text("see @./x.md\n")
    (folder / "notes.txt").write_text("see @./y.md\n")

    result = localmd.adopt(co, localmd.entry(co, "rules/team"), tracked=False)

    assert result.relative_imports == ["./x.md"]


def test_import_sources_is_one_rule_for_every_kind(co, tmp_path):
    """Verify CLAUDE.local.md and every markdown file of a rule are scanned, nothing else."""
    rule_dir = tmp_path / "team"
    (rule_dir / "sub").mkdir(parents=True)
    (rule_dir / "a.md").write_text("x")
    (rule_dir / "sub" / "b.md").write_text("x")
    (rule_dir / "c.txt").write_text("x")
    text_rule = tmp_path / "r.txt"
    text_rule.write_text("x")
    md_rule = tmp_path / "r.md"
    md_rule.write_text("x")
    local = tmp_path / NAME
    local.write_text("x")

    assert localmd.import_sources(localmd.entry(co, "rules/team"), rule_dir) == [
        rule_dir / "a.md",
        rule_dir / "sub" / "b.md",
    ]
    assert localmd.import_sources(localmd.entry(co, "rules/r.txt"), text_rule) == []
    assert localmd.import_sources(localmd.entry(co, "rules/r.md"), md_rule) == [md_rule]
    assert localmd.import_sources(localmd.entry(co, NAME), local) == [local]
    assert localmd.import_sources(localmd.entry(co, "skills/team"), rule_dir) == []


def test_adopt_file_refuses_a_vault_copy_that_appeared_meanwhile(co, md, monkeypatch):
    """Verify a vault copy written after the state check is never replaced."""
    md.link.write_text("mine\n")
    real = localmd.bootstrap.ensure_local_ignore

    def racing(vault: Path) -> bool:
        md.target.parent.mkdir(parents=True, exist_ok=True)
        md.target.write_text("theirs\n")
        return real(vault)

    monkeypatch.setattr(localmd.bootstrap, "ensure_local_ignore", racing)

    with pytest.raises(LocalMdError, match="appeared"):
        _adopt(co, md)

    assert md.target.read_text() == "theirs\n"
    assert md.link.read_text() == "mine\n"
    assert not md.link.is_symlink()
    assert not [p for p in md.target.parent.iterdir() if p.name.startswith(".")]


@pytest.mark.parametrize("inner", ["node_modules", ".venv", "venv", ".git", "deep/node_modules"])
def test_adopt_refuses_a_folder_holding_regenerable_or_repository_folders(co, inner):
    """Verify a skill holding installed dependencies or a repository is refused by path."""
    folder = _checkout_skill(co, "a")
    (folder / inner).mkdir(parents=True)
    (folder / inner / "x").write_text("x\n")
    skill = localmd.entry(co, "skills/a")

    with pytest.raises(LocalMdError) as raised:
        localmd.adopt(co, skill, tracked=False)

    assert str(folder / inner) in raised.value.message
    assert "regenerable or a separate repository" in " ".join(raised.value.details)
    assert "reinstall" in " ".join(raised.value.details)
    assert not skill.target.exists()
    assert not skill.link.is_symlink()


def test_adopt_accepts_a_file_named_like_a_regenerable_folder(co):
    """Verify only folders of those names are refused, not files."""
    folder = _checkout_skill(co, "a")
    (folder / "venv").write_text("a note about venvs\n")

    localmd.adopt(co, localmd.entry(co, "skills/a"), tracked=False)

    assert localmd.state(localmd.entry(co, "skills/a")) is State.LINKED


@pytest.mark.parametrize("inner", [".venv/bin", "node_modules/.bin"])
def test_adopt_names_the_kept_out_folder_before_a_symlink_inside_it(co, inner):
    """Verify a virtualenv or package folder holding its usual symlinks gets its own remedy."""
    folder = _checkout_skill(co, "a")
    (folder / inner).mkdir(parents=True)
    (folder / inner / "tool").symlink_to("/usr/bin/true")
    skill = localmd.entry(co, "skills/a")

    with pytest.raises(LocalMdError) as raised:
        localmd.adopt(co, skill, tracked=False)

    assert raised.value.message == f"{folder / inner.split('/')[0]} cannot be kept in the vault"
    assert "reinstall" in " ".join(raised.value.details)


def test_copy_file_falls_back_to_an_exclusive_write_without_hard_links(tmp_path, monkeypatch):
    """Verify a filesystem without hard links still gets an exclusive, verified copy."""
    source = tmp_path / "src.md"
    source.write_bytes(b"mine\n")
    dest = tmp_path / "out" / "dest.md"

    def no_links(_self: Path, _target: Path) -> None:
        msg = "operation not supported"
        raise OSError(45, msg)

    monkeypatch.setattr(Path, "hardlink_to", no_links)

    assert localtree.copy_file(source, dest) is True
    assert dest.read_bytes() == b"mine\n"
    assert localtree.copy_file(source, dest) is False
    assert not [p for p in dest.parent.iterdir() if p.name.startswith(".")]


def test_adopt_reports_a_copy_failure_naming_the_path(co, md, monkeypatch):
    """Verify a filesystem error while copying becomes a reported error naming the target."""
    md.link.write_text("mine\n")

    def broken(_source: Path, _dest: Path) -> bool:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr(localtree, "copy_file", broken)

    with pytest.raises(LocalMdError) as raised:
        _adopt(co, md)

    assert str(md.target) in raised.value.message
    assert "disk full" in raised.value.message
    assert md.link.read_text() == "mine\n"


def test_adopt_leaves_no_partial_copy_when_the_fallback_write_fails(co, md, monkeypatch):
    """Verify a failed exclusive write removes the half-written vault copy."""
    md.link.write_text("mine\n")

    def no_links(_self: Path, _target: Path) -> None:
        msg = "operation not supported"
        raise OSError(45, msg)

    def failing_copymode(_src: Path, _dst: Path) -> None:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr(Path, "hardlink_to", no_links)
    monkeypatch.setattr(localtree.shutil, "copymode", failing_copymode)

    with pytest.raises(LocalMdError, match="disk full"):
        _adopt(co, md)

    assert not md.target.exists()
    assert md.link.read_text() == "mine\n"
    assert not md.link.is_symlink()


def test_adopt_refuses_a_misshapen_vault_copy(co, md):
    """Verify adopt names a wrong-shape vault copy instead of copying over it."""
    md.target.mkdir(parents=True)
    md.link.write_text("# mine\n")

    assert localmd.candidates(co) == []
    with pytest.raises(LocalMdError, match="is not a regular file"):
        _adopt(co, md)

    assert md.link.read_text() == "# mine\n"
    assert md.target.is_dir()


def test_adopt_linked_entry_behind_a_symlinked_folder_names_the_folder(co, tmp_path):
    """Verify an already linked entry behind a symlinked `.claude` is refused, not passed to git."""
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "skills").mkdir(parents=True)
    (co.worktree / ".claude").symlink_to(elsewhere)
    skill = localmd.entry(co, "skills/b")
    skill.target.mkdir(parents=True)
    (elsewhere / "skills" / "b").symlink_to(skill.target)

    with pytest.raises(LocalMdError, match="symlink or not a folder"):
        localmd.adopt(co, skill, tracked=False)


def test_link_all_reports_a_dangling_link_it_cannot_remove(co, vault, monkeypatch):
    """Verify a failed removal is a conflict and the other results are still returned."""
    _vault_skill(co, "a")
    gone = co.worktree / ".claude" / "skills" / "gone"
    gone.parent.mkdir(parents=True)
    gone.symlink_to(co.local / "claude" / "skills" / "gone")
    real_unlink = Path.unlink

    def refuse(self: Path, *args: object, **kwargs: object) -> None:
        if self == gone:
            msg = "denied"
            raise PermissionError(msg)
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse)

    statuses = {r.entry.name: r.status for r in localmd.link_all(co)}

    assert statuses == {"skills/a": "created", "skills/gone": "conflict"}
    assert gone.is_symlink()


# Settings kept by copy


def _settings(
    co: localmd.Checkout, *, checkout: str | None = None, vault: str | None = None
) -> localmd.Entry:
    item = localmd.settings_entry(co)
    if checkout is not None:
        item.link.parent.mkdir(parents=True, exist_ok=True)
        item.link.write_text(checkout, encoding="utf-8")
    if vault is not None:
        item.target.parent.mkdir(parents=True, exist_ok=True)
        item.target.write_text(vault, encoding="utf-8")
    return item


def test_settings_entry_paths(co):
    """Verify the settings entry maps .claude/ to the vault's claude/ folder and copies."""
    item = localmd.settings_entry(co)

    assert item.kind == "settings"
    assert item.name == "settings.local.json"
    assert item.link == co.worktree / ".claude" / "settings.local.json"
    assert item.target == co.local / "claude" / "settings.local.json"
    assert item.mode == "copy"
    assert localmd.entry(co, "skills/a").mode == "link"


@pytest.mark.parametrize(
    ("ours", "theirs", "expected"),
    [
        ('{"a": 1}', '{"a": 1}', localmd.CopyState.SAME),
        ('{"a": 1}', '{"a": 2}', localmd.CopyState.DIFFERS),
        ('{"a": 1}', None, localmd.CopyState.CHECKOUT_ONLY),
        (None, '{"a": 1}', localmd.CopyState.VAULT_ONLY),
        (None, None, None),
    ],
)
def test_copy_states(co, ours, theirs, expected):
    """Verify each pair of copies maps to its state."""
    item = _settings(co, checkout=ours, vault=theirs)

    assert localmd.copy_state(co, item, tracked=False) == expected


def test_copy_state_tracked_and_blocked(co, tmp_path):
    """Verify tracked and blocked outrank the content comparison."""
    item = _settings(co, checkout="{}", vault='{"a": 1}')
    assert localmd.copy_state(co, item, tracked=True) is localmd.CopyState.TRACKED

    shutil.rmtree(co.worktree / ".claude")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (co.worktree / ".claude").symlink_to(elsewhere)
    assert localmd.copy_state(co, item, tracked=False) is localmd.CopyState.BLOCKED


def test_copy_state_blocked_with_no_copies_is_none(co, tmp_path):
    """Verify a blocked .claude with no settings on either side reports nothing."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (co.worktree / ".claude").symlink_to(elsewhere)

    assert localmd.copy_state(co, localmd.settings_entry(co), tracked=False) is None


def test_settings_reformat_is_same(co):
    """Verify copies equal as parsed JSON are the same whatever their layout."""
    item = _settings(co, checkout='{"b": [1], "a": 2}\n', vault='{\n  "a": 2,\n  "b": [1]\n}')

    assert localmd.copy_state(co, item, tracked=False) is localmd.CopyState.SAME
    assert localmd.settings_pending(co, tracked=False) is None


def test_settings_invalid_json_summary_and_keep(co):
    """Verify an invalid side is named in the summary and still copied as bytes."""
    _settings(co, checkout="{broken", vault='{"a": 1}')

    pending = localmd.settings_pending(co, tracked=False)

    assert pending is not None
    assert pending.state is localmd.CopyState.DIFFERS
    assert pending.summary == ("the checkout's copy is not valid JSON",)
    kept = localmd.keep_settings(co, "checkout", tracked=False)
    assert kept.path.read_text(encoding="utf-8") == "{broken"


def test_settings_pending_carries_paths_times_and_summary(co):
    """Verify a pending difference names both paths, both times, and the summary."""
    item = _settings(co, checkout='{"permissions": {"allow": ["A"]}}', vault="{}")

    pending = localmd.settings_pending(co, tracked=False)

    assert pending is not None
    assert pending.checkout == item.link
    assert pending.vault == item.target
    assert pending.checkout_mtime is not None
    assert pending.checkout_mtime.endswith("Z")
    assert pending.summary == ("permissions: only in the checkout",)


def test_settings_pending_one_side(co):
    """Verify a copy on one side only is pending with no time for the missing side."""
    _settings(co, vault="{}")

    pending = localmd.settings_pending(co, tracked=False)

    assert pending is not None
    assert pending.state is localmd.CopyState.VAULT_ONLY
    assert pending.checkout_mtime is None


def test_keep_checkout_writes_vault_copy(co):
    """Verify keeping the checkout's copy writes it over the vault copy."""
    item = _settings(co, checkout='{"a": 1}', vault='{"a": 2}')

    assert localmd.keep_settings(co, "checkout", tracked=False) == localmd.KeptSettings(
        path=item.target, written=True
    )
    assert item.target.read_text(encoding="utf-8") == '{"a": 1}'
    assert item.link.read_text(encoding="utf-8") == '{"a": 1}'


def test_keep_settings_reports_no_write_when_copies_match(co):
    """Verify keeping either copy of identical settings reports that nothing was written."""
    item = _settings(co, checkout='{"a": 1}', vault='{"a": 1}')

    assert localmd.keep_settings(co, "vault", tracked=False) == localmd.KeptSettings(
        path=item.link, written=False
    )


def test_keep_vault_creates_claude_dir_and_ignores(co):
    """Verify keeping the vault's copy creates .claude/ and ignores the file."""
    item = _settings(co, vault='{"a": 2}')

    assert localmd.keep_settings(co, "vault", tracked=False) == localmd.KeptSettings(
        path=item.link, written=True
    )
    assert item.link.read_text(encoding="utf-8") == '{"a": 2}'
    assert "/.claude/settings.local.json" in _exclude(co).read_text(encoding="utf-8")


def test_settings_symlink_is_linked_and_never_copied(co, tmp_path):
    """Verify a symlinked checkout copy is reported and both keeps refuse."""
    item = _settings(co, vault='{"a": 2}')
    elsewhere = tmp_path / "other.json"
    elsewhere.write_text('{"a": 1}', encoding="utf-8")
    item.link.parent.mkdir(parents=True, exist_ok=True)
    item.link.symlink_to(elsewhere)

    assert localmd.copy_state(co, item, tracked=False) is localmd.CopyState.LINKED
    for side in ("checkout", "vault"):
        with pytest.raises(LocalMdError):
            localmd.keep_settings(co, side, tracked=False)
    assert elsewhere.read_text(encoding="utf-8") == '{"a": 1}'
    assert item.target.read_text(encoding="utf-8") == '{"a": 2}'


def test_keep_refuses_tracked_blocked_and_missing_side(co, tmp_path):
    """Verify keep refuses a tracked entry, a blocked .claude, and a missing chosen side."""
    _settings(co, vault='{"a": 2}')
    with pytest.raises(LocalMdError, match="no"):
        localmd.keep_settings(co, "checkout", tracked=False)
    with pytest.raises(LocalMdError, match="tracked"):
        localmd.keep_settings(co, "vault", tracked=True)

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (co.worktree / ".claude").symlink_to(elsewhere)
    with pytest.raises(LocalMdError):
        localmd.keep_settings(co, "vault", tracked=False)
    assert not (elsewhere / "settings.local.json").exists()


def test_link_all_and_candidates_skip_settings(co):
    """Verify linking and adoption never touch the settings entry."""
    item = _settings(co, checkout='{"a": 1}', vault='{"a": 2}')

    results = localmd.link_all(co)

    assert item.link.read_text(encoding="utf-8") == '{"a": 1}'
    assert all(result.entry.kind != "settings" for result in results)
    assert all(found.kind != "settings" for found in localmd.candidates(co))
    assert not item.link.is_symlink()


def test_settings_order_only_difference_has_a_summary(co):
    """Verify copies that differ only in list order or repetition still explain the difference."""
    _settings(
        co,
        checkout='{"permissions": {"allow": ["a", "b"]}}',
        vault='{"permissions": {"allow": ["b", "a"]}}',
    )

    pending = localmd.settings_pending(co, tracked=False)

    assert pending is not None
    assert pending.state is localmd.CopyState.DIFFERS
    assert pending.summary == ("the copies differ only in the order or repetition of list items",)


@pytest.mark.parametrize("side", ["checkout", "vault"])
def test_settings_non_regular_copy_is_blocked(co, tmp_path, side):
    """Verify a folder or a symlink in place of either copy blocks syncing and both keeps refuse."""
    item = _settings(co, checkout='{"a": 1}', vault='{"a": 2}')
    path = item.link if side == "checkout" else item.target
    path.unlink()
    if side == "checkout":
        path.mkdir()
    else:
        other = tmp_path / "outside.json"
        other.write_text('{"secret": 1}', encoding="utf-8")
        path.symlink_to(other)

    assert localmd.copy_state(co, item, tracked=False) is localmd.CopyState.BLOCKED
    for keep in ("checkout", "vault"):
        with pytest.raises(LocalMdError):
            localmd.keep_settings(co, keep, tracked=False)


def test_settings_compare_as_strict_json(co):
    """Verify true and 1 differ, and a copy holding null is valid JSON."""
    item = _settings(co, checkout='{"x": true}', vault='{"x": 1}')
    assert localmd.copy_state(co, item, tracked=False) is localmd.CopyState.DIFFERS

    _settings(co, checkout="null", vault="{}")
    pending = localmd.settings_pending(co, tracked=False)
    assert pending is not None
    assert pending.summary == ("the settings differ",)


def test_keep_settings_keeps_the_destination_mode(co):
    """Verify keeping the vault copy leaves a private checkout file private."""
    item = _settings(co, checkout='{"a": 1}', vault='{"a": 2}')
    item.link.chmod(0o600)

    localmd.keep_settings(co, "vault", tracked=False)

    assert item.link.read_text(encoding="utf-8") == '{"a": 2}'
    assert item.link.stat().st_mode & 0o777 == 0o600


def test_keep_settings_new_copy_takes_the_source_mode(co):
    """Verify a first vault copy of a private checkout file stays private."""
    item = _settings(co, checkout='{"a": 1}')
    item.link.chmod(0o600)

    localmd.keep_settings(co, "checkout", tracked=False)

    assert item.target.stat().st_mode & 0o777 == 0o600


def test_keep_checkout_ignores_the_checkout_copy(co):
    """Verify keeping the checkout's copy also hides it from git."""
    _settings(co, checkout='{"a": 1}')

    localmd.keep_settings(co, "checkout", tracked=False)

    assert "/.claude/settings.local.json" in _exclude(co).read_text(encoding="utf-8")


def test_adopt_path_file_lists_it_and_links_back(co):
    """Verify adopting a file by path moves it under claude/, lists it, and links it."""
    (co.worktree / ".claude").mkdir()
    (co.worktree / ".claude" / "t.toml").write_text("a = 1\n")
    item = localmd.path_entry(co, ".claude/t.toml")

    result = localmd.adopt(co, item, tracked=False)

    assert result.target == co.local / "claude" / "t.toml"
    assert (co.worktree / ".claude" / "t.toml").is_symlink()
    assert localmd.manifest(co).paths == (".claude/t.toml",)
    assert result.relative_imports == []


def test_adopt_path_folder_links_whole_folder(co):
    """Verify adopting a folder by path links the folder itself and skips the import scan."""
    folder = co.worktree / ".claude" / "agents"
    folder.mkdir(parents=True)
    (folder / "a.md").write_text("see @./b.md\n")

    result = localmd.adopt(co, localmd.path_entry(co, ".claude/agents"), tracked=False)

    assert folder.is_symlink()
    assert (co.local / "claude" / "agents" / "a.md").is_file()
    assert result.relative_imports == []


def test_link_all_links_listed_path_and_creates_parent(co):
    """Verify a listed path is linked into a checkout that lacks its parent folder."""
    target = co.local / "notes" / "private.md"
    target.parent.mkdir(parents=True)
    target.write_text("x\n")
    localmanifest.add(co.local, "notes/private.md")

    results = localmd.link_all(co)

    assert [(r.entry.name, r.status) for r in results] == [("notes/private.md", "created")]
    assert (co.worktree / "notes" / "private.md").resolve() == target.resolve()
    assert "/notes/private.md" in _exclude(co).read_text()


def test_link_all_removes_dangling_link_at_listed_path(co):
    """Verify a link at a listed path whose vault copy is gone is removed, the line kept."""
    localmanifest.add(co.local, ".claude/t.toml")
    (co.worktree / ".claude").mkdir()
    (co.worktree / ".claude" / "t.toml").symlink_to(co.local / "claude" / "t.toml")

    results = localmd.link_all(co)

    assert [(r.entry.name, r.status) for r in results] == [(".claude/t.toml", "removed")]
    assert not (co.worktree / ".claude" / "t.toml").is_symlink()
    assert localmd.manifest(co).paths == (".claude/t.toml",)


def test_readopt_linked_path_restores_lost_manifest_line(co):
    """Verify adopting a linked path again lists it when its manifest line was lost."""
    (co.worktree / ".claude").mkdir()
    (co.worktree / ".claude" / "t.toml").write_text("a = 1\n")
    item = localmd.path_entry(co, ".claude/t.toml")
    localmd.adopt(co, item, tracked=False)
    (co.local / "entries.toml").unlink()

    result = localmd.adopt(co, item, tracked=False)

    assert result.already_linked is True
    assert localmd.manifest(co).paths == (".claude/t.toml",)


def test_adopt_path_refused_when_manifest_broken(co):
    """Verify a manifest with problems stops adopt before anything moves."""
    (co.worktree / "x.txt").write_text("x\n")
    co.local.mkdir(parents=True)
    (co.local / "entries.toml").write_text("paths = 3\n")

    with pytest.raises(LocalMdError):
        localmd.adopt(co, localmd.path_entry(co, "x.txt"), tracked=False)

    assert not (co.worktree / "x.txt").is_symlink()
    assert not (co.local / "x.txt").exists()


def test_path_entry_blocked_by_symlinked_parent(co, tmp_path):
    """Verify a symlinked folder above a path entry blocks it and is named."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (co.worktree / "notes").symlink_to(elsewhere)
    item = localmd.path_entry(co, "notes/private.md")

    assert localmd.blocked(co, item) is True
    assert localmd.blocking_folder(co, item) == co.worktree / "notes"


def test_candidates_never_offer_path_entries(co):
    """Verify a listed path present only in the checkout is never offered."""
    localmanifest.add(co.local, "x.txt")
    (co.worktree / "x.txt").write_text("x\n")

    assert localmd.candidates(co) == []


def test_entry_falls_back_to_path_entry(co):
    """Verify a plain path that is no kind name builds a path entry."""
    item = localmd.entry(co, ".claude/t.toml")

    assert item.kind == "path"
    assert item.target == co.local / "claude" / "t.toml"
    assert localmd.entry(co, "skills/x").kind == "skill"


@pytest.mark.parametrize(
    ("raw", "kind", "name"),
    [
        ("skills/x", "skill", "skills/x"),
        (".claude/skills/x", "skill", "skills/x"),
        ("./CLAUDE.local.md", "local-md", "CLAUDE.local.md"),
        (".claude/agents/", "path", ".claude/agents"),
        (".claude/t.toml", "path", ".claude/t.toml"),
    ],
)
def test_resolve_adopt_routes(co, raw, kind, name):
    """Verify a typed name or path resolves to the entry it means."""
    item = localmd.resolve_adopt(co, raw, co.worktree)

    assert (item.kind, item.name) == (kind, name)


def test_resolve_adopt_from_subdirectory(co):
    """Verify a relative path resolves against the shell's directory, not the top level."""
    (co.worktree / ".claude").mkdir()

    item = localmd.resolve_adopt(co, "t.toml", co.worktree / ".claude")

    assert item.name == ".claude/t.toml"


@pytest.mark.parametrize("raw", ["../outside", ".", ".claude/settings.local.json", ".git/config"])
def test_resolve_adopt_refuses(co, raw):
    """Verify a path outside the checkout, the checkout, settings, or git's folder is refused."""
    with pytest.raises(LocalMdError):
        localmd.resolve_adopt(co, raw, co.worktree)


@pytest.mark.parametrize("raw", [".claude/agents/foo.md", "notes/../.claude/agents/x"])
def test_resolve_adopt_refuses_path_inside_listed_path(co, raw):
    """Verify a path inside a listed folder is refused, naming that folder."""
    localmanifest.add(co.local, ".claude/agents")

    with pytest.raises(LocalMdError, match=r"\.claude/agents"):
        localmd.resolve_adopt(co, raw, co.worktree)


def test_resolve_adopt_refuses_path_around_listed_path(co):
    """Verify a folder holding a listed path is refused, naming the listed path."""
    localmanifest.add(co.local, ".claude/agents/sub")

    with pytest.raises(LocalMdError, match=r"\.claude/agents/sub"):
        localmd.resolve_adopt(co, ".claude/agents", co.worktree)


def test_resolve_adopt_refuses_case_variant_of_listed_path(co):
    """Verify a path that differs from a listed one only in case is refused."""
    localmanifest.add(co.local, "docs")

    with pytest.raises(LocalMdError, match="only in case"):
        localmd.resolve_adopt(co, "Docs", co.worktree)


def test_resolve_adopt_names_overlap_inside_linked_folder(co):
    """Verify a path inside an adopted, linked folder reports the overlap, not the vault."""
    folder = co.worktree / ".claude" / "agents"
    folder.mkdir(parents=True)
    (folder / "reviewer.md").write_text("# reviewer\n")
    localmd.adopt(co, localmd.path_entry(co, ".claude/agents"), tracked=False)

    with pytest.raises(LocalMdError, match="overlaps"):
        localmd.resolve_adopt(co, ".claude/agents/reviewer.md", co.worktree)


def test_resolve_adopt_overlap_ignores_case(co):
    """Verify a case variant of a path inside a listed folder is refused as an overlap."""
    localmanifest.add(co.local, ".claude/agents")

    with pytest.raises(LocalMdError, match="overlaps"):
        localmd.resolve_adopt(co, ".Claude/Agents/x.md", co.worktree)


def test_resolve_adopt_relative_cwd_reads_lexically(co, monkeypatch):
    """Verify a relative base directory still reports an overlap inside a linked folder."""
    folder = co.worktree / ".claude" / "agents"
    folder.mkdir(parents=True)
    (folder / "reviewer.md").write_text("# reviewer\n")
    localmd.adopt(co, localmd.path_entry(co, ".claude/agents"), tracked=False)
    monkeypatch.chdir(co.worktree)

    with pytest.raises(LocalMdError, match="overlaps"):
        localmd.resolve_adopt(co, ".claude/agents/reviewer.md", Path())


def test_adopt_failed_listing_removes_created_folders(co, monkeypatch):
    """Verify a listing failure after the copy leaves no copy and no empty folder behind."""
    (co.worktree / "notes").mkdir()
    (co.worktree / "notes" / "private.md").write_text("x\n")

    def refuse(*_args: object) -> bool:
        msg = "no"
        raise localmanifest.ManifestError(msg)

    monkeypatch.setattr(localmd.localmanifest, "add", refuse)

    with pytest.raises(LocalMdError):
        localmd.adopt(co, localmd.path_entry(co, "notes/private.md"), tracked=False)

    assert not (co.local / "notes").exists()
    assert (co.worktree / "notes" / "private.md").read_text() == "x\n"


# Resolving a conflict the person chose how to settle


def test_relink_replaces_a_foreign_link(co, md, tmp_path):
    """Verify relink points a link aimed elsewhere at the vault copy, leaving its old target."""
    _vault_copy(md)
    other = tmp_path / "other.md"
    other.write_text("mine\n")
    md.link.symlink_to(other)

    result = localmd.relink(co, md, tracked=False)

    assert result.status == "created"
    assert localmd.state(md) is State.LINKED
    assert other.read_text() == "mine\n"


def test_relink_restores_the_old_link_when_linking_fails(co, md, tmp_path, monkeypatch):
    """Verify relink puts the person's link back when the vault copy cannot be linked."""
    _vault_copy(md)
    other = tmp_path / "other.md"
    other.write_text("mine\n")
    md.link.symlink_to(other)

    def refuse(*_args: object, **_kwargs: object) -> bool:
        msg = "no space"
        raise OSError(msg)

    monkeypatch.setattr(localmd.localtree, "create_link", refuse)

    result = localmd.relink(co, md, tracked=False)

    assert result.status == "conflict"
    assert md.link.resolve() == other.resolve()


def test_relink_refuses_a_regular_file(co, md):
    """Verify relink never removes a file, only a symlink pointing elsewhere."""
    _vault_copy(md)
    md.link.write_text("mine\n")

    with pytest.raises(LocalMdError):
        localmd.relink(co, md, tracked=False)

    assert md.link.read_text() == "mine\n"


def test_keep_copy_checkout_writes_it_over_the_vault_copy(co, md):
    """Verify keeping the checkout's copy replaces the vault copy and links it back."""
    _vault_copy(md, "vault\n")
    md.link.write_text("checkout\n")

    result = localmd.keep_copy(co, md, "checkout", tracked=False)

    assert result.status == "created"
    assert md.target.read_text() == "checkout\n"
    assert localmd.state(md) is State.LINKED


def test_keep_copy_vault_replaces_the_checkout_copy(co, md):
    """Verify keeping the vault's copy replaces the checkout copy with the link."""
    _vault_copy(md, "vault\n")
    md.link.write_text("checkout\n")

    result = localmd.keep_copy(co, md, "vault", tracked=False)

    assert result.status == "created"
    assert md.link.read_text() == "vault\n"
    assert localmd.state(md) is State.LINKED


@pytest.mark.parametrize(("side", "expected"), [("checkout", "a.md"), ("vault", "b.md")])
def test_keep_copy_handles_a_skill_folder(co, side, expected):
    """Verify keeping either copy of a skill folder leaves that folder's files, linked."""
    item = localmd.entry(co, "skills/s")
    item.target.mkdir(parents=True)
    (item.target / "b.md").write_text("vault\n")
    item.link.mkdir(parents=True)
    (item.link / "a.md").write_text("checkout\n")

    localmd.keep_copy(co, item, side, tracked=False)

    assert localmd.state(item) is State.LINKED
    assert sorted(path.name for path in item.target.iterdir()) == [expected]
    assert not [path for path in item.target.parent.iterdir() if path.name.startswith(".")]


def test_keep_copy_refuses_an_entry_that_is_not_diverged(co, md):
    """Verify keep_copy refuses when there are not two copies to choose between."""
    _vault_copy(md)

    with pytest.raises(LocalMdError):
        localmd.keep_copy(co, md, "checkout", tracked=False)


@pytest.mark.parametrize(("keep", "expected"), [("old", "# old\n"), ("local", "# new\n")])
def test_keep_legacy_keeps_the_chosen_copy_and_links_it(co, md, vault, keep, expected):
    """Verify keeping one of two differing vault copies removes the other and links."""
    old = _legacy(vault)
    _vault_copy(md, "# new\n")

    result = localmd.keep_legacy(co, keep, tracked=False)

    assert result.status == "created"
    assert not old.exists()
    assert md.target.read_text() == expected
    assert localmd.state(md) is State.LINKED


@pytest.mark.parametrize("raw", [".claude/settings.local.json", "settings.local.json"])
def test_resolve_adopt_refuses_the_settings_file(co, raw):
    """Verify adopting the settings file is refused in favor of choosing a copy."""
    (co.worktree / ".claude").mkdir()
    cwd = co.worktree / ".claude" if raw == "settings.local.json" else co.worktree

    with pytest.raises(localmd.LocalMdError, match="kept by copy, not adopted"):
        localmd.resolve_adopt(co, raw, cwd)


def test_candidates_skip_declined_names(co):
    """Verify an entry the person declined is no longer offered."""
    _checkout_skill(co, "a")
    _checkout_skill(co, "b")
    localdeclined.add(co.local, ["skills/a"])

    assert _names(localmd.candidates(co)) == ["skills/b"]


def test_candidates_honor_a_decline_from_another_checkout(co, vault, tmp_path):
    """Verify a decline recorded from one checkout silences every checkout of the project."""
    other_root = tmp_path / "other"
    other_root.mkdir()
    _git("init", "-q", ".", cwd=other_root)
    other = localmd.checkout(vault, "demo", other_root)
    _checkout_skill(other, "a")
    localdeclined.add(co.local, ["skills/a"])

    assert localmd.candidates(other) == []


def test_candidates_still_offer_when_the_declined_file_is_unparsable(co):
    """Verify a declined file that does not parse hides nothing."""
    _checkout_skill(co, "a")
    co.local.mkdir(parents=True, exist_ok=True)
    (co.local / localdeclined.DECLINED_NAME).write_text("names = [\n")

    assert _names(localmd.candidates(co)) == ["skills/a"]


def test_adopt_removes_the_name_from_the_declined_list(co):
    """Verify adopting a declined entry, named by its checkout path, clears the decline."""
    _checkout_skill(co, "a")
    localdeclined.add(co.local, ["skills/a"])
    item = localmd.resolve_adopt(co=co, raw=".claude/skills/a", cwd=co.worktree)

    localmd.adopt(co, item, tracked=False)

    assert localdeclined.read(co.local).names == ()


def test_adopt_reports_a_decline_it_could_not_clear(co, mocker):
    """Verify a failed write to the declined list leaves the adopt done and flags the name."""
    _checkout_skill(co, "a")
    localdeclined.add(co.local, ["skills/a"])
    mocker.patch.object(localdeclined, "remove", autospec=True, side_effect=OSError("disk full"))

    result = localmd.adopt(co, localmd.entry(co, "skills/a"), tracked=False)

    assert result.still_declined is True
    assert localmd.state(localmd.entry(co, "skills/a")) is State.LINKED


def test_adopt_reads_the_declined_list_once_when_nothing_is_declined(co, mocker):
    """Verify an adopt of an undeclined entry reads the declined list a single time."""
    _checkout_skill(co, "a")
    spy = mocker.spy(localdeclined, "read")

    localmd.adopt(co, localmd.entry(co, "skills/a"), tracked=False)

    assert spy.call_count == 1
