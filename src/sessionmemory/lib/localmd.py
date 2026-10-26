"""Link a checkout's local entries to the copies kept in the vault.

An entry is one thing a person keeps out of the project's git history but wants in every
checkout: `CLAUDE.local.md`, a skill folder under `.claude/skills/`, a rule under
`.claude/rules/`, or `.claude/settings.local.json`. This is the one place the states of an
entry are known. The vault holds each entry under the project's `local/` folder so it
survives a lost machine and is shared by every checkout of a project.

Every entry but the settings file is linked: each checkout holds a symlink to the vault
copy. Claude Code refuses to write through a symlinked settings file, so that one is
copied instead, and only ever in the direction the person chooses. It is never part of
`entries`, so no link operation can reach it.

Entries are discovered from both sides rather than configured, and a hidden name is never
an entry, so a `.DS_Store` or a set-aside folder left by a crash is skipped.

A link is absolute. The vault's root differs from machine to machine and a linked
worktree sits at a different depth from the main checkout, so no relative path is
correct for both. Paths are taken from the git work tree's top level, which is where
Claude Code looks for them, rather than from the registered root or the working directory.

The ignore entry goes in `info/exclude`, never in a `.gitignore`, because a
`.gitignore` is a committed file and linking must not leave a change to commit. Git
reads `info/exclude` from the shared git directory, so one entry covers every linked
worktree of a repository.
"""

from __future__ import annotations

import datetime as dt
import enum
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from sessionmemory.lib import atomic, bootstrap, gitinfo, localtree, paths, settingsdiff

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Iterable

Kind = Literal["local-md", "skill", "rule", "settings"]
Mode = Literal["link", "copy"]
SETTINGS_NAME = "settings.local.json"
# Folders adopt refuses inside a skill or rule: installed dependencies, virtualenvs, and
# repositories.
KEPT_OUT_FOLDERS = frozenset({"node_modules", ".venv", "venv", ".git"})
LinkStatus = Literal["created", "present", "conflict", "tracked", "removed"]

_KIND_ORDER: dict[Kind, int] = {"local-md": 0, "skill": 1, "rule": 2, "settings": 3}
# Each folder kind's directory name, shared by the checkout's `.claude/` and the vault's
# `claude/`, so a checkout path maps to a vault path by swapping one prefix.
_KIND_DIRS: dict[Kind, str] = {"skill": "skills", "rule": "rules"}
# What each kind must be, as messages name it.
SHAPES: dict[Kind, str] = {
    "local-md": "regular file",
    "skill": "folder",
    "rule": "file or a folder",
    "settings": "regular file",
}
_CHECKOUT_CLAUDE = ".claude"
# No leading dot, since Finder, Obsidian, and `ls` hide dot-folders in the vault.
_VAULT_CLAUDE = "claude"

# For two vault copies, where deleting either leaves one for link to use.
KEEP_ONE = "Keep one of them, delete the other, then run local sync."
# For a checkout copy beside the vault copy, where only the vault copy survives a link.
REPLACE_VAULT_COPY = (
    "copy the newer one over the vault copy, delete the checkout copy, and run local sync"
)

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
    """What a checkout holds at an entry's link path, relative to the vault copy."""

    LINKED = "linked"
    NOT_LINKED = "not linked"
    UNADOPTED = "unadopted"
    DIVERGED = "diverged"
    FOREIGN_LINK = "foreign link"
    DANGLING = "dangling"
    NONE = "none"


class CopyState(enum.StrEnum):
    """How the checkout's copy of a copied entry relates to the vault copy."""

    SAME = "same"
    DIFFERS = "differs"
    CHECKOUT_ONLY = "checkout only"
    VAULT_ONLY = "vault only"
    LINKED = "linked"
    TRACKED = "tracked"
    BLOCKED = "blocked"


_PENDING_STATES = frozenset({CopyState.DIFFERS, CopyState.CHECKOUT_ONLY, CopyState.VAULT_ONLY})


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
        command = next((arg for arg in args if not arg.startswith("-")), args[0])
        msg = f"git could not run in {cwd}"
        raise LocalMdError(msg, [f"git {command} did not start or did not finish in time"])
    return done


@dataclass(frozen=True)
class Checkout:
    """One git work tree, and the vault root, slug, and `local/` folder of its project."""

    worktree: Path
    vault: Path
    slug: str
    local: Path


@dataclass(frozen=True)
class Entry:
    """One managed name, with where it sits in the checkout and in the vault."""

    kind: Kind
    name: str
    link: Path
    target: Path

    @property
    def mode(self) -> Mode:
        """How the entry reaches a checkout: a symlink, or a copy for the settings file."""
        return "copy" if self.kind == "settings" else "link"

    @property
    def relpath(self) -> str:
        """The link path relative to the work tree's top level, as git names it."""
        if self.kind == "local-md":
            return self.name
        return f"{_CHECKOUT_CLAUDE}/{self.name}"


def checkout(vault: Path, slug: str, cwd: Path) -> Checkout:
    """Locate the work tree holding `cwd` and the vault's `local/` folder for `slug`.

    Args:
        vault (Path): The vault root.
        slug (str): The project's slug.
        cwd (Path): Any directory inside the checkout.

    Returns:
        Checkout: The work tree's top level and the project it belongs to.

    Raises:
        LocalMdError: When `cwd` is not inside a git work tree.
    """
    done = _git(cwd, "rev-parse", "--show-toplevel")
    toplevel = done.stdout.strip()
    if done.returncode != 0 or not toplevel:
        msg = f"{cwd} is not inside a git work tree"
        raise LocalMdError(msg)
    return Checkout(
        worktree=Path(toplevel).resolve(),
        vault=vault,
        slug=slug,
        local=paths.local_dir(vault, slug),
    )


