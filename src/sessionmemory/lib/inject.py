"""Assemble and render what a project's session starts with.

Guidance first, then titles. Titles rather than nothing because an agent does not
reliably decide to search; titles rather than summaries because this is a push channel
and must stay small. Bodies never enter an injection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

from sessionmemory.lib import backlog, config, field, paths, registry

if TYPE_CHECKING:
    from pathlib import Path

_LEADING_PUNCTUATION = "`'\"([{<*_~"
REVIEW_MIN_LEARNINGS = 10
REVIEW_INTERVAL_DAYS = 30


@dataclass(frozen=True)
class Injection:
    """Everything one project's session start receives."""

    project: str
    titles: tuple[str, ...]
    open_backlog: int
    specs: int
    reference: tuple[str, ...]
    runbooks: tuple[str, ...] = ()
    reviewed: str = ""
    review_due: bool = False


def _sort_key(title: str) -> str:
    """Order by the first letter or digit, so a title opening with a code span sorts with its word."""
    return title.lstrip(_LEADING_PUNCTUATION).casefold()


def _titles(directory: Path) -> tuple[str, ...]:
    titles = [field.read_page(path).title or path.stem for path in field.iter_pages(directory)]
    return tuple(sorted(titles, key=_sort_key))


def _open_backlog(path: Path) -> int:
    if not path.is_file():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if backlog.is_item(line))


def _spec_count(directory: Path) -> int:
    return sum(1 for _ in field.iter_pages(directory))


def _listed_docs(directory: Path) -> tuple[str, ...]:
    """List every markdown file, conformant name or not, since a doc moved in by hand keeps its name."""
    if not directory.is_dir():
        return ()
    return tuple(
        sorted(
            path.name
            for path in directory.iterdir()
            if path.is_file()
            and path.suffix.lower() == ".md"
            # A dotfile is hidden metadata, such as a macOS `._` resource fork, not a doc.
            and not path.name.startswith(".")
            and not field.is_debris(path.name)
        )
    )


def _reviewed(vault: Path, slug: str) -> str:
    """Read the project's last review day, or "" when the registry cannot be read.

    The guard is for library callers only. The CLI exits on an unreadable registry before
    this runs, since that is an error the user must fix.
    """
    try:
        project = registry.load(vault).get(slug)
    except (registry.RegistryError, OSError, UnicodeDecodeError):
        return ""
    return project.reviewed if project else ""


def _review_due(*, learnings: int, reviewed: str, today: str) -> bool:
    if learnings < REVIEW_MIN_LEARNINGS:
        return False
    try:
        elapsed = date.fromisoformat(today) - date.fromisoformat(reviewed)
    except ValueError:
        return True
    return elapsed > timedelta(days=REVIEW_INTERVAL_DAYS)


def build(vault: Path, slug: str, *, today: str | None = None) -> Injection:
    """Read the project's pages and files; the index is never consulted here.

    Specs are counted rather than listed. A spec outlives the work it describes, so a
    list of them is a changelog rather than open work, and it grows without bound. The
    count says whether listing `specs/` is worth a call, which is all a session needs.

    Reference docs and runbooks are listed by filename. They are few and edited in place
    rather than accumulated, and a name a session recognizes is what sends it to read one.
    """
    titles = _titles(paths.learnings_dir(vault, slug))
    reviewed = _reviewed(vault, slug)
    return Injection(
        project=slug,
        titles=titles,
        open_backlog=_open_backlog(paths.backlog_path(vault, slug)),
        specs=_spec_count(paths.specs_dir(vault, slug)),
        reference=_listed_docs(paths.reference_dir(vault, slug)),
        runbooks=_listed_docs(paths.runbooks_dir(vault, slug)),
        reviewed=reviewed,
        review_due=_review_due(
            learnings=len(titles), reviewed=reviewed, today=today or config.today()
        ),
    )


