"""Replace a file's contents in one step, or not at all.

Every file this CLI owns is the source of truth for something that cannot be rebuilt: a
note is knowledge no index holds a copy of, and the registry is the only record of which
slug a project's existing notes were filed under. A write interrupted by a crash, a full
disk, or a signal must therefore leave the previous contents in place rather than a
truncated file, so contents are written to a temporary file beside the target and renamed
over it.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

DEFAULT_UMASK = 0o022


def write_text(path: Path, text: str) -> None:
    """Write `text` to `path`, replacing it in a single rename.

    Args:
        path (Path): The destination file. Its parent directory is created if needed.
        text (str): The complete contents to write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    handle, name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(text)
        # mkstemp creates the file mode 0600; match the umask default so a CLI-written
        # file is indistinguishable from one made by hand, since Path.replace preserves
        # the source file's mode across the rename.
        umask = os.umask(DEFAULT_UMASK)
        os.umask(umask)
        temporary.chmod(0o666 & ~umask)
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def claim(path: Path) -> bool:
    """Create `path` as an empty file, or report that someone else already has it.

    Exclusive creation is the only way to take a filename that cannot lose a race. A
    check followed by a write leaves a window in which another process takes the same
    name, and `write_text` renames over whatever is there, so the loser's note is
    destroyed with nothing reported. `Path.touch(exist_ok=False)` is `O_CREAT |
    O_EXCL | O_WRONLY` under the hood, so this needs no lock file to clean up after a
    crash.

    On a case-insensitive filesystem, which is the default on macOS, this also refuses a
    name differing only in case from an existing file. That is the wanted behavior:
    those two names cannot coexist there.

    Args:
        path (Path): The file to claim. Its parent directory is created if needed.

    Returns:
        bool: True when this call created the file, False when it already existed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.touch(exist_ok=False)
    except FileExistsError:
        return False
    return True