def entry(co: Checkout, name: str) -> Entry:
    """Build the entry a person names, as `CLAUDE.local.md`, `skills/<n>`, or `rules/<n>`.

    Args:
        co (Checkout): The checkout the entry belongs to.
        name (str): The entry's name.

    Returns:
        Entry: The entry with its checkout and vault paths.

    Raises:
        LocalMdError: When `name` is not a valid entry name.
    """
    if name == paths.LOCAL_MD_FILE:
        return Entry(kind="local-md", name=name, link=co.worktree / name, target=co.local / name)
    if name == SETTINGS_NAME:
        return Entry(
            kind="settings",
            name=name,
            link=co.worktree / _CHECKOUT_CLAUDE / name,
            target=co.local / _VAULT_CLAUDE / name,
        )
    prefix, sep, child = name.partition("/")
    kind = next((k for k, folder in _KIND_DIRS.items() if folder == prefix), None)
    if kind is None or not sep or not child or "/" in child or child.startswith("."):
        msg = f"{name!r} is not an entry name"
        raise LocalMdError(
            msg,
            [f"Use {paths.LOCAL_MD_FILE}, skills/<name>, or rules/<name>, with no leading dot."],
        )
    return Entry(
        kind=kind,
        name=name,
        link=co.worktree / _CHECKOUT_CLAUDE / prefix / child,
        target=co.local / _VAULT_CLAUDE / prefix / child,
    )


def _visible_children(folder: Path) -> list[str]:
    """List the non-hidden names directly inside `folder`, or none when it is absent."""
    if not folder.is_dir():
        return []
    return [child.name for child in folder.iterdir() if not child.name.startswith(".")]


def _present(path: Path, kind: Kind) -> bool:
    """Report whether `path` holds something of the shape `kind` expects."""
    if kind in {"local-md", "settings"}:
        return path.is_file()
    if kind == "skill":
        return path.is_dir()
    return path.is_file() or path.is_dir()


def _names_in(base: Path, claude: str) -> dict[str, tuple[Kind, Path]]:
    """Map every name found under one side's root to its kind and path, whatever its shape."""
    found: dict[str, tuple[Kind, Path]] = {}
    top = base / paths.LOCAL_MD_FILE
    if top.is_symlink() or top.exists():
        found[paths.LOCAL_MD_FILE] = ("local-md", top)
    for kind, folder in _KIND_DIRS.items():
        for child in _visible_children(base / claude / folder):
            found[f"{folder}/{child}"] = (kind, base / claude / folder / child)
    return found


def _vault_names(co: Checkout, *, shaped: bool = True) -> set[str]:
    """Name the vault's entries of the right shape, or with `shaped` unset, the wrong one."""
    return {
        name
        for name, (kind, path) in _names_in(co.local, _VAULT_CLAUDE).items()
        if _present(path, kind) is shaped
    }


def _checkout_names(co: Checkout) -> set[str]:
    """Name each checkout entry that is a symlink or of its kind's shape.

    A plain file among the skills is not a skill, so it is never offered for adoption.
    """
    return {
        name
        for name, (kind, path) in _names_in(co.worktree, _CHECKOUT_CLAUDE).items()
        if path.is_symlink() or _present(path, kind)
    }


def _sorted(entries: Iterable[Entry]) -> list[Entry]:
    return sorted(entries, key=lambda e: (_KIND_ORDER[e.kind], e.name))


def entries(co: Checkout) -> list[Entry]:
    """List every entry on either side, so one held only in the checkout is seen too.

    Args:
        co (Checkout): The checkout to inspect.

    Returns:
        list[Entry]: The entries, `CLAUDE.local.md` first, then skills, then rules, each
            by name.
    """
    return _sorted(entry(co, name) for name in _vault_names(co) | _checkout_names(co))


def vault_entries(co: Checkout) -> list[Entry]:
    """List the entries the vault holds, ignoring anything only the checkout has."""
    return _sorted(entry(co, name) for name in _vault_names(co))


def misshapen(co: Checkout) -> list[Entry]:
    """List the vault's entries whose shape their kind cannot hold, such as a file among skills.

    Args:
        co (Checkout): The checkout whose project's vault folder is read.

    Returns:
        list[Entry]: The entries in the vault that are never linked until fixed there.
    """
    return _sorted(entry(co, name) for name in _vault_names(co, shaped=False))


def shape_problem(item: Entry) -> str:
    """Name what is wrong with a misshapen vault entry, for link's conflict warning.

    Args:
        item (Entry): An entry from `misshapen`.

    Returns:
        str: One line naming the vault path and the shape it should have.
    """
    return f"the vault copy at {item.target} is not a {SHAPES[item.kind]}"


def target_misshapen(item: Entry) -> bool:
    """Report whether the vault holds something at the entry's path of the wrong shape.

    `state` reads such a vault copy as absent, so adopt must check this before copying
    over it.

    Args:
        item (Entry): The entry to check.

    Returns:
        bool: True when something sits at the vault path but is not of the kind's shape.
    """
    exists = item.target.is_symlink() or item.target.exists()
    return exists and not _present(item.target, item.kind)


