"""Link a checkout's local entries to the copies kept in the vault.

An entry is one thing a person keeps out of the project's git history but wants in every
checkout: `CLAUDE.local.md`, a skill folder under `.claude/skills/`, a rule under
`.claude/rules/`, `.claude/settings.local.json`, or any other file or folder adopted by its
path. An adopted path has no fixed depth, so `lib/localmanifest.py` lists each one. This
is the one place the states of an entry are known. The vault holds each entry under the
project's `local/` folder so it survives a lost machine and is shared by every checkout of
a project.

Every entry but the settings file is linked: each checkout holds a symlink to the vault
copy. Claude Code refuses to write through a symlinked settings file, so that one is
copied instead, and only ever in the direction the person chooses. It is never part of
`entries`, so no link operation can reach it.

Entries are discovered from both sides rather than configured, and a hidden name is never
an entry, so a `.DS_Store` or a set-aside folder left by a crash is skipped.

A link is absolute. The vault's root differs from machine to machine and a linked
worktree sits at a different depth from the main checkout, so no relative path is
correct for both. Paths are taken from the checkout's root: the git work tree's top
level, which is where Claude Code looks for them, rather than the registered root or the
working directory. A project folder outside git has no work tree, so its registered root
is the root, and with no history to keep a link out of, nothing there is tracked or
ignored.

In a git work tree the ignore entry goes in `info/exclude`, never in a `.gitignore`,
because a `.gitignore` is a committed file and linking must not leave a change to commit.
Git reads `info/exclude` from the shared git directory, so one entry covers every linked
worktree of a repository.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import enum
import os
import re
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from sessionmemory.lib import (
    atomic,
    bootstrap,
    gitinfo,
    localdeclined,
    localmanifest,
    localtree,
    paths,
    registry,
    settingsdiff,
)

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Iterable, Mapping

Kind = Literal["local-md", "skill", "rule", "path", "settings"]
Mode = Literal["link", "copy"]
SETTINGS_NAME = "settings.local.json"
# Folders adopt refuses inside a skill or rule: installed dependencies, virtualenvs, and
# repositories.
KEPT_OUT_FOLDERS = frozenset({"node_modules", ".venv", "venv", ".git"})
LinkStatus = Literal["created", "present", "conflict", "tracked", "removed"]

_KIND_ORDER: dict[Kind, int] = {"local-md": 0, "skill": 1, "rule": 2, "path": 3, "settings": 4}
# Each folder kind's directory name, shared by the checkout's `.claude/` and the vault's
# `claude/`, so a checkout path maps to a vault path by swapping one prefix.
_KIND_DIRS: dict[Kind, str] = {"skill": "skills", "rule": "rules"}
# What each kind must be, as messages name it.
SHAPES: dict[Kind, str] = {
    "local-md": "regular file",
    "skill": "folder",
    "rule": "file or a folder",
    "path": "file or a folder",
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


def _git(cwd: Path, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run git in `cwd`.

    Args:
        cwd (Path): The directory to run in.
        *args: Arguments passed to git.
        input_text (str | None): Text for git's standard input, or None to inherit it.

    Returns:
        subprocess.CompletedProcess[str]: The finished process, whatever its exit code.

    Raises:
        LocalMdError: When git cannot be run or does not finish in time.
    """
    done = gitinfo.run_git(cwd, *args, env=_git_safe_env(), input_text=input_text)
    if done is None:
        command = next((arg for arg in args if not arg.startswith("-")), args[0])
        msg = f"git could not run in {cwd}"
        raise LocalMdError(msg, [f"git {command} did not start or did not finish in time"])
    return done


@dataclass(frozen=True)
class Checkout:
    """The folder entries are linked into, and its project's vault root, slug, and `local/`.

    `root` is the resolved git work tree's top level, or the resolved registered root of a
    project folder outside git, where `in_git` is false. `child_roots` are the resolved
    roots of other registered projects inside it.
    """

    root: Path
    in_git: bool
    vault: Path
    slug: str
    local: Path
    child_roots: tuple[Path, ...] = ()


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
        """The link path relative to the checkout's root, as git names it."""
        if self.kind in {"local-md", "path"}:
            return self.name
        return f"{_CHECKOUT_CLAUDE}/{self.name}"


