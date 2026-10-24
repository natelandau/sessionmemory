"""Link a checkout's `CLAUDE.local.md` to the copy kept in the vault.

This is the one place the states of a checkout's link are known. The vault holds the
file so it survives a lost machine and is shared by every checkout of a project, and
each checkout holds a symlink to it.

The link is absolute. The vault's root differs from machine to machine and a linked
worktree sits at a different depth from the main checkout, so no relative path is
correct for both. It sits at the git work tree's top level, which is where Claude Code
looks for the file, rather than at the registered root or the working directory.

The ignore entry goes in `info/exclude`, never in a `.gitignore`, because a
`.gitignore` is a committed file and linking must not leave a change to commit. Git
reads `info/exclude` from the shared git directory, so one entry covers every linked
worktree of a repository.
"""

from __future__ import annotations

import enum
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from sessionmemory.lib import atomic, gitinfo, paths

if TYPE_CHECKING:
    import subprocess

LinkStatus = Literal["created", "present", "none", "conflict"]

_FENCE = re.compile(r"^\s*(```|~~~)")
# A list marker on an indented line opens a nested item, not an indented code block.
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")
_INLINE_CODE = re.compile(r"`[^`]*`")
# Four spaces or a tab open an indented code block.
_INDENTED_CODE = ("    ", "\t")
# Prose punctuation that follows an import, as in "see @a.md, then @b.md)".
_TRAILING = ",.;:!?)]}>'\""
# An import starts a line or follows whitespace, so the `@` in an address is not one.
_IMPORT = re.compile(r"(?:^|(?<=\s))@(\S+)")


class LocalMdError(Exception):
    """A failure to link that the caller reports rather than recovers from."""

    def __init__(self, message: str, details: list[str] | None = None) -> None:
        """Create the error.

        Args:
            message (str): The one-line reason.
            details (list[str] | None): Follow-up lines for the reader.
        """
        super().__init__(message)
        self.message = message
        self.details = details or []


class State(enum.StrEnum):
    """What a checkout holds at the link path, relative to the vault copy."""

    LINKED = "linked"
    NOT_LINKED = "not linked"
    UNADOPTED = "unadopted"
    DIVERGED = "diverged"
    FOREIGN_LINK = "foreign link"
    DANGLING = "dangling"
    NONE = "none"


def _git_safe_env() -> dict[str, str]:
    """The process environment without any `GIT_`-prefixed variable.

    A git hook exports `GIT_DIR` and `GIT_INDEX_FILE`, which would retarget these calls
    at the repository running the hook. A prefix rather than a fixed set, because the
    indexed `GIT_CONFIG_KEY_<n>` family cannot be enumerated.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git in `cwd`.

    Args:
        cwd (Path): The directory to run in.
        *args: Arguments passed to git.

    Returns:
        subprocess.CompletedProcess[str]: The finished process, whatever its exit code.

    Raises:
        LocalMdError: When git cannot be run or does not finish in time.
    """
    done = gitinfo.run_git(cwd, *args, env=_git_safe_env())
    if done is None:
        msg = f"git could not run in {cwd}"
        raise LocalMdError(msg, [f"git {args[0]} did not start or did not finish in time"])
    return done


@dataclass(frozen=True)
class Checkout:
    """One git work tree and the vault copy its link should point at."""

    worktree: Path
    link: Path
    target: Path

    def tracked(self) -> bool:
        """Report whether git tracks the link path.

        Returns:
            bool: True when `CLAUDE.local.md` is committed or staged in this checkout.
        """
        done = _git(self.worktree, "ls-files", "--error-unmatch", "--", paths.LOCAL_MD_FILE)
        return done.returncode == 0

    def ignored(self) -> bool:
        """Report whether git already ignores the link path, by any rule.

        Returns:
            bool: True when `git check-ignore` matches the link path.

        Raises:
            LocalMdError: When git fails for any reason other than "not ignored", such as
                a directory that is not a repository.
        """
        done = _git(self.worktree, "check-ignore", "-q", "--", paths.LOCAL_MD_FILE)
        if done.returncode not in (0, 1):
            msg = f"git check-ignore failed in {self.worktree}"
            raise LocalMdError(msg, [done.stderr.strip()] if done.stderr.strip() else [])
        return done.returncode == 0