def legacy_conflict(legacy: Path, target: Path) -> str:
    """Name both vault copies of CLAUDE.local.md when they differ and neither was moved.

    Args:
        legacy (Path): The copy at the project folder's top.
        target (Path): The copy in `local/`.

    Returns:
        str: The one line every command prints before `KEEP_ONE`.
    """
    return f"{legacy} and {target} differ, so {paths.LOCAL_MD_FILE} was not moved into local/"


def state(item: Entry) -> State:
    """Classify what the checkout holds at the entry's link path.

    Args:
        item (Entry): The entry to inspect.

    Returns:
        State: The link's state.
    """
    target_present = _present(item.target, item.kind)
    if item.link.is_symlink():
        if not item.link.exists():
            return State.DANGLING
        if target_present and item.link.resolve() == item.target.resolve():
            return State.LINKED
        return State.FOREIGN_LINK
    if item.link.exists():
        return State.DIVERGED if target_present else State.UNADOPTED
    return State.NOT_LINKED if target_present else State.NONE


def contents_match(item: Entry) -> bool | None:
    """Compare the checkout's copy with the vault copy, byte for byte and file by file.

    Args:
        item (Entry): The entry to inspect.

    Returns:
        bool | None: None unless both sides are regular files or both are folders, which
            a link never is.
    """
    if item.link.is_symlink() or not _present(item.target, item.kind):
        return None
    if item.link.is_file() and item.target.is_file():
        return item.link.read_bytes() == item.target.read_bytes()
    if item.link.is_dir() and item.target.is_dir():
        return localtree.same_tree(item.link, item.target)
    return None


def _kind_folder(co: Checkout, kind: Kind) -> Path:
    return co.worktree / _CHECKOUT_CLAUDE / _KIND_DIRS[kind]


def _blocked_folders(co: Checkout, kind: Kind) -> bool:
    """Report whether `.claude/` or the kind's folder is a symlink or not a folder.

    Writing through a symlink would place links wherever it points, git refuses a
    pathspec beyond one, and nothing can be placed under a file, so such entries are
    never linked, adopted, or asked about.
    """
    folder = _kind_folder(co, kind)
    return any(
        path.is_symlink() or (path.exists() and not path.is_dir())
        for path in (folder.parent, folder)
    )


def blocked(co: Checkout, item: Entry) -> bool:
    """Report whether the entry sits behind a symlinked or non-folder `.claude/` path.

    Git refuses to answer for such a path, so a caller asking `ignored` about it must
    check this first.

    Args:
        co (Checkout): The checkout holding the entry.
        item (Entry): The entry to check.

    Returns:
        bool: True when nothing may be linked, adopted, or asked of git for the entry.
    """
    if item.kind == "local-md":
        return False
    if item.kind == "settings":
        claude = co.worktree / _CHECKOUT_CLAUDE
        return claude.is_symlink() or (claude.exists() and not claude.is_dir())
    return _blocked_folders(co, item.kind)


def tracked_names(co: Checkout, items: list[Entry]) -> set[str]:
    """Name the entries git tracks, in one git call however many entries there are.

    Args:
        co (Checkout): The checkout to inspect.
        items (list[Entry]): The entries to check.

    Returns:
        set[str]: The names of entries whose path, or any file under it, git tracks.
            An entry under a symlinked or non-folder `.claude/` path is never reported,
            since git cannot be asked about it.

    Raises:
        LocalMdError: When git cannot list the index.
    """
    by_path = {item.relpath: item.name for item in items if not blocked(co, item)}
    if not by_path:
        return set()
    # Literal, so a name holding `*` or `?` matches only itself.
    done = _git(co.worktree, "--literal-pathspecs", "ls-files", "-z", "--", *by_path)
    if done.returncode != 0:
        msg = f"git ls-files failed in {co.worktree}"
        raise LocalMdError(msg, [done.stderr.strip()] if done.stderr.strip() else [])
    found: set[str] = set()
    for listed in filter(None, done.stdout.split("\0")):
        for rel, name in by_path.items():
            if listed == rel or listed.startswith(f"{rel}/"):
                found.add(name)
    return found


def ignored(co: Checkout, item: Entry) -> bool:
    """Report whether git already ignores the entry's link path, by any rule.

    Args:
        co (Checkout): The checkout to inspect.
        item (Entry): The entry to check.

    Returns:
        bool: True when `git check-ignore` matches the link path.

    Raises:
        LocalMdError: When git fails for any reason other than "not ignored", such as
            a directory that is not a repository.
    """
    done = _git(co.worktree, "check-ignore", "-q", "--", item.relpath)
    if done.returncode not in (0, 1):
        msg = f"git check-ignore failed in {co.worktree}"
        raise LocalMdError(msg, [done.stderr.strip()] if done.stderr.strip() else [])
    return done.returncode == 0


_GLOB_CHARS = re.compile(r"([\[\]*?\\])")


def _escape_pattern(path: str) -> str:
    """Escape a path so git reads it as itself, never as a glob, negation, or comment."""
    escaped = _GLOB_CHARS.sub(r"\\\1", path)
    if escaped.startswith(("!", "#")):
        escaped = f"\\{escaped}"
    # Git strips trailing spaces from a pattern unless each one is escaped.
    stripped = escaped.rstrip(" ")
    return stripped + "\\ " * (len(escaped) - len(stripped))


def _exclude_line(item: Entry) -> str:
    # Skills and rules are anchored so a same-named file deeper in the tree stays visible.
    # CLAUDE.local.md keeps its bare line, so exclude files written before stay valid.
    if item.kind == "local-md":
        return item.name
    return f"/{_escape_pattern(item.relpath)}"