def checkout(
    vault: Path,
    slug: str,
    cwd: Path,
    worktree: Path | None = None,
    registered_root: Path | None = None,
    projects: Mapping[str, registry.Project] | None = None,
) -> Checkout:
    """Locate the root holding `cwd` and the vault's `local/` folder for `slug`.

    Args:
        vault (Path): The vault root.
        slug (str): The project's slug.
        cwd (Path): Any directory inside the checkout.
        worktree (Path | None): The work tree's top level when the caller already asked
            git for it, so git is not asked again. Ignored while a `GIT_` variable is set,
            since the caller's answer was not given under `_git_safe_env`.
        registered_root (Path | None): The project's registered root, used as the root
            when git says `cwd` is not inside a repository. Pass it only when resolution
            matched the project by path.
        projects (Mapping[str, registry.Project] | None): The loaded registry, or None to
            read it, for finding the child projects inside the root.

    Returns:
        Checkout: The root and the project it belongs to.

    Raises:
        LocalMdError: When `cwd` is not inside a git work tree and no registered root
            was given, or when git refuses to say whether it is.
    """
    if worktree is not None and not any(key.startswith("GIT_") for key in os.environ):
        return _build_checkout(
            vault=vault, slug=slug, root=worktree, in_git=True, projects=projects
        )
    done = _git(cwd, "rev-parse", "--show-toplevel")
    toplevel = done.stdout.strip()
    if done.returncode == 0 and toplevel:
        return _build_checkout(
            vault=vault, slug=slug, root=Path(toplevel), in_git=True, projects=projects
        )
    if registered_root is None:
        msg = f"{cwd} is not inside a git work tree"
        raise LocalMdError(msg)
    stderr = done.stderr.strip()
    # Any other refusal, such as `safe.directory`, may hide a repository whose links must
    # stay ignored, so only git's own verdict lets a folder count as outside git.
    if done.returncode != 0 and gitinfo.NOT_A_REPOSITORY not in stderr.lower():
        msg = f"git could not say whether {cwd} is inside a work tree"
        raise LocalMdError(msg, [stderr] if stderr else [])
    return _build_checkout(
        vault=vault, slug=slug, root=registered_root, in_git=False, projects=projects
    )


def checkout_at(
    vault: Path,
    slug: str,
    root: Path,
    projects: Mapping[str, registry.Project] | None = None,
) -> Checkout:
    """Build the checkout for a project's registered root, asking git whether it is a repo.

    For callers such as `doctor` that start from the registry rather than a working
    directory. A root inside a repository is checked at the work tree's top level, where
    sync links. When git cannot run or will not answer, the root is taken as a work tree,
    so the git-only checks raise and the caller reports that git could not be asked.

    Args:
        vault (Path): The vault root.
        slug (str): The project's slug.
        root (Path): The project's registered root, an existing directory.
        projects (Mapping[str, registry.Project] | None): The loaded registry, or None to
            read it.

    Returns:
        Checkout: The checkout for the project.
    """
    try:
        return checkout(vault=vault, slug=slug, cwd=root, registered_root=root, projects=projects)
    except LocalMdError:
        return _build_checkout(vault=vault, slug=slug, root=root, in_git=True, projects=projects)


def vault_stand_in(vault: Path, slug: str) -> Checkout:
    """Build a checkout with no folder to link into, for checks of the vault side only.

    Args:
        vault (Path): The vault root.
        slug (str): The project's slug.

    Returns:
        Checkout: A checkout rooted at the vault, outside git, with no child projects.
    """
    return Checkout(
        root=vault, in_git=False, vault=vault, slug=slug, local=paths.local_dir(vault, slug)
    )


def _build_checkout(
    vault: Path,
    slug: str,
    root: Path,
    *,
    in_git: bool,
    projects: Mapping[str, registry.Project] | None,
) -> Checkout:
    top = root.resolve()
    try:
        known = registry.load(vault) if projects is None else projects
        children = registry.find_under_root(known, str(top), exclude=slug)
    except registry.RegistryError:
        # Resolution and doctor report an unreadable registry; here it only means no
        # child is known.
        children = ()
    return Checkout(
        root=top,
        in_git=in_git,
        vault=vault,
        slug=slug,
        local=paths.local_dir(vault, slug),
        child_roots=children,
    )


def child_project(co: Checkout, relpath: str) -> Path | None:
    """Name the child folder that is its own project and holds, or is, `relpath`.

    A registered child owns its files, and a nested repository could commit a link
    placed in it, so neither may receive an entry of the project around it. The path is
    read as written, never through a symlink, so a linked entry is judged where it sits.

    Args:
        co (Checkout): The checkout the path is relative to.
        relpath (str): A path relative to the checkout's root.

    Returns:
        Path | None: The child project's folder under `co.root`, or None when the path
            belongs to this project or the checkout is a vault stand-in.
    """
    if co.root == co.vault:
        return None
    parts = Path(relpath).parts
    for depth in range(1, len(parts) + 1):
        folder = co.root.joinpath(*parts[:depth])
        if folder in co.child_roots:
            return folder
        # A linked entry's folder is the vault copy, whose contents say nothing here.
        if folder.is_symlink():
            return None
        marker = folder / ".git"
        if marker.is_symlink() or marker.exists():
            return folder
    return None