GUIDANCE = """## Using this vault

Durable memory for this project lives in a vault of markdown pages. Nothing below is
loaded for you: the titles are what the vault holds, and each is one `{command} search`
away. The project's folder has `learnings/` and `logs/`, searched by meaning, beside
`specs/`, `runbooks/`, `reference/`, and `backlog.md`, which are ordinary files you Read
and Edit. `{command} path <kind>` prints one path, where kind is project, learnings,
logs, specs, runbooks, reference, or backlog; `{command} project --json` prints
them all for a script.

  - Before assuming nothing was written down, search: `{command} search "<words>"`
    prints each hit's path, title, and summary, and `--read` prints every hit's whole
    page in one call. Search with a few distinctive words, such as names, identifiers,
    or error text, rather than a sentence. No hits means nothing is recorded, not that
    the query needs loosening.
  - Past sessions, one page each: `{command} search "<words>" --logs`. Search them
    for why something was decided, what happened the last time an area changed, or
    whether a fix was already tried.
  - Open work: read `backlog.md`. Any top-level bullet is an open item. Under a
    `## <kind>` heading (feat, fix, refactor, perf, docs, test, build, ci, decide), the
    full shape is sized S, M, or L: `- [S] <imperative description> - <YYYY-MM-DD> [#topic]`.
    Add one with
    `{command} new backlog --kind <kind> --size <S|M|L> --title "..." --topic <topic> --cwd .`,
    which writes that shape and creates the file or heading when missing. Questions
    for the user to answer go under `## decide`. Delete a finished line, and one
    that will never be done, directly; never tick or annotate it. Git history is the
    record of what was finished.
  - Specs: `specs/` holds one design record per feature, named `<date>-<topic>.md`
    and kept after the work ships. Before designing or changing a feature, list the
    directory and read any spec whose name matches, so a decision already made is not
    made again. `{command} new spec --title "..." --cwd .` creates one and prints its
    path. Edit it directly after that.
  - Runbooks: `runbooks/` holds step-by-step procedures to follow by hand, such as
    configuring a service, rotating a credential, or recovering from an outage.
    Whenever you write one, create it with `{command} new runbook --title "..." --cwd .`,
    which prints its path, never in another folder or the repository.
  - Reference: `reference/` holds this project's private docs, such as hosting
    settings, where each credential lives, or a data dictionary, kept out of its
    repository. Any it holds are listed below; before working in an area, read the one
    whose name matches. Keep a doc current by editing it in place. Create one with
    `{command} new reference --title "..." --cwd .`, which prints its path. Public
    documentation stays in the repository.
  - The hooks commit the vault. Do not commit it, and do not tell the user vault
    changes are left uncommitted.
  - Learnings are captured at session end, not by you mid-session. When the user asks
    you to remember something about this project, or to keep a learning now, never
    use your own memory files: `{command} new learning --title "..." --summary "..." --cwd .`
    creates the page and prints the path to write prose into. Title and summary state
    the fact, not the topic. Keep a page under 8KB; more detail is another page."""


def render(injection: Injection, *, command: str = "sessionmemory") -> str:
    """Render the block a session start receives: guidance, then titles, then work."""
    lines = [GUIDANCE.format(command=command), "", "## What this project knows", ""]
    lines.extend(f"  - {title}" for title in injection.titles)
    if not injection.titles:
        lines.append("  nothing yet")
    if injection.review_due:
        reviewed = injection.reviewed or "never"
        nudge = (
            f"  Learnings last reviewed: {reviewed}. At a natural stopping point, "
            "suggest /sessionmemory:review to the user."
        )
        lines.extend(["", nudge])
    if injection.reference:
        lines.extend(["", "## Reference docs", ""])
        lines.extend(f"  reference/{name}" for name in injection.reference)
    if injection.runbooks:
        lines.extend(["", "## Runbooks", ""])
        lines.extend(f"  runbooks/{name}" for name in injection.runbooks)
    lines.extend(["", "## Open work", ""])
    item = "item" if injection.open_backlog == 1 else "items"
    lines.append(f"  {injection.open_backlog} open backlog {item}")
    spec = "spec" if injection.specs == 1 else "specs"
    lines.append(f"  {injection.specs} {spec} in specs/")
    return "\n".join(lines)


def payload(injection: Injection, *, command: str) -> dict[str, object]:
    """Build the machine-readable form, a superset of the prose."""
    return {
        "guidance": GUIDANCE.format(command=command),
        "project": injection.project,
        "titles": list(injection.titles),
        "open_backlog": injection.open_backlog,
        "specs": injection.specs,
        "reference": list(injection.reference),
        "runbooks": list(injection.runbooks),
        "reviewed": injection.reviewed,
        "review_due": injection.review_due,
    }