def ensure_ignored(co: Checkout, item: Entry) -> bool:
    """Make git ignore the entry's link path without touching any committed file.

    Args:
        co (Checkout): The checkout to hide the link in.
        item (Entry): The entry to hide.

    Returns:
        bool: True when it wrote an entry, False when git already ignored the path.

    Raises:
        LocalMdError: When git cannot name its exclude file.
    """
    if ignored(co, item):
        return False

    done = _git(co.worktree, "rev-parse", "--git-path", "info/exclude")
    if done.returncode != 0 or not done.stdout.strip():
        msg = "git did not name its exclude file"
        raise LocalMdError(msg, [done.stderr.strip()] if done.stderr.strip() else [])

    exclude = co.worktree / done.stdout.strip()
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8", errors="replace") if exclude.exists() else ""
    line = _exclude_line(item)
    # A `.gitignore` negation outranks info/exclude, so an entry already there would only
    # be appended again on every call without ever taking effect.
    if line.strip() in (existing_line.strip() for existing_line in existing.splitlines()):
        return False
    prefix = "\n" if existing and not existing.endswith("\n") else ""
    with exclude.open("a", encoding="utf-8") as handle:
        handle.write(f"{prefix}{line}\n")
    return True


def migrate_legacy(vault: Path, slug: str) -> Path | None:
    """Move the vault copy from the project folder into `local/`, or drop an identical one.

    Args:
        vault (Path): The vault root.
        slug (str): The project's slug.

    Returns:
        Path | None: The old path when both copies exist and differ, so the caller can
        report the conflict; otherwise None.
    """
    old = paths.legacy_local_md_path(vault, slug)
    if not old.is_file():
        return None
    bootstrap.ensure_local_ignore(vault)
    new = paths.local_dir(vault, slug) / paths.LOCAL_MD_FILE
    if not new.exists():
        new.parent.mkdir(parents=True, exist_ok=True)
        old.replace(new)
        return None
    if new.read_bytes() == old.read_bytes():
        old.unlink()
        return None
    return old


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


@dataclass(frozen=True)
class LinkResult:
    """The outcome of linking one entry, with why and how to resolve it for a conflict."""

    entry: Entry
    status: LinkStatus
    state: State
    reason: str | None = None
    remedy: str | None = None


def _conflict_reason(co: Checkout, item: Entry, current: State) -> tuple[str, str]:
    """Name why the entry was not linked and the remedy that fits that cause."""
    if blocked(co, item):
        folder = _kind_folder(co, item.kind)
        return (
            f"{folder} or its parent is a symlink or not a folder, so {item.name} was not linked",
            "Replace it with a real folder, then run local sync.",
        )
    if current is State.FOREIGN_LINK:
        return (
            f"{item.link} is a symlink to {item.link.resolve()}, not to the vault copy",
            "Remove it and run local sync to link the vault copy, or run local status to compare.",
        )
    if current is State.DIVERGED:
        try:
            match = contents_match(item)
        except OSError:
            match = None
        if match is True:
            return (
                f"{item.link} is a copy identical to the vault copy",
                f"Run local sync --adopt {item.name} to replace it with the link.",
            )
        return (
            f"{item.link} differs from the vault copy at {item.target}",
            f"{REPLACE_VAULT_COPY[0].upper()}{REPLACE_VAULT_COPY[1:]}.",
        )
    return (
        f"something appeared at {item.link} while it was being linked",
        "Run local status to see what is there.",
    )


def link(co: Checkout, item: Entry, *, tracked: bool) -> LinkResult:
    """Link one entry to its vault copy when that is safe.

    Args:
        co (Checkout): The checkout to link.
        item (Entry): An entry the vault holds.
        tracked (bool): Whether git tracks the entry's path, from `tracked_names`.

    Returns:
        LinkResult: `tracked` when git tracks the path and nothing was changed, `present`
            when already linked, `created` when it placed the link, and `conflict` when a
            file, a foreign link, or a symlinked or non-folder `.claude/` path is in the
            way and nothing was changed. `present` and `created` also make git ignore
            the link path.

    Raises:
        LocalMdError: When the vault holds no copy of the entry.
    """
    current = state(item)

    def conflict(now: State) -> LinkResult:
        reason, remedy = _conflict_reason(co, item, now)
        return LinkResult(item, "conflict", now, reason=reason, remedy=remedy)

    if not _present(item.target, item.kind):
        msg = f"the vault holds no {item.name}"
        raise LocalMdError(msg, [f"Nothing is at {item.target}."])
    if blocked(co, item):
        return conflict(current)
    if tracked:
        return LinkResult(item, "tracked", current)
    if current is State.LINKED:
        ensure_ignored(co, item)
        return LinkResult(item, "present", current)
    if current in {State.DIVERGED, State.FOREIGN_LINK}:
        return conflict(current)

    # The folder first, so a failure leaves no exclude line; the line before the link,
    # so the link never exists unignored.
    item.link.parent.mkdir(parents=True, exist_ok=True)
    ensure_ignored(co, item)
    if current is State.DANGLING:
        localtree.place_link(item.link, item.target)
    elif not localtree.create_link(item.link, item.target):
        return conflict(state(item))
    return LinkResult(item, "created", current)