def path_entry(co: Checkout, relpath: str) -> Entry:
    """Build the entry for a path adopted by name, without judging the path.

    Args:
        co (Checkout): The checkout the entry belongs to.
        relpath (str): The path relative to the checkout's root.

    Returns:
        Entry: The entry, linked at the path and kept at its mirror under `local/`.
    """
    return Entry(
        kind="path",
        name=relpath,
        link=co.root / relpath,
        target=co.local / localmanifest.vault_relpath(relpath),
    )


def manifest(co: Checkout) -> localmanifest.Manifest:
    """Read the project's list of paths adopted by name.

    Args:
        co (Checkout): The checkout whose project's `local/` folder holds the list.

    Returns:
        localmanifest.Manifest: The usable paths and any problems in the file. A listed
            path inside a child project is a problem rather than a usable path, so it is
            reported and never linked.
    """
    found = localmanifest.read(co.local)
    kept: list[str] = []
    moved: list[str] = []
    for relpath in found.paths:
        folder = child_project(co, relpath)
        if folder is None:
            kept.append(relpath)
        else:
            link = co.root / relpath
            # A link placed before the folder became a project is still the vault copy,
            # so deleting it loses nothing and keeps the child from committing it.
            remedy = (
                f"delete the link at {link} and remove the line from entries.toml"
                if link.is_symlink()
                else "remove the line from entries.toml"
            )
            moved.append(f"{relpath} is inside {folder}, which is its own project; {remedy}")
    if not moved:
        return found
    return localmanifest.Manifest(paths=tuple(kept), problems=(*found.problems, *moved))


def declined(co: Checkout) -> localdeclined.Declined:
    """Read the project's list of entries the person declined to adopt.

    Args:
        co (Checkout): The checkout whose project's `local/` folder holds the list.

    Returns:
        localdeclined.Declined: The usable names and any problems in the file.
    """
    return localdeclined.read(co.local)


def declinable(co: Checkout, raw: str, cwd: Path, present: list[str]) -> str:
    """Return the entry name `raw` means, refusing one there is nothing to decline for.

    A name this checkout lacks is still accepted, since another checkout may hold it.

    Args:
        co (Checkout): The checkout the name is read against.
        raw (str): The name or path as typed.
        cwd (Path): The directory a relative path is read from.
        present (list[str]): The names of this checkout's entries.

    Returns:
        str: The entry's name.

    Raises:
        LocalMdError: When `raw` names no entry, differs only in case from one present,
            is adopted only by name, or is already in the vault and not offered.
    """
    try:
        item = resolve_adopt(co=co, raw=raw, cwd=cwd)
        # A checkout copy identical to the vault's is offered by sync, so it can be declined.
        offered = state(item) is State.DIVERGED and identical(item)
        in_vault = item.target.is_symlink() or item.target.exists()
    except OSError as err:
        msg = f"could not read {raw} in {co.root}: {err}"
        raise LocalMdError(msg) from err
    # candidates() compares names exactly, so a case-only spelling would silence nothing.
    other = localmanifest.same_but_case(item.name, present)
    if other is not None:
        msg = f"{item.name} differs only in case from {other}"
        raise LocalMdError(msg, [f"Decline it as {other}."])
    if item.kind == "path":
        msg = f"{item.name} is adopted only by name and is never offered, so there is nothing to decline"
        raise LocalMdError(msg)
    if in_vault and not offered:
        msg = f"{item.name} is already in the vault, so there is nothing to decline"
        raise LocalMdError(msg)
    return item.name


def decline(co: Checkout, names: list[str]) -> list[str]:
    """Record entries the person declined, so `candidates` never offers them again.

    Args:
        co (Checkout): The checkout whose project's `local/` folder holds the list.
        names (list[str]): Entry names such as `skills/<name>`.

    Returns:
        list[str]: The names that were not listed before.

    Raises:
        localdeclined.DeclinedError: When the list has problems or a name is not one an
            adopt question can offer.
    """
    # Like every write under local/, so a global ignore cannot keep the list off git.
    bootstrap.ensure_local_ignore(co.vault)
    return localdeclined.add(co.local, names)


