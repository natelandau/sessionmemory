"""Copy, compare, and link files and folders for the `local` entries.

These are the filesystem steps adopt and link are built from, with no knowledge of an
entry's state: every decision about whether a step is safe stays in `localmd`. Each step
works through a hidden sibling name, so a crash leaves either the old content or the new
and never neither, and discovery skips the leftover as hidden.
"""

from __future__ import annotations

import shutil
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def _hidden_sibling(path: Path, suffix: str) -> Path:
    return path.with_name(f".{path.name}.{uuid.uuid4().hex}.{suffix}")


def place_link(link: Path, target: Path) -> None:
    """Point `link` at `target`, replacing a symlink or file already there.

    The symlink is built at a temporary name and renamed over `link`, so a crash leaves
    either the old entry or the new link and never neither. Use `create_link` when
    nothing is at `link`, since a crash here can leave the temporary name behind.

    Args:
        link (Path): Where the symlink goes.
        target (Path): What it points at.
    """
    temp = _hidden_sibling(link, "tmp")
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


def _walk(root: Path) -> Iterator[Path]:
    """Yield every path under `root` without following a symlinked folder."""
    for parent, dirs, files in root.walk():
        for name in (*dirs, *files):
            yield parent / name


def first_symlink(root: Path) -> Path | None:
    """Find a symlink anywhere under `root`, which a folder copy cannot carry faithfully.

    Args:
        root (Path): The folder to search.

    Returns:
        Path | None: The first symlink found, or None when the folder holds none.
    """
    return next((path for path in _walk(root) if path.is_symlink()), None)


def first_folder_named(root: Path, names: frozenset[str]) -> Path | None:
    """Find a folder anywhere under `root` whose name is one of `names`.

    Args:
        root (Path): The folder to search, without following a symlinked folder.
        names (frozenset[str]): The folder names to look for.

    Returns:
        Path | None: The first such folder found, or None when there is none.
    """
    return next(
        (
            path
            for path in _walk(root)
            if path.name in names and path.is_dir() and not path.is_symlink()
        ),
        None,
    )


def _tree(root: Path) -> dict[str, tuple[bool, bytes]]:
    """Map each file and symlink under `root` to whether it is a link and its content."""
    found: dict[str, tuple[bool, bytes]] = {}
    for path in _walk(root):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            found[rel] = (True, str(path.readlink()).encode())
        elif path.is_file():
            found[rel] = (False, path.read_bytes())
    return found


def same_tree(left: Path, right: Path) -> bool:
    """Report whether two folders hold the same relative files with the same bytes.

    Args:
        left (Path): One folder.
        right (Path): The other folder.

    Returns:
        bool: True when both hold the same file paths and each file's bytes match.
    """
    return _tree(left) == _tree(right)


def copy_tree(source: Path, dest: Path) -> bool:
    """Copy a folder to `dest`, which must not exist, and verify it before placing it.

    The copy is built at a hidden sibling and renamed into place only once it matches,
    so `dest` never holds a partial copy. Executable bits are kept.

    Args:
        source (Path): The folder to copy.
        dest (Path): Where the copy goes.

    Returns:
        bool: True when the copy is in place, False when it did not match and was removed.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = _hidden_sibling(dest, "tmp")
    try:
        shutil.copytree(source, temp, symlinks=True)
        if not same_tree(source, temp):
            return False
        temp.rename(dest)
    finally:
        if temp.exists():
            shutil.rmtree(temp)
    return True


def copy_file(source: Path, dest: Path) -> bool:
    """Copy a file's bytes and mode to `dest` in one step, refusing to replace a file there.

    The copy is built at a hidden sibling and hard-linked into place, which fails rather
    than replacing a file that appeared at `dest`, as a rename would. Where the filesystem
    has no hard links, the bytes are written to `dest` opened for exclusive creation.

    Args:
        source (Path): The file to copy.
        dest (Path): Where the copy goes; its parent is created if needed.

    Returns:
        bool: True when the copy is in place, False when something already sat at `dest`.

    Raises:
        OSError: When the copy cannot be written.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = _hidden_sibling(dest, "tmp")
    try:
        shutil.copy2(source, temp)
        try:
            dest.hardlink_to(temp)
        except FileExistsError:
            raise
        except OSError:
            with dest.open("xb") as handle:
                try:
                    handle.write(temp.read_bytes())
                    shutil.copymode(temp, dest)
                except BaseException:
                    # The file is ours from the exclusive open, so a partial copy is removed.
                    dest.unlink(missing_ok=True)
                    raise
    except FileExistsError:
        return False
    finally:
        temp.unlink(missing_ok=True)
    return True


def set_aside(path: Path) -> Path:
    """Rename `path` to a hidden sibling, since a link cannot be renamed over a folder.

    Args:
        path (Path): The folder to move out of the way.

    Returns:
        Path: Where it now sits; rename it back to restore it.
    """
    aside = _hidden_sibling(path, "adopting")
    path.rename(aside)
    return aside