def _points_into(link_path: Path, folder: Path) -> bool:
    """Report whether a symlink's target lies under `folder`, though it may not exist."""
    try:
        destination = (link_path.parent / link_path.readlink()).resolve()
    except (OSError, RuntimeError):
        # A symlink loop has no destination to judge, so it is left alone.
        return False
    return destination.is_relative_to(folder.resolve())


def _remove_dangling(co: Checkout) -> list[LinkResult]:
    """Delete links left pointing into `local/` after a vault entry was removed or renamed.

    A dangling link pointing anywhere else is the person's own and is left alone.
    """
    removed: list[LinkResult] = []
    for kind, folder in _KIND_DIRS.items():
        if _blocked_folders(co, kind):
            continue
        for name in sorted(_visible_children(_kind_folder(co, kind))):
            item = entry(co, f"{folder}/{name}")
            if (
                item.link.is_symlink()
                and not item.link.exists()
                and _points_into(item.link, co.local)
            ):
                try:
                    item.link.unlink()
                except OSError as err:
                    reason = f"could not remove {item.link}, whose vault entry is gone: {err}"
                    removed.append(LinkResult(item, "conflict", State.DANGLING, reason=reason))
                    continue
                removed.append(LinkResult(item, "removed", State.DANGLING))
    return removed


def _safe_state(item: Entry) -> State:
    """Classify the entry for a failure report, reading an unreadable one as none."""
    try:
        return state(item)
    except OSError:
        return State.NONE


def tracked_all(co: Checkout) -> set[str]:
    """Name every entry git tracks, on either side and the settings file, in one git call.

    A caller that links, offers, and compares in one run passes this set to each step, so
    the run lists git's index once.

    Args:
        co (Checkout): The checkout to inspect.

    Returns:
        set[str]: The names of the entries git tracks.

    Raises:
        LocalMdError: When git cannot list the index.
    """
    names = {item.name: item for item in entries(co)}
    for name in (paths.LOCAL_MD_FILE, SETTINGS_NAME):
        names.setdefault(name, entry(co, name))
    return tracked_names(co, list(names.values()))


def link_all(co: Checkout, *, tracked: set[str] | None = None) -> list[LinkResult]:
    """Link every entry the vault holds, after moving a legacy `CLAUDE.local.md` copy.

    One entry's failure never stops the others, so a malformed vault entry or a link
    that cannot be placed is reported as a `conflict` and the rest proceed.

    Args:
        co (Checkout): The checkout to link.
        tracked (set[str] | None): The tracked names from `tracked_all`, or None to ask git.

    Returns:
        list[LinkResult]: One result per vault-side entry, misshapen ones included, then
            one per dangling link removed. `CLAUDE.local.md` is a `conflict` naming both
            paths when its legacy and new vault copies differ.

    Raises:
        LocalMdError: When git cannot list the index, the vault folder cannot be read,
            or an old-path copy cannot be moved.
    """
    try:
        legacy = migrate_legacy(co.vault, co.slug)
    except OSError as err:
        msg = f"could not move the vault's old {paths.LOCAL_MD_FILE} into {co.local}: {err}"
        raise LocalMdError(msg) from err
    try:
        items = vault_entries(co)
        odd = {item.name for item in misshapen(co)}
    except OSError as err:
        msg = f"could not read {co.local}: {err}"
        raise LocalMdError(msg) from err
    if tracked is None:
        tracked = tracked_names(co, items)
    results: list[LinkResult] = []
    for item in _sorted([*items, *(entry(co, name) for name in odd)]):
        if item.name in odd:
            results.append(
                LinkResult(
                    item,
                    "conflict",
                    _safe_state(item),
                    reason=shape_problem(item),
                    remedy="Fix or remove it in the vault, then run local sync.",
                )
            )
        elif item.kind == "local-md" and legacy is not None:
            results.append(
                LinkResult(
                    item,
                    "conflict",
                    _safe_state(item),
                    reason=legacy_conflict(legacy, item.target),
                    remedy=KEEP_ONE,
                )
            )
        else:
            results.append(_link_contained(co, item, tracked=item.name in tracked))
    results.extend(_remove_dangling(co))
    return results


def _link_contained(co: Checkout, item: Entry, *, tracked: bool) -> LinkResult:
    """Link one entry, reporting any failure as a conflict so the next entry still runs."""
    try:
        return link(co, item, tracked=tracked)
    except LocalMdError as err:
        reason = err.message
    except OSError as err:
        reason = f"could not link {item.name}: {err}"
    return LinkResult(item, "conflict", _safe_state(item), reason=reason)


def _offerable(item: Entry) -> bool:
    """Report whether adopt could take the entry; one that cannot be read is never offered."""
    try:
        if target_misshapen(item):
            return False
        current = state(item)
        return current is State.UNADOPTED or (
            current is State.DIVERGED and contents_match(item) is True
        )
    except OSError:
        return False


def candidates(co: Checkout, *, tracked: set[str] | None = None) -> list[Entry]:
    """List the entries `adopt` may offer: untracked, and unadopted or identical.

    Args:
        co (Checkout): The checkout to inspect.
        tracked (set[str] | None): The tracked names from `tracked_all`, or None to ask git.

    Returns:
        list[Entry]: Untracked entries whose state is unadopted, or diverged with
            matching contents, which adopt finishes by linking.
    """
    pool = [item for item in entries(co) if not blocked(co, item) and _offerable(item)]
    if tracked is None:
        tracked = tracked_names(co, pool)
    return [item for item in pool if item.name not in tracked]


@dataclass(frozen=True)
class AdoptResult:
    """The outcome of adopting one entry into the vault."""

    entry: Entry
    target: Path
    already_linked: bool
    relative_imports: list[str]