def entry(co: Checkout, name: str) -> Entry:
    """Build the entry a name means: `CLAUDE.local.md`, `skills/<n>`, `rules/<n>`, or a path.

    Args:
        co (Checkout): The checkout the entry belongs to.
        name (str): The entry's name.

    Returns:
        Entry: The entry with its checkout and vault paths.

    Raises:
        LocalMdError: When `name` is not a valid entry name.
    """
    if name == paths.LOCAL_MD_FILE:
        return Entry(kind="local-md", name=name, link=co.root / name, target=co.local / name)
    if name == SETTINGS_NAME:
        return Entry(
            kind="settings",
            name=name,
            link=co.root / _CHECKOUT_CLAUDE / name,
            target=co.local / _VAULT_CLAUDE / name,
        )
    prefix, sep, child = name.partition("/")
    kind = next((k for k, folder in _KIND_DIRS.items() if folder == prefix), None)
    if kind is None or not sep or not child or "/" in child or child.startswith("."):
        reason = None
        if not localmanifest.is_kind_name(name):
            reason = localmanifest.name_problem(name)
            if reason is None:
                return path_entry(co, name)
        msg = f"{name!r} is not an entry name"
        usage = (
            f"Use {paths.LOCAL_MD_FILE}, skills/<name>, or rules/<name>, with no leading dot, "
            "or a path inside the checkout."
        )
        raise LocalMdError(msg, [usage, *([reason] if reason else [])])
    return Entry(
        kind=kind,
        name=name,
        link=co.root / _CHECKOUT_CLAUDE / prefix / child,
        target=co.local / _VAULT_CLAUDE / prefix / child,
    )


def _lexical(path: Path) -> Path:
    """Drop `.` segments and fold `..` into its parent without reading the file system."""
    parts: list[str] = []
    for part in path.parts:
        if part == "..":
            if len(parts) > 1:
                parts.pop()
        elif part != ".":
            parts.append(part)
    return Path(*parts)


def _work_tree_path(co: Checkout, raw: str, cwd: Path) -> str:
    """Turn a typed path into its path from the checkout's root.

    Raises:
        LocalMdError: When the path is outside the checkout or is the checkout itself.
    """
    typed = Path(raw) if Path(raw).is_absolute() else cwd.resolve() / raw
    resolved = _lexical(typed)
    # Read lexically first, so a path inside a linked folder is judged where it was typed
    # rather than in the vault. Resolving catches a checkout reached through a symlink.
    if not resolved.is_relative_to(co.root):
        resolved = typed.parent.resolve() / typed.name if typed.name else typed.resolve()
        if resolved.name in {"", ".", ".."}:
            resolved = resolved.resolve()
    try:
        relpath = resolved.relative_to(co.root).as_posix()
    except ValueError:
        msg = f"{raw} is outside the checkout at {co.root}"
        raise LocalMdError(msg) from None
    if relpath == ".":
        msg = f"{raw} is the checkout itself"
        raise LocalMdError(msg, ["Adopt a file or folder inside it."])
    return relpath


def resolve_adopt(co: Checkout, raw: str, cwd: Path) -> Entry:
    """Turn what a person named for `local adopt` into the entry it means.

    A name `local adopt` already knows (`CLAUDE.local.md`, `skills/<n>`, `rules/<n>`) keeps its
    meaning. Anything else is a path from `cwd`, and a path that lands on a known entry is
    that entry, so it keeps its kind's warnings.

    Args:
        co (Checkout): The checkout to adopt into the vault from.
        raw (str): The name or path as typed.
        cwd (Path): The directory a relative path is read from.

    Returns:
        Entry: The entry to adopt.

    Raises:
        LocalMdError: When the path is outside the checkout, the checkout itself, the
            settings file, a path adopt refuses, inside a child project, or overlaps a path
            already adopted.
    """
    if localmanifest.is_kind_name(raw):
        return entry(co, raw)
    relpath = _work_tree_path(co, raw, cwd)
    if relpath == f"{_CHECKOUT_CLAUDE}/{SETTINGS_NAME}":
        msg = f"{relpath} is kept by copy, not adopted"
        raise LocalMdError(
            msg,
            ["Choose a copy with local sync --keep-settings checkout, or --keep-settings vault."],
        )
    if relpath == paths.LOCAL_MD_FILE:
        return entry(co, relpath)
    head, _, rest = relpath.partition(f"{_CHECKOUT_CLAUDE}/")
    if not head and localmanifest.is_kind_name(rest):
        return entry(co, rest)
    reason = localmanifest.name_problem(relpath)
    if reason is not None:
        msg = f"{relpath} cannot be adopted"
        raise LocalMdError(msg, [reason])
    folder = child_project(co, relpath)
    if folder is not None:
        msg = f"{relpath} belongs to the project at {folder}"
        raise LocalMdError(msg, ["Adopt it from inside that project."])
    listed = manifest(co).paths
    other = localmanifest.same_but_case(relpath, listed)
    if other is not None:
        msg = f"{relpath} differs only in case from {other}, which is already adopted"
        raise LocalMdError(msg, [f"Adopt it as {other}."])
    other = localmanifest.outer_of(relpath, listed)
    if other is not None:
        msg = f"{relpath} overlaps {other}, which is already adopted"
        raise LocalMdError(msg, ["An adopted path cannot hold or sit inside another."])
    return path_entry(co, relpath)


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