def checkout(vault: Path, slug: str, cwd: Path) -> Checkout:
    """Locate the work tree holding `cwd` and the vault copy for `slug`.

    Args:
        vault (Path): The vault root.
        slug (str): The project's slug.
        cwd (Path): Any directory inside the checkout.

    Returns:
        Checkout: The work tree's top level, its link path, and the vault copy.

    Raises:
        LocalMdError: When `cwd` is not inside a git work tree.
    """
    done = _git(cwd, "rev-parse", "--show-toplevel")
    toplevel = done.stdout.strip()
    if done.returncode != 0 or not toplevel:
        msg = f"{cwd} is not inside a git work tree"
        raise LocalMdError(msg)
    worktree = Path(toplevel).resolve()
    return Checkout(
        worktree=worktree,
        link=worktree / paths.LOCAL_MD_FILE,
        target=paths.local_md_path(vault, slug),
    )


def state(co: Checkout) -> State:
    """Classify what the checkout holds at the link path.

    Args:
        co (Checkout): The checkout to inspect.

    Returns:
        State: The link's state.
    """
    target_is_file = co.target.is_file()
    if co.link.is_symlink():
        if not co.link.exists():
            return State.DANGLING
        if target_is_file and co.link.resolve() == co.target.resolve():
            return State.LINKED
        return State.FOREIGN_LINK
    if co.link.exists():
        return State.DIVERGED if target_is_file else State.UNADOPTED
    return State.NOT_LINKED if target_is_file else State.NONE


def contents_match(co: Checkout) -> bool | None:
    """Compare the checkout's file with the vault copy byte for byte.

    Args:
        co (Checkout): The checkout to inspect.

    Returns:
        bool | None: None unless both are regular files, which a link is not.
    """
    if co.link.is_symlink() or not co.link.is_file() or not co.target.is_file():
        return None
    return co.link.read_bytes() == co.target.read_bytes()


def ensure_ignored(co: Checkout) -> bool:
    """Make git ignore the link path without touching any committed file.

    Args:
        co (Checkout): The checkout to hide the link in.

    Returns:
        bool: True when it wrote an entry, False when git already ignored the path.

    Raises:
        LocalMdError: When git cannot name its exclude file.
    """
    if co.ignored():
        return False

    done = _git(co.worktree, "rev-parse", "--git-path", "info/exclude")
    if done.returncode != 0 or not done.stdout.strip():
        msg = "git did not name its exclude file"
        raise LocalMdError(msg, [done.stderr.strip()] if done.stderr.strip() else [])

    exclude = co.worktree / done.stdout.strip()
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8", errors="replace") if exclude.exists() else ""
    # A `.gitignore` negation outranks info/exclude, so an entry already there would only
    # be appended again on every call without ever taking effect.
    if paths.LOCAL_MD_FILE in (line.strip() for line in existing.splitlines()):
        return False
    prefix = "\n" if existing and not existing.endswith("\n") else ""
    with exclude.open("a", encoding="utf-8") as handle:
        handle.write(f"{prefix}{paths.LOCAL_MD_FILE}\n")
    return True


def relative_imports(text: str) -> list[str]:
    """List the `@path` imports in `text` that resolve against the file's own directory.

    Args:
        text (str): The markdown to scan.

    Returns:
        list[str]: Each import path, without its `@`, in order. Absolute and home paths
            are left out, since they mean the same file from the vault.
    """
    found: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        if opened := _FENCE.match(line):
            marker = opened.group(1)
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
            continue
        if fence is not None or (line.startswith(_INDENTED_CODE) and not _LIST_ITEM.match(line)):
            continue
        # A space rather than nothing, so text either side of a code span stays apart.
        for match in _IMPORT.finditer(_INLINE_CODE.sub(" ", line)):
            target = match.group(1).rstrip(_TRAILING)
            if target and not target.startswith(("/", "~")):
                found.append(target)
    return found


def place_link(link: Path, target: Path) -> None:
    """Point `link` at `target`, replacing a symlink or file already there.

    The symlink is built at a temporary name and renamed over `link`, so a crash leaves
    either the old entry or the new link and never neither. Use `create_link` when
    nothing is at `link`, since a crash here can leave the temporary name behind.

    Args:
        link (Path): Where the symlink goes.
        target (Path): What it points at.
    """
    temp = link.with_name(f".{link.name}.{uuid.uuid4().hex}.tmp")
    temp.unlink(missing_ok=True)
    temp.symlink_to(target)
    try:
        temp.replace(link)
    except OSError:
        temp.unlink(missing_ok=True)
        raise


