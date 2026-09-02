"""Assemble and render what a project's session starts with.

Guidance first, then titles. Titles rather than nothing because an agent does not
reliably decide to search; titles rather than summaries because this is a push channel
and must stay small. Bodies never enter an injection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sessionmemory.lib import field, paths

if TYPE_CHECKING:
    from pathlib import Path

OPEN_ITEM = "- [ ]"


@dataclass(frozen=True)
class Injection:
    """Everything one project's session start receives."""

    project: str
    titles: tuple[str, ...]
    open_backlog: int
    specs: tuple[str, ...]
    plans: tuple[str, ...]


def _titles(directory: Path) -> tuple[str, ...]:
    titles = [field.read_page(path).title or path.stem for path in field.iter_pages(directory)]
    return tuple(sorted(titles, key=str.casefold))


def _open_backlog(path: Path) -> int:
    if not path.is_file():
        return 0
    return sum(
        1
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.lstrip().startswith(OPEN_ITEM)
    )


def build(vault: Path, slug: str) -> Injection:
    """Read the project's pages and files; the index is never consulted here."""
    return Injection(
        project=slug,
        titles=_titles(paths.learnings_dir(vault, slug)),
        open_backlog=_open_backlog(paths.backlog_path(vault, slug)),
        specs=_titles(paths.specs_dir(vault, slug)),
        plans=_titles(paths.plans_dir(vault, slug)),
    )


GUIDANCE = """## Using this vault

Durable memory for this project lives in a vault of markdown pages. Below is the
list of what it already knows; each title is one `{command} search` away.

  - A title below matches what you are doing: `{command} search "<words>"` returns
    the page's path, and you Read it. Search before assuming nothing was written down.
  - Past sessions: `{command} search "<words>" --logs`. Open work: read `backlog.md`
    in the project's vault folder (`{command} project --json` prints every path).
  - Something worth keeping past this session: `{command} new learning --title "..."
    --summary "..." --cwd .` creates the page and prints the path to write prose into.
    Keep a page under 8KB; more detail is another page.
  - Specs and plans: `{command} new spec|plan --title "..." --cwd .` creates the file.
    Edit `backlog.md`, specs, and plans directly; the CLI only creates pages."""


def render(injection: Injection, *, command: str = "sessionmemory") -> str:
    """Render the block a session start receives: guidance, then titles, then work."""
    lines = [GUIDANCE.format(command=command), "", "## What this project knows", ""]
    lines.extend(f"  - {title}" for title in injection.titles)
    if not injection.titles:
        lines.append("  nothing yet")
    lines.extend(["", "## Open work", ""])
    item = "item" if injection.open_backlog == 1 else "items"
    lines.append(f"  {injection.open_backlog} open backlog {item}")
    lines.extend(f"  spec: {title}" for title in injection.specs)
    lines.extend(f"  plan: {title}" for title in injection.plans)
    return "\n".join(lines)


def payload(injection: Injection, *, command: str) -> dict[str, object]:
    """Build the machine-readable form, a superset of the prose."""
    return {
        "guidance": GUIDANCE.format(command=command),
        "project": injection.project,
        "titles": list(injection.titles),
        "open_backlog": injection.open_backlog,
        "specs": list(injection.specs),
        "plans": list(injection.plans),
    }