def _listed(co: Checkout) -> list[Entry]:
    """Build an entry for each path the manifest lists."""
    return [path_entry(co, relpath) for relpath in manifest(co).paths]


def _vault_names(
    co: Checkout, *, shaped: bool = True, listed: list[Entry] | None = None
) -> set[str]:
    """Name the vault's entries of the right shape, or with `shaped` unset, the wrong one.

    `listed` is the manifest's entries when the caller already read them.
    """
    found = {
        name
        for name, (kind, path) in _names_in(co.local, _VAULT_CLAUDE).items()
        if _present(path, kind) is shaped
    }
    found.update(
        item.name
        for item in (_listed(co) if listed is None else listed)
        if (item.target.is_symlink() or item.target.exists())
        and _present(item.target, item.kind) is shaped
    )
    return found


def _checkout_names(co: Checkout, listed: list[Entry]) -> set[str]:
    """Name each checkout entry that is a symlink or of its kind's shape.

    A plain file among the skills is not a skill, so it is never offered for adoption.
    """
    found = {
        name
        for name, (kind, path) in _names_in(co.root, _CHECKOUT_CLAUDE).items()
        if path.is_symlink() or _present(path, kind)
    }
    found.update(
        item.name for item in listed if item.link.is_symlink() or _present(item.link, item.kind)
    )
    return found


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
    listed = _listed(co)
    names = _vault_names(co, listed=listed) | _checkout_names(co, listed)
    return _sorted(entry(co, name) for name in names)


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


def identical(item: Entry) -> bool:
    """Report whether the checkout copy matches the vault copy, reading an unreadable one as no.

    Args:
        item (Entry): The entry to inspect.

    Returns:
        bool: True only when both copies exist and match.
    """
    try:
        return contents_match(item) is True
    except OSError:
        return False


def _kind_folder(co: Checkout, kind: Kind) -> Path:
    return co.root / _CHECKOUT_CLAUDE / _KIND_DIRS[kind]


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


def _blocked_ancestor(co: Checkout, item: Entry) -> Path | None:
    """Return the first folder above a path entry that is a symlink or not a folder."""
    current = co.root
    for part in Path(item.relpath).parts[:-1]:
        current /= part
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            return current
    return None


def blocking_folder(co: Checkout, item: Entry) -> Path:
    """Name the folder a blocked entry sits behind, for a message.

    Args:
        co (Checkout): The checkout holding the entry.
        item (Entry): An entry `blocked` reported.

    Returns:
        Path: The kind's folder for a skill or rule, the first symlinked or non-folder
            ancestor for a path, or `.claude` otherwise.
    """
    if item.kind == "path":
        return _blocked_ancestor(co, item) or item.link.parent
    if item.kind in _KIND_DIRS:
        return _kind_folder(co, item.kind)
    return co.root / _CHECKOUT_CLAUDE


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
        claude = co.root / _CHECKOUT_CLAUDE
        return claude.is_symlink() or (claude.exists() and not claude.is_dir())
    if item.kind == "path":
        return _blocked_ancestor(co, item) is not None
    return _blocked_folders(co, item.kind)