def create_link(link: Path, target: Path) -> bool:
    """Point a new `link` at `target`, refusing to replace anything that appeared there.

    Creating the symlink is exclusive, so a file written at `link` after its state was
    read is never overwritten, and no temporary name is left behind.

    Args:
        link (Path): Where the symlink goes.
        target (Path): What it points at.

    Returns:
        bool: True when it created the link, False when something already sat at `link`.
    """
    try:
        link.symlink_to(target)
    except FileExistsError:
        return False
    return True


@dataclass(frozen=True)
class LinkResult:
    """The outcome of linking one checkout."""

    status: LinkStatus
    link: Path
    target: Path
    state: State
    added_ignore: bool


def link(co: Checkout) -> LinkResult:
    """Link the checkout to the vault copy when that is safe.

    Args:
        co (Checkout): The checkout to link.

    Returns:
        LinkResult: `none` when the vault holds no copy, `present` when already linked,
            `created` when it placed the link, and `conflict` when a file or foreign
            link is in the way and nothing was changed. `present` and `created` also
            make git ignore the link path.

    Raises:
        LocalMdError: When git tracks `CLAUDE.local.md`, since replacing a committed
            file would leave a change to commit.
    """
    current = state(co)

    def result(
        status: LinkStatus, *, added_ignore: bool = False, now: State | None = None
    ) -> LinkResult:
        return LinkResult(status, co.link, co.target, now or current, added_ignore)

    if not co.target.is_file():
        return result("none")
    if co.tracked():
        msg = f"{paths.LOCAL_MD_FILE} is tracked by git in {co.worktree}"
        raise LocalMdError(msg, ["Remove it from the index before linking it."])
    if current is State.LINKED:
        return result("present", added_ignore=ensure_ignored(co))
    if current in {State.DIVERGED, State.FOREIGN_LINK}:
        return result("conflict")

    added = ensure_ignored(co)
    if current is State.DANGLING:
        place_link(co.link, co.target)
    elif not create_link(co.link, co.target):
        return result("conflict", added_ignore=added, now=state(co))
    return result("created", added_ignore=added)


@dataclass(frozen=True)
class AdoptResult:
    """The outcome of adopting one checkout's file into the vault."""

    target: Path
    already_linked: bool
    relative_imports: list[str]
    added_ignore: bool


def adopt(co: Checkout) -> AdoptResult:
    """Move the checkout's `CLAUDE.local.md` into the vault and link it back.

    The vault copy is written and read back before the original is replaced, so a
    failed copy leaves the original untouched.

    Args:
        co (Checkout): The checkout holding the file to adopt.

    Returns:
        AdoptResult: Where the vault copy lives, whether the checkout was already
            linked, the file's relative `@` imports, and whether an ignore entry was
            written.

    Raises:
        LocalMdError: When there is no regular file to adopt, git tracks it, the vault
            already holds different content, a symlink points elsewhere, or the file is
            not UTF-8.
    """
    current = state(co)
    if current is State.LINKED:
        return AdoptResult(
            target=co.target,
            already_linked=True,
            relative_imports=[],
            added_ignore=ensure_ignored(co),
        )
    if co.tracked():
        msg = f"{paths.LOCAL_MD_FILE} is tracked by git in {co.worktree}"
        raise LocalMdError(msg, ["Remove it from the index before adopting it."])
    if current is State.FOREIGN_LINK:
        msg = f"{co.link} is a symlink to {co.link.resolve()}, not to the vault copy"
        raise LocalMdError(msg)
    if current in {State.NONE, State.NOT_LINKED, State.DANGLING}:
        msg = f"no {paths.LOCAL_MD_FILE} in {co.worktree}"
        raise LocalMdError(msg)
    if not co.link.is_file():
        msg = f"{co.link} is not a regular file"
        raise LocalMdError(msg)
    if current is State.DIVERGED and not contents_match(co):
        msg = f"{co.link} and {co.target} differ"
        raise LocalMdError(msg, ["Keep one of them, delete the other, and run this again."])

    original = co.link.read_bytes()
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError as exc:
        msg = f"{co.link} is not valid UTF-8"
        raise LocalMdError(msg, [str(exc)]) from exc

    if current is State.UNADOPTED:
        atomic.write_text(co.target, text)
        if co.target.read_bytes() != original:
            co.target.unlink(missing_ok=True)
            msg = f"the copy at {co.target} does not match {co.link}"
            raise LocalMdError(msg, [f"{co.link} was left untouched."])

    added = ensure_ignored(co)
    place_link(co.link, co.target)
    return AdoptResult(
        target=co.target,
        already_linked=False,
        relative_imports=relative_imports(text),
        added_ignore=added,
    )