def _check_shape(item: Entry) -> None:
    """Refuse a checkout copy whose shape the entry's kind cannot hold."""
    if item.kind == "local-md" and not item.link.is_file():
        msg = f"{item.link} is not a regular file"
        raise LocalMdError(msg)
    if item.kind == "skill" and not item.link.is_dir():
        msg = f"{item.link} is not a folder"
        raise LocalMdError(msg)
    if not (item.link.is_file() or item.link.is_dir()):
        msg = f"{item.link} is not a regular file or a folder"
        raise LocalMdError(msg)


def _refuse_kept_out(item: Entry) -> None:
    """Refuse a folder holding installed dependencies, a virtualenv, or a repository.

    The vault ignores the first two, so the linked copy would be missing them, and a
    nested repository is committed as an embedded one rather than its files.
    """
    inner = localtree.first_folder_named(item.link, KEPT_OUT_FOLDERS)
    if inner is not None:
        msg = f"{inner} cannot be kept in the vault"
        raise LocalMdError(
            msg,
            [
                (
                    "A node_modules, .venv, venv, or .git folder is regenerable or a separate "
                    "repository; delete or move it, adopt, then reinstall."
                )
            ],
        )


def _refuse_inner_symlink(item: Entry) -> None:
    """Refuse a folder holding a symlink, since copying one silently changes what it means."""
    inner = localtree.first_symlink(item.link)
    if inner is not None:
        msg = f"{inner} is a symlink inside {item.link}"
        raise LocalMdError(
            msg, ["Replace it with the file it points at, or remove it, then run this again."]
        )


def import_sources(item: Entry, root: Path) -> list[Path]:
    """List the files of an entry whose `@` imports Claude Code loads, for adopt and doctor.

    That is CLAUDE.local.md and every markdown file of a rule, whether the rule is one
    file or a folder. A skill's files are not memory files, so none is scanned.

    Args:
        item (Entry): The entry whose kind decides the rule.
        root (Path): The entry's file or folder, on either side.

    Returns:
        list[Path]: The files to scan, sorted.
    """
    if item.kind == "skill":
        return []
    if root.is_file():
        return [root] if item.kind == "local-md" or root.suffix == ".md" else []
    if item.kind == "rule" and root.is_dir():
        return sorted(path for path in root.rglob("*.md") if path.is_file())
    return []


def _file_text(item: Entry) -> str | None:
    """Decode a markdown entry, or return None for a file adopt copies as bytes.

    Raises:
        LocalMdError: When a markdown entry is not UTF-8.
    """
    if not import_sources(item, item.link):
        return None
    try:
        return item.link.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        msg = f"{item.link} is not valid UTF-8"
        raise LocalMdError(msg, [str(exc)]) from exc


def _write_file(item: Entry) -> None:
    """Copy a file entry into the vault and verify it reads back.

    The copy never replaces a vault copy written since the state was read, such as by
    a second adopt running at once.

    Raises:
        LocalMdError: When a vault copy appeared meanwhile or the copy does not match.
    """
    original = item.link.read_bytes()
    try:
        placed = localtree.copy_file(item.link, item.target)
    except OSError as exc:
        msg = f"could not copy {item.link} to {item.target}: {exc}"
        raise LocalMdError(msg, [f"{item.link} was left untouched."]) from exc
    if not placed:
        msg = f"{item.target} appeared in the vault while {item.name} was being adopted"
        raise LocalMdError(msg, [f"{item.link} was left untouched; run local status to compare."])
    if item.target.read_bytes() != original:
        item.target.unlink(missing_ok=True)
        msg = f"the copy at {item.target} does not match {item.link}"
        raise LocalMdError(msg, [f"{item.link} was left untouched."])


def _copy_folder(item: Entry) -> None:
    """Copy a folder entry into the vault, verified before it is placed.

    Raises:
        LocalMdError: When the copy fails or does not match the original.
    """
    try:
        copied = localtree.copy_tree(item.link, item.target)
    except OSError as exc:
        msg = f"could not copy {item.link} to {item.target}: {exc}"
        raise LocalMdError(msg, [f"{item.link} was left untouched."]) from exc
    if not copied:
        msg = f"the copy at {item.target} does not match {item.link}"
        raise LocalMdError(msg, [f"{item.link} was left untouched."])


def _folder_imports(item: Entry) -> list[str]:
    """List the relative `@` imports in a rule folder's markdown files, read before the move."""
    found: list[str] = []
    for source in import_sources(item, item.link):
        found.extend(relative_imports(source.read_text(encoding="utf-8", errors="replace")))
    return found


def _swap_folder(item: Entry, *, created: bool) -> None:
    """Replace the checkout's folder with the link, restoring it if the link fails.

    A non-empty folder cannot be renamed over, so the original is set aside to a hidden
    sibling first. A crash can leave that sibling, which is skipped as hidden and whose
    content the vault copy already holds.

    Raises:
        LocalMdError: When the folder cannot be set aside, the link cannot be placed, the
            set-aside folder cannot be put back, or it cannot be removed afterward.
    """
    try:
        aside = localtree.set_aside(item.link)
    except OSError as exc:
        if created:
            shutil.rmtree(item.target, ignore_errors=True)
        msg = f"could not move {item.link} aside: {exc}"
        raise LocalMdError(msg, [f"{item.link} was left untouched."]) from exc
    try:
        localtree.place_link(item.link, item.target)
    except OSError as exc:
        try:
            aside.rename(item.link)
        except OSError as restore:
            msg = f"could not link {item.link}, and could not put it back: {restore}"
            raise LocalMdError(
                msg, [f"Its content is at {aside}; rename it back to {item.link.name}."]
            ) from exc
        if created:
            shutil.rmtree(item.target, ignore_errors=True)
        msg = f"could not link {item.link} to {item.target}: {exc}"
        raise LocalMdError(msg, [f"{item.link} was left untouched."]) from exc
    try:
        shutil.rmtree(aside)
    except OSError as exc:
        msg = f"{item.link} is linked, but {aside} could not be removed: {exc}"
        raise LocalMdError(msg, ["The vault copy holds its content; delete it by hand."]) from exc