def tracked_names(co: Checkout, items: list[Entry]) -> set[str]:
    """Name the entries git tracks, in one git call however many entries there are.

    Args:
        co (Checkout): The checkout to inspect.
        items (list[Entry]): The entries to check.

    Returns:
        set[str]: The names of entries whose path, or any file under it, git tracks.
            An entry under a symlinked or non-folder `.claude/` path is never reported,
            since git cannot be asked about it. Empty outside git, where nothing is
            tracked.

    Raises:
        LocalMdError: When git cannot list the index.
    """
    if not co.in_git:
        return set()
    by_path = {item.relpath: item.name for item in items if not blocked(co, item)}
    if not by_path:
        return set()
    # Literal, so a name holding `*` or `?` matches only itself.
    done = _git(co.root, "--literal-pathspecs", "ls-files", "-z", "--", *by_path)
    if done.returncode != 0:
        msg = f"git ls-files failed in {co.root}"
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
        bool: True when `git check-ignore` matches the link path. True outside git,
            where there is no history to hide a link from.

    Raises:
        LocalMdError: When git fails for any reason other than "not ignored", such as
            a directory that is not a repository.
    """
    if not co.in_git:
        return True
    done = _git(co.root, "check-ignore", "-q", "--", item.relpath)
    if done.returncode not in (0, 1):
        msg = f"git check-ignore failed in {co.root}"
        raise LocalMdError(msg, [done.stderr.strip()] if done.stderr.strip() else [])
    return done.returncode == 0


def ignored_names(co: Checkout, items: list[Entry]) -> set[str] | None:
    """Name the entries whose link path git already ignores, in one git call.

    Args:
        co (Checkout): The checkout to inspect.
        items (list[Entry]): The entries to check, none behind a blocked `.claude/`.

    Returns:
        set[str] | None: The names of entries `git check-ignore` matches, or None
            outside git, where ignoring does not apply.

    Raises:
        LocalMdError: When git fails for any reason other than "none ignored".
    """
    if not co.in_git:
        return None
    by_path = {item.relpath: item.name for item in items}
    if not by_path:
        return set()
    done = _git(
        co.root, "check-ignore", "--stdin", "-z", input_text="".join(f"{p}\0" for p in by_path)
    )
    if done.returncode not in (0, 1):
        msg = f"git check-ignore failed in {co.root}"
        raise LocalMdError(msg, [done.stderr.strip()] if done.stderr.strip() else [])
    return {by_path[path] for path in done.stdout.split("\0") if path in by_path}


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
        bool: True when it wrote an entry, False when the path is already hidden,
            which `ignored` reports for every path outside git.

    Raises:
        LocalMdError: When git cannot name its exclude file.
    """
    if ignored(co, item):
        return False

    done = _git(co.root, "rev-parse", "--git-path", "info/exclude")
    if done.returncode != 0 or not done.stdout.strip():
        msg = "git did not name its exclude file"
        raise LocalMdError(msg, [done.stderr.strip()] if done.stderr.strip() else [])

    exclude = co.root / done.stdout.strip()
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
        folder = blocking_folder(co, item)
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
                f"Run local adopt {shlex.quote(item.name)} to replace it with the link.",
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
    candidates: list[Entry] = []
    for kind, folder in _KIND_DIRS.items():
        if _blocked_folders(co, kind):
            continue
        candidates.extend(
            entry(co, f"{folder}/{name}")
            for name in sorted(_visible_children(_kind_folder(co, kind)))
        )
    candidates.extend(item for item in _listed(co) if not blocked(co, item))
    removed: list[LinkResult] = []
    for item in candidates:
        if item.link.is_symlink() and not item.link.exists() and _points_into(item.link, co.local):
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
    """List the entries `adopt` may offer: untracked, undeclined, and unadopted or identical.

    Args:
        co (Checkout): The checkout to inspect.
        tracked (set[str] | None): The tracked names from `tracked_all`, or None to ask git.

    Returns:
        list[Entry]: Untracked entries whose state is unadopted, or diverged with
            matching contents, which adopt finishes by linking.
    """
    # A path entry is adopted only by name, so it is never offered.
    refused = set(declined(co).names)
    pool = [
        item
        for item in entries(co)
        if item.kind != "path"
        and item.name not in refused
        and not blocked(co, item)
        and _offerable(item)
    ]
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
    still_declined: bool = False


def _clear_decline(co: Checkout, item: Entry) -> bool:
    """Drop an adopted entry from the declined list, reporting whether it is still listed.

    A failure here never undoes the adopt it follows, so it is reported, not raised.
    """
    if item.name not in declined(co).names:
        return False
    try:
        return not localdeclined.remove(co.local, item.name)
    except OSError:
        return True


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
    if item.kind in {"skill", "path"}:
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
        msg = f"{blocking_folder(co, item)} or its parent is a symlink or not a folder"
        raise LocalMdError(msg, [f"{item.name} cannot be adopted through it."])
    if tracked:
        msg = f"{item.name} is tracked by git in {co.root}"
        raise LocalMdError(msg, ["Remove it from the index before adopting it."])
    if target_misshapen(item):
        raise LocalMdError(
            shape_problem(item), ["Fix or remove it in the vault, then run this again."]
        )
    if current is State.FOREIGN_LINK:
        msg = f"{item.link} is a symlink to {item.link.resolve()}, not to the vault copy"
        raise LocalMdError(msg)
    if current in {State.NONE, State.NOT_LINKED, State.DANGLING}:
        msg = f"no {item.name} in {co.root}"
        raise LocalMdError(msg)
    _check_shape(item)
    if item.link.is_dir():
        # First, since a virtualenv or package folder holds symlinks of its own.
        _refuse_kept_out(item)
        _refuse_inner_symlink(item)
    if current is State.DIVERGED and not contents_match(item):
        msg = f"{item.link} and {item.target} differ"
        raise LocalMdError(msg, ["Keep one of them, delete the other, and run this again."])


