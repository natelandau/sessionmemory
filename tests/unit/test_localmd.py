"""Tests for the CLAUDE.local.md linking library."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from sessionmemory.lib import localmd, paths
from sessionmemory.lib.localmd import LocalMdError, State

if TYPE_CHECKING:
    from pathlib import Path

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


def _vault_copy(co: localmd.Checkout, text: str = "# local\n") -> None:
    co.target.parent.mkdir(parents=True, exist_ok=True)
    co.target.write_text(text)


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

    assert localmd.checkout(vault, "demo", sub).link == repo.resolve() / NAME

    linked = tmp_path / "linked"
    _git("worktree", "add", "-q", str(linked), "-b", "other", cwd=repo)

    assert localmd.checkout(vault, "demo", linked).link == linked.resolve() / NAME


def test_checkout_outside_git_raises(vault, tmp_path):
    """Verify a directory outside any work tree is refused."""
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(LocalMdError):
        localmd.checkout(vault, "demo", plain)


def test_state_none(co):
    """Verify no link and no vault copy reads none."""
    assert localmd.state(co) is State.NONE


def test_state_not_linked(co):
    """Verify a vault copy with no file in the checkout reads not linked."""
    _vault_copy(co)

    assert localmd.state(co) is State.NOT_LINKED


def test_state_unadopted(co):
    """Verify a regular file with no vault copy reads unadopted."""
    co.link.write_text("mine\n")

    assert localmd.state(co) is State.UNADOPTED


def test_state_diverged(co):
    """Verify a regular file beside a vault copy reads diverged."""
    _vault_copy(co)
    co.link.write_text("mine\n")

    assert localmd.state(co) is State.DIVERGED


def test_state_linked(co):
    """Verify a symlink to the vault copy reads linked."""
    _vault_copy(co)
    co.link.symlink_to(co.target)

    assert localmd.state(co) is State.LINKED


def test_state_linked_through_symlinked_parent(co, tmp_path):
    """Verify a target reached through a symlinked directory still reads linked."""
    _vault_copy(co)
    alias = tmp_path / "alias"
    alias.symlink_to(co.target.parent)
    co.link.symlink_to(alias / NAME)

    assert localmd.state(co) is State.LINKED


def test_state_dangling(co, tmp_path):
    """Verify a symlink to nothing reads dangling."""
    co.link.symlink_to(tmp_path / "missing")

    assert localmd.state(co) is State.DANGLING


def test_state_foreign_link(co, tmp_path):
    """Verify a symlink to some other file reads foreign link."""
    _vault_copy(co)
    other = tmp_path / "other.md"
    other.write_text("x\n")
    co.link.symlink_to(other)

    assert localmd.state(co) is State.FOREIGN_LINK


def test_contents_match(co):
    """Verify contents compare only when both are regular files."""
    assert localmd.contents_match(co) is None

    _vault_copy(co, "same\n")
    co.link.write_text("same\n")
    assert localmd.contents_match(co) is True

    co.link.write_text("different\n")
    assert localmd.contents_match(co) is False

    co.link.unlink()
    co.link.symlink_to(co.target)
    assert localmd.contents_match(co) is None


def test_link_creates_absolute_symlink_and_ignores_it(co):
    """Verify link places an absolute symlink and hides it from git."""
    _vault_copy(co)

    result = localmd.link(co)

    assert result.status == "created"
    assert co.link.readlink().is_absolute()
    assert co.link.readlink() == co.target
    assert co.ignored()


def test_link_twice_is_present_and_one_exclude_entry(co):
    """Verify a second link changes nothing and writes one exclude entry."""
    _vault_copy(co)
    localmd.link(co)

    again = localmd.link(co)

    assert again.status == "present"
    assert again.added_ignore is False
    assert _exclude(co).read_text().splitlines().count(NAME) == 1


def test_link_replaces_dangling_symlink(co, tmp_path):
    """Verify a dangling symlink is replaced by the link."""
    _vault_copy(co)
    co.link.symlink_to(tmp_path / "missing")

    result = localmd.link(co)

    assert result.status == "created"
    assert localmd.state(co) is State.LINKED


def test_link_refuses_regular_file_as_conflict(co):
    """Verify a regular file beside a vault copy is left untouched."""
    _vault_copy(co)
    co.link.write_text("mine\n")

    result = localmd.link(co)

    assert result.status == "conflict"
    assert result.state is State.DIVERGED
    assert co.link.read_text() == "mine\n"
    assert not co.link.is_symlink()


def test_link_without_vault_copy_is_none(co):
    """Verify nothing is linked when the vault has no copy."""
    result = localmd.link(co)

    assert result.status == "none"
    assert not co.link.exists()
    assert not co.link.is_symlink()


def test_link_refuses_tracked_file(co):
    """Verify a committed CLAUDE.local.md is never replaced."""
    _vault_copy(co)
    co.link.write_text("committed\n")
    _git("add", NAME, cwd=co.worktree)
    _git("commit", "-q", "-m", "track", cwd=co.worktree)
    co.link.unlink()

    with pytest.raises(LocalMdError):
        localmd.link(co)


def test_link_unadopted_file_is_none_and_untouched(co):
    """Verify a regular file with no vault copy is never linked over."""
    _vault_copy(co)
    co.target.unlink()
    co.link.write_text("mine\n")

    result = localmd.link(co)

    assert result.status == "none"
    assert co.link.read_text() == "mine\n"
    assert not co.link.is_symlink()


def test_link_foreign_link_is_conflict(co, tmp_path):
    """Verify a symlink to some other file is left pointing there."""
    _vault_copy(co)
    other = tmp_path / "other.md"
    other.write_text("x\n")
    co.link.symlink_to(other)

    result = localmd.link(co)

    assert result.status == "conflict"
    assert result.state is State.FOREIGN_LINK
    assert co.link.readlink() == other


def test_link_tracked_file_without_vault_copy_is_none(co):
    """Verify a tracked file is not an error while the vault holds no copy."""
    co.link.write_text("committed\n")
    _git("add", NAME, cwd=co.worktree)
    _git("commit", "-q", "-m", "track", cwd=co.worktree)

    assert localmd.link(co).status == "none"
    assert co.link.read_text() == "committed\n"


def test_adopt_moves_file_and_links(co):
    """Verify adopt copies the bytes into the vault, links, and ignores the link."""
    co.link.write_bytes(b"# mine\n")

    result = localmd.adopt(co)

    assert result.already_linked is False
    assert result.target == co.target
    assert result.added_ignore is True
    assert co.target.read_bytes() == b"# mine\n"
    assert localmd.state(co) is State.LINKED
    assert co.ignored()


def test_adopt_twice_reports_already_linked(co):
    """Verify a second adopt changes nothing."""
    co.link.write_text("mine\n")
    localmd.adopt(co)

    again = localmd.adopt(co)

    assert again.already_linked is True
    assert again.added_ignore is False
    assert co.target.read_text() == "mine\n"


def test_adopt_identical_vault_copy_just_links(co):
    """Verify an equal vault copy is kept and the file is replaced by the link."""
    _vault_copy(co, "same\n")
    co.link.write_text("same\n")

    result = localmd.adopt(co)

    assert result.already_linked is False
    assert localmd.state(co) is State.LINKED
    assert co.target.read_text() == "same\n"


def test_adopt_refuses_differing_vault_copy(co):
    """Verify differing copies are both left alone and both named."""
    _vault_copy(co, "vault\n")
    co.link.write_text("mine\n")

    with pytest.raises(LocalMdError) as caught:
        localmd.adopt(co)

    text = " ".join([caught.value.message, *caught.value.details])
    assert str(co.link) in text
    assert str(co.target) in text
    assert co.link.read_text() == "mine\n"
    assert not co.link.is_symlink()
    assert co.target.read_text() == "vault\n"


def test_adopt_refuses_missing_file(co):
    """Verify adopting with nothing to adopt names the work tree."""
    with pytest.raises(LocalMdError, match=r"no CLAUDE\.local\.md in"):
        localmd.adopt(co)


def test_adopt_refuses_dangling_and_not_linked(co, tmp_path):
    """Verify a dangling link and a lone vault copy are both refused."""
    co.link.symlink_to(tmp_path / "missing")
    with pytest.raises(LocalMdError, match=r"no CLAUDE\.local\.md in"):
        localmd.adopt(co)

    co.link.unlink()
    _vault_copy(co)
    with pytest.raises(LocalMdError, match=r"no CLAUDE\.local\.md in"):
        localmd.adopt(co)


def test_adopt_refuses_foreign_link(co, tmp_path):
    """Verify a link to another file is refused and names where it points."""
    other = tmp_path / "other.md"
    other.write_text("x\n")
    co.link.symlink_to(other)

    with pytest.raises(LocalMdError) as caught:
        localmd.adopt(co)

    assert str(other) in " ".join([caught.value.message, *caught.value.details])
    assert co.link.readlink() == other


def test_adopt_refuses_tracked_file(co):
    """Verify a committed CLAUDE.local.md is never adopted."""
    co.link.write_text("committed\n")
    _git("add", NAME, cwd=co.worktree)
    _git("commit", "-q", "-m", "track", cwd=co.worktree)

    with pytest.raises(LocalMdError, match="tracked"):
        localmd.adopt(co)

    assert not co.target.exists()
    assert not co.link.is_symlink()


def test_adopt_refuses_non_utf8(co):
    """Verify bytes that are not UTF-8 are refused with nothing changed."""
    co.link.write_bytes(b"\xff\xfe")

    with pytest.raises(LocalMdError):
        localmd.adopt(co)

    assert not co.target.exists()
    assert not co.link.is_symlink()
    assert co.link.read_bytes() == b"\xff\xfe"


def test_adopt_empty_file(co):
    """Verify an empty file is adopted like any other."""
    co.link.write_bytes(b"")

    localmd.adopt(co)

    assert co.target.read_bytes() == b""
    assert localmd.state(co) is State.LINKED


def test_adopt_reports_relative_imports(co):
    """Verify relative imports are reported so the reader can move their targets."""
    co.link.write_text("see @./sibling.md and @/abs.md\n")

    assert localmd.adopt(co).relative_imports == ["./sibling.md"]


def test_ensure_ignored_creates_info_dir(co):
    """Verify the exclude file is created when .git/info is missing."""
    info = co.worktree / ".git" / "info"
    if info.exists():
        for child in info.iterdir():
            child.unlink()
        info.rmdir()

    assert localmd.ensure_ignored(co) is True
    assert co.ignored()


def test_ensure_ignored_adds_newline_before_entry(co):
    """Verify an exclude file without a trailing newline keeps its last line."""
    exclude = _exclude(co)
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("foo")

    localmd.ensure_ignored(co)

    assert exclude.read_text() == f"foo\n{NAME}\n"


def test_ensure_ignored_skips_when_gitignore_covers_it(co):
    """Verify an existing ignore rule means no exclude write."""
    (co.worktree / ".gitignore").write_text(f"{NAME}\n")

    assert localmd.ensure_ignored(co) is False
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
    co = localmd.Checkout(worktree=plain, link=plain / "CLAUDE.local.md", target=tmp_path / "t")

    with pytest.raises(LocalMdError):
        co.ignored()


def test_link_present_adds_a_missing_ignore_entry(co):
    """Verify linking an already linked checkout still makes git ignore the link."""
    _vault_copy(co)
    co.link.symlink_to(co.target)

    result = localmd.link(co)

    assert result.status == "present"
    assert result.added_ignore is True
    assert co.ignored()


def test_adopt_already_linked_adds_a_missing_ignore_entry(co):
    """Verify adopting an already linked checkout still makes git ignore the link."""
    _vault_copy(co)
    co.link.symlink_to(co.target)

    result = localmd.adopt(co)

    assert result.already_linked is True
    assert result.added_ignore is True
    assert co.ignored()


def test_link_creates_without_a_temporary_name(co, monkeypatch):
    """Verify linking into an empty path places the symlink directly, with no rename."""
    _vault_copy(co)

    def no_replace(*_args) -> None:
        msg = "place_link must not run when nothing is at the link path"
        raise AssertionError(msg)

    monkeypatch.setattr(localmd, "place_link", no_replace)

    assert localmd.link(co).status == "created"
    assert localmd.state(co) is State.LINKED
    assert not list(co.worktree.glob(".CLAUDE.local.md.*.tmp"))


def test_link_reports_a_file_that_appears_before_the_link(co, monkeypatch):
    """Verify a file written after the state was read is never overwritten."""
    _vault_copy(co)
    real_ensure = localmd.ensure_ignored

    def racing_ensure(checkout: localmd.Checkout) -> bool:
        checkout.link.write_text("mine\n")
        return real_ensure(checkout)

    monkeypatch.setattr(localmd, "ensure_ignored", racing_ensure)

    result = localmd.link(co)

    assert result.status == "conflict"
    assert result.state is State.DIVERGED
    assert co.link.read_text() == "mine\n"


def test_create_link_refuses_an_existing_entry(tmp_path):
    """Verify create_link never replaces what is already at the path."""
    link = tmp_path / NAME
    link.write_text("x")

    assert localmd.create_link(link, tmp_path / "target") is False
    assert link.read_text() == "x"


def test_adopt_removes_a_copy_that_does_not_read_back(co, monkeypatch):
    """Verify a vault copy that fails its read-back is removed and the original kept."""
    co.link.write_text("mine\n")

    def bad_write(path: Path, _text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("corrupt\n")

    monkeypatch.setattr(localmd.atomic, "write_text", bad_write)

    with pytest.raises(LocalMdError, match="does not match"):
        localmd.adopt(co)

    assert not co.target.exists()
    assert co.link.read_text() == "mine\n"
    assert not co.link.is_symlink()


def test_ensure_ignored_does_not_repeat_an_entry_a_gitignore_negates(co):
    """Verify an exclude entry overridden by a `.gitignore` negation is written once."""
    (co.worktree / ".gitignore").write_text(f"!{NAME}\n")

    assert localmd.ensure_ignored(co) is True
    assert localmd.ensure_ignored(co) is False

    assert _exclude(co).read_text().count(NAME) == 1


def test_adopt_refuses_a_directory(co):
    """Verify a directory at the link path is refused rather than read as a file."""
    co.link.mkdir()

    with pytest.raises(localmd.LocalMdError, match="not a regular file"):
        localmd.adopt(co)


def test_git_calls_ignore_an_ambient_git_dir(co, tmp_path, monkeypatch):
    """Verify GIT_DIR and GIT_INDEX_FILE from a running hook do not retarget the checkout."""
    other = tmp_path / "other"
    other.mkdir()
    _git("init", "-q", ".", cwd=other)
    (other / NAME).write_text("x", encoding="utf-8")
    _git("add", "-f", NAME, cwd=other)
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other / ".git" / "index"))

    assert co.tracked() is False
    assert localmd.ensure_ignored(co) is True

    monkeypatch.delenv("GIT_DIR")
    monkeypatch.delenv("GIT_INDEX_FILE")
    assert co.ignored() is True
    assert NAME not in (other / ".git" / "info" / "exclude").read_text(encoding="utf-8")