def _refuse_adopt(co: Checkout, item: Entry, current: State, *, tracked: bool) -> None:
    """Refuse an entry adopt cannot take, in the order a person would fix them.

    Raises:
        LocalMdError: When git tracks it, nothing is there, a symlink points elsewhere, the
            shape is wrong, a folder holds a symlink or a folder adopt keeps out, or the
            vault holds different content.
    """
    if blocked(co, item):
        msg = f"{_kind_folder(co, item.kind)} or its parent is a symlink or not a folder"
        raise LocalMdError(msg, [f"{item.name} cannot be adopted through it."])
    if tracked:
        msg = f"{item.name} is tracked by git in {co.worktree}"
        raise LocalMdError(msg, ["Remove it from the index before adopting it."])
    if target_misshapen(item):
        raise LocalMdError(
            shape_problem(item), ["Fix or remove it in the vault, then run this again."]
        )
    if current is State.FOREIGN_LINK:
        msg = f"{item.link} is a symlink to {item.link.resolve()}, not to the vault copy"
        raise LocalMdError(msg)
    if current in {State.NONE, State.NOT_LINKED, State.DANGLING}:
        msg = f"no {item.name} in {co.worktree}"
        raise LocalMdError(msg)
    _check_shape(item)
    if item.link.is_dir():
        # First, since a virtualenv or package folder holds symlinks of its own.
        _refuse_kept_out(item)
        _refuse_inner_symlink(item)
    if current is State.DIVERGED and not contents_match(item):
        msg = f"{item.link} and {item.target} differ"
        raise LocalMdError(msg, ["Keep one of them, delete the other, and run this again."])


def adopt(co: Checkout, item: Entry, *, tracked: bool) -> AdoptResult:
    """Move the checkout's copy of an entry into the vault and link it back.

    The vault copy is written and verified before the original is replaced, so a failed
    copy leaves the original untouched.

    Args:
        co (Checkout): The checkout holding the entry.
        item (Entry): The entry to adopt.
        tracked (bool): Whether git tracks the entry's path, from `tracked_names`.

    Returns:
        AdoptResult: Where the vault copy lives, whether the checkout was already
            linked, and a markdown file's relative `@` imports.

    Raises:
        LocalMdError: When there is nothing to adopt, git tracks it, the vault already
            holds different content, a symlink points elsewhere, a folder holds a
            symlink or installed dependencies, a virtualenv, or a repository, a vault
            copy appeared meanwhile, or a markdown file is not UTF-8.
    """
    current = state(item)

    def result(*, imports: list[str], already: bool = False) -> AdoptResult:
        return AdoptResult(
            entry=item, target=item.target, already_linked=already, relative_imports=imports
        )

    if current is State.LINKED and not blocked(co, item):
        ensure_ignored(co, item)
        return result(imports=[], already=True)
    _refuse_adopt(co, item, current, tracked=tracked)
    folder = item.link.is_dir()

    created = current is State.UNADOPTED
    if folder:
        imports = _folder_imports(item)
        if created:
            bootstrap.ensure_local_ignore(co.vault)
            _copy_folder(item)
        ensure_ignored(co, item)
        _swap_folder(item, created=created)
        return result(imports=imports)

    text = _file_text(item)
    if created:
        bootstrap.ensure_local_ignore(co.vault)
        _write_file(item)
    ensure_ignored(co, item)
    localtree.place_link(item.link, item.target)
    return result(imports=relative_imports(text) if text is not None else [])


def settings_entry(co: Checkout) -> Entry:
    """Return the checkout's `.claude/settings.local.json` entry, which is kept by copy.

    Args:
        co (Checkout): The checkout the entry belongs to.

    Returns:
        Entry: The settings entry with its checkout and vault paths.
    """
    return entry(co, SETTINGS_NAME)


def _same_settings(item: Entry) -> bool:
    """Compare two settings copies as parsed JSON, or as bytes when either is not JSON.

    Parsed equality means a reformat or a reordered key never asks the person to choose.
    """
    ours, theirs = settingsdiff.load(item.link), settingsdiff.load(item.target)
    if settingsdiff.INVALID in (ours, theirs):
        return item.link.read_bytes() == item.target.read_bytes()
    return settingsdiff.same(ours, theirs)


def _irregular_copy(item: Entry) -> bool:
    """Report a folder in place of either copy, or a symlink in place of the vault copy.

    A vault symlink would be read through to wherever it points, so it is never copied.
    A checkout symlink is reported as `linked` instead.
    """
    vault_odd = item.target.is_symlink() or (item.target.exists() and not item.target.is_file())
    checkout_odd = not item.link.is_symlink() and item.link.exists() and not item.link.is_file()
    return vault_odd or checkout_odd