def _list(co: Checkout, item: Entry, *, created: bool = False) -> None:
    """List a path entry in the manifest once its vault copy exists.

    Raises:
        LocalMdError: When the manifest cannot be updated. A vault copy this adopt
            created is removed first, so nothing is left unlisted.
    """
    if item.kind != "path":
        return
    try:
        localmanifest.add(co.local, item.name)
    except (localmanifest.ManifestError, OSError) as err:
        if created:
            if item.target.is_dir() and not item.target.is_symlink():
                shutil.rmtree(item.target, ignore_errors=True)
            else:
                item.target.unlink(missing_ok=True)
            _remove_empty_parents(item.target.parent, co.local)
        if isinstance(err, localmanifest.ManifestError):
            raise LocalMdError(err.message, err.details) from err
        msg = f"could not update {co.local / localmanifest.MANIFEST_NAME}: {err}"
        raise LocalMdError(msg, [f"{item.link} was left untouched."]) from err


def _remove_empty_parents(folder: Path, stop: Path) -> None:
    """Remove `folder` and each parent below `stop` while it is empty."""
    while folder != stop and folder.is_relative_to(stop):
        try:
            folder.rmdir()
        except OSError:
            return
        folder = folder.parent


def _check_manifest(co: Checkout, item: Entry) -> None:
    """Refuse a path entry before anything moves when the manifest cannot take it."""
    if item.kind != "path":
        return
    problems = manifest(co).problems
    if problems:
        msg = (
            f"{co.local / localmanifest.MANIFEST_NAME} has problems, so {item.name} was not adopted"
        )
        raise LocalMdError(msg, [*problems, "Fix the file, then run this again."])


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
            entry=item,
            target=item.target,
            already_linked=already,
            relative_imports=imports,
            still_declined=_clear_decline(co, item),
        )

    if current is State.LINKED and not blocked(co, item):
        _list(co, item)
        ensure_ignored(co, item)
        return result(imports=[], already=True)
    _refuse_adopt(co, item, current, tracked=tracked)
    _check_manifest(co, item)
    folder = item.link.is_dir()

    created = current is State.UNADOPTED
    if folder:
        imports = _folder_imports(item)
        if created:
            bootstrap.ensure_local_ignore(co.vault)
            _copy_folder(item)
        _list(co, item, created=created)
        ensure_ignored(co, item)
        try:
            _swap_folder(item, created=created)
        except LocalMdError:
            # A failed swap may have removed the copy this adopt made; drop its line too.
            if item.kind == "path" and not (item.target.is_symlink() or item.target.exists()):
                localmanifest.discard(co.local, item.name)
            raise
        return result(imports=imports)

    text = _file_text(item)
    if created:
        bootstrap.ensure_local_ignore(co.vault)
        _write_file(item)
    _list(co, item, created=created)
    ensure_ignored(co, item)
    localtree.place_link(item.link, item.target)
    return result(imports=relative_imports(text) if text is not None else [])


def relink(co: Checkout, item: Entry, *, tracked: bool) -> LinkResult:
    """Point a link aimed somewhere else at the vault copy, as the person chose.

    Only the symlink is removed, so whatever it pointed at is left as it was. When the
    vault copy cannot be linked, the old link is put back.

    Args:
        co (Checkout): The checkout holding the link.
        item (Entry): An entry whose link points away from its vault copy.
        tracked (bool): Whether git tracks the entry's path, from `tracked_names`.

    Returns:
        LinkResult: The outcome of linking once the old link is gone.

    Raises:
        LocalMdError: When the entry is not a link to somewhere else, or it cannot be removed.
    """
    if state(item) is not State.FOREIGN_LINK:
        msg = f"{item.link} is not a symlink to somewhere other than the vault copy"
        raise LocalMdError(msg)
    try:
        previous = item.link.readlink()
        item.link.unlink()
    except OSError as err:
        msg = f"could not remove {item.link}: {err}"
        raise LocalMdError(msg) from err
    result = _link_contained(co, item, tracked=tracked)
    if result.status == "created":
        return result
    try:
        if not item.link.is_symlink() and not item.link.exists():
            item.link.symlink_to(previous)
    except OSError as err:
        msg = f"could not link {item.link}, and could not restore its link to {previous}: {err}"
        raise LocalMdError(msg, [result.reason] if result.reason else []) from err
    return result


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _restore(co: Checkout, item: Entry, aside: Path, discarded: Path) -> None:
    """Put a set-aside copy back, relisting a path entry a failed adopt dropped."""
    aside.rename(discarded)
    if item.kind == "path":
        # Best effort: the error being raised already names what went wrong.
        with contextlib.suppress(localmanifest.ManifestError, OSError):
            localmanifest.add(co.local, item.name)


def keep_copy(
    co: Checkout, item: Entry, side: Literal["checkout", "vault"], *, tracked: bool
) -> LinkResult:
    """Settle an entry whose checkout and vault copies differ by keeping the chosen one.

    The discarded copy is set aside until the kept one is linked, so a failure puts it
    back. Keeping the checkout's copy runs it through `adopt`, so every check adopt makes
    still applies.

    Args:
        co (Checkout): The checkout holding the entry.
        item (Entry): An entry whose two copies differ.
        side (Literal["checkout", "vault"]): The copy to keep.
        tracked (bool): Whether git tracks the entry's path, from `tracked_names`.

    Returns:
        LinkResult: `created` once the kept copy is in the vault and linked.

    Raises:
        LocalMdError: When there are not two copies, adopt refuses the checkout copy, or
            a copy cannot be moved.
    """
    if state(item) is not State.DIVERGED:
        msg = f"{item.link} and {item.target} are not two copies to choose between"
        raise LocalMdError(msg)
    discarded = item.target if side == "checkout" else item.link
    try:
        aside = localtree.set_aside(discarded)
    except OSError as err:
        msg = f"could not move {discarded} aside: {err}"
        raise LocalMdError(msg, [f"{item.link} and {item.target} were left untouched."]) from err
    try:
        if side == "checkout":
            adopt(co, item, tracked=tracked)
            result = LinkResult(item, "created", State.DIVERGED)
        else:
            result = link(co, item, tracked=tracked)
    except (LocalMdError, OSError):
        _restore(co, item, aside, discarded)
        raise
    if result.status != "created":
        _restore(co, item, aside, discarded)
        return result
    try:
        _remove(aside)
    except OSError as err:
        msg = f"{item.link} is linked, but {aside} could not be removed: {err}"
        raise LocalMdError(msg, ["Delete it by hand."]) from err
    return result


def keep_legacy(co: Checkout, keep: Literal["old", "local"], *, tracked: bool) -> LinkResult:
    """Settle two differing vault copies of CLAUDE.local.md by keeping the chosen one.

    Args:
        co (Checkout): The checkout to link once one copy remains.
        keep (Literal["old", "local"]): The copy at the project folder's top, or the one
            in `local/`.
        tracked (bool): Whether git tracks CLAUDE.local.md, from `tracked_names`.

    Returns:
        LinkResult: The outcome of linking the copy that remains.

    Raises:
        LocalMdError: When there are not two copies, or the other cannot be removed.
    """
    old = paths.legacy_local_md_path(co.vault, co.slug)
    item = entry(co, paths.LOCAL_MD_FILE)
    if not (old.is_file() and item.target.is_file()):
        msg = f"there are not two vault copies of {paths.LOCAL_MD_FILE} to choose between"
        raise LocalMdError(msg)
    try:
        if keep == "old":
            old.replace(item.target)
        else:
            old.unlink()
    except OSError as err:
        msg = f"could not keep the {keep} copy of {paths.LOCAL_MD_FILE}: {err}"
        raise LocalMdError(msg) from err
    return _link_contained(co, item, tracked=tracked)


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


def modified(path: Path) -> str | None:
    """Return a file's or folder's modification time as ISO-8601 UTC, for a person choosing.

    Args:
        path (Path): The file or folder.

    Returns:
        str | None: The time, or None when nothing is there.
    """
    if not path.exists():
        return None
    stamp = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.UTC)
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def _mtime(path: Path) -> str | None:
    """Return a file's modification time as ISO-8601 UTC, or None when it is absent."""
    return modified(path) if path.is_file() else None


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


@dataclass(frozen=True)
class KeptSettings:
    """Where the kept settings copy now lives, and whether keeping it wrote anything."""

    path: Path
    written: bool


def keep_settings(
    co: Checkout, side: Literal["checkout", "vault"], *, tracked: bool
) -> KeptSettings:
    """Write the chosen settings copy over the other one, as the person decided.

    Args:
        co (Checkout): The checkout whose settings to sync.
        side (Literal["checkout", "vault"]): The copy to keep.
        tracked (bool): Whether git tracks the settings path.

    Returns:
        KeptSettings: The path that now holds the kept copy's content, and False for
            `written` when the two copies already matched.

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
        return KeptSettings(path=dest, written=False)
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
    return KeptSettings(path=dest, written=True)