def copy_state(co: Checkout, item: Entry, *, tracked: bool) -> CopyState | None:
    """Classify the checkout's copy of a copied entry against the vault copy.

    Args:
        co (Checkout): The checkout holding the entry.
        item (Entry): The settings entry.
        tracked (bool): Whether git tracks the entry's path, from `tracked_names`.

    Returns:
        CopyState | None: The state, or None when neither side has a copy.
    """
    if not (
        item.link.is_symlink()
        or item.link.exists()
        or item.target.is_symlink()
        or item.target.exists()
    ):
        return None
    if blocked(co, item) or _irregular_copy(item):
        return CopyState.BLOCKED
    if tracked:
        return CopyState.TRACKED
    if item.link.is_symlink():
        return CopyState.LINKED
    one_sided: dict[tuple[bool, bool], CopyState | None] = {
        (False, False): None,
        (True, False): CopyState.CHECKOUT_ONLY,
        (False, True): CopyState.VAULT_ONLY,
    }
    sides = (item.link.is_file(), item.target.is_file())
    if sides in one_sided:
        return one_sided[sides]
    return CopyState.SAME if _same_settings(item) else CopyState.DIFFERS


@dataclass(frozen=True)
class SettingsPending:
    """A settings difference waiting for the person to choose which copy to keep."""

    state: CopyState
    relpath: str
    checkout: Path
    vault: Path
    checkout_mtime: str | None
    vault_mtime: str | None
    summary: tuple[str, ...]


def _mtime(path: Path) -> str | None:
    """Return a file's modification time as ISO-8601 UTC, or None when it is absent."""
    if not path.is_file():
        return None
    stamp = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.UTC)
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def _settings_summary(item: Entry) -> tuple[str, ...]:
    ours, theirs = settingsdiff.load(item.link), settingsdiff.load(item.target)
    if ours is settingsdiff.INVALID and theirs is settingsdiff.INVALID:
        return ("neither copy is valid JSON",)
    if ours is settingsdiff.INVALID:
        return ("the checkout's copy is not valid JSON",)
    if theirs is settingsdiff.INVALID:
        return ("the vault's copy is not valid JSON",)
    # Lists compare as sets in the summary, so order or repeats alone leave it empty.
    return tuple(settingsdiff.summarize(ours, theirs)) or (
        "the copies differ only in the order or repetition of list items",
    )


def settings_pending(co: Checkout, *, tracked: bool) -> SettingsPending | None:
    """Describe the settings difference the person must resolve, if there is one.

    Args:
        co (Checkout): The checkout to inspect.
        tracked (bool): Whether git tracks the settings path.

    Returns:
        SettingsPending | None: The difference when the copies differ or only one side
            has a copy, else None.
    """
    item = settings_entry(co)
    current = copy_state(co, item, tracked=tracked)
    if current not in _PENDING_STATES:
        return None
    summary = _settings_summary(item) if current is CopyState.DIFFERS else ()
    return SettingsPending(
        state=current,
        relpath=item.relpath,
        checkout=item.link,
        vault=item.target,
        checkout_mtime=_mtime(item.link),
        vault_mtime=_mtime(item.target),
        summary=summary,
    )


def keep_settings(co: Checkout, side: Literal["checkout", "vault"], *, tracked: bool) -> Path:
    """Write the chosen settings copy over the other one, as the person decided.

    Args:
        co (Checkout): The checkout whose settings to sync.
        side (Literal["checkout", "vault"]): The copy to keep.
        tracked (bool): Whether git tracks the settings path.

    Returns:
        Path: The path that now holds the kept copy's content.

    Raises:
        LocalMdError: When the entry is tracked, behind a blocked `.claude/`, a symlink,
            or the chosen side has no copy.
    """
    item = settings_entry(co)
    current = copy_state(co, item, tracked=tracked)
    if current is CopyState.TRACKED:
        msg = f"{item.link} is tracked by git, so it is never copied"
        raise LocalMdError(msg, [f"Run git rm --cached {item.relpath} to keep it private."])
    if current is CopyState.BLOCKED and blocked(co, item):
        msg = f"{item.link.parent} is a symlink or not a folder, so nothing was copied"
        raise LocalMdError(msg, ["Replace it with a real folder, then run local sync."])
    if current is CopyState.BLOCKED:
        msg = f"a copy of {SETTINGS_NAME} is a folder or a symlink, so nothing was copied"
        raise LocalMdError(
            msg, [f"Make {item.link} and {item.target} regular files, then run local sync."]
        )
    if current is CopyState.LINKED:
        msg = f"{item.link} is a symlink, which Claude Code cannot write through"
        raise LocalMdError(msg, ["Replace it with a regular file, then run local sync."])
    source, dest = (item.link, item.target) if side == "checkout" else (item.target, item.link)
    if not source.is_file():
        msg = f"the {side} has no {SETTINGS_NAME} to keep"
        raise LocalMdError(msg, [f"Nothing is at {source}."])
    if current is CopyState.SAME:
        return dest
    data = source.read_bytes()
    # The replacement takes the umask default, and the file can hold `env` secrets, so a
    # new copy takes the source's mode rather than a world-readable one.
    mode = (dest if dest.is_file() else source).stat().st_mode & 0o777
    if side == "checkout":
        bootstrap.ensure_local_ignore(co.vault)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
    # Before the write, so the checkout's copy never exists unignored.
    ensure_ignored(co, item)
    atomic.write_bytes(dest, data)
    dest.chmod(mode)
    return dest
