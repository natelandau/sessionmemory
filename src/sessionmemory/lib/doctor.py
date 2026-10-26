"""The things that can be wrong with a vault, stated without severity.

Every check is a suggestion. The article's argument holds: an irrelevant or imperfect
page is never surfaced by semantic search, so nothing here fails a build. There is no
repair, because repair is what grew to three hundred lines last time.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sessionmemory.lib import field, fieldindex, localmd, localtree, paths, registry, specs
from sessionmemory.lib.frontmatter import (
    FrontmatterError,
    MissingFrontmatterError,
    parse,
    unquoted_datetime_keys,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from sessionmemory.lib.embed import Embedder


@dataclass(frozen=True)
class Finding:
    """One thing a check found."""

    check: str
    path: str
    message: str


def nonconformant_names(vault: Path, _embedder: Embedder) -> list[Finding]:
    """Report a markdown file in a field or `specs/` whose name breaks the filename rule.

    Such a file is never indexed, so it is never searched, and a spec is never counted.
    """
    return [
        Finding("filename", str(path), "not lowercase ascii letters, digits, and hyphens")
        for directory in paths.iter_index_dirs(vault)
        for path in sorted(directory.glob("*.md"))
        if path.is_file() and not field.is_debris(path.name) and not field.is_page_name(path.name)
    ]


def oversized_pages(vault: Path, _embedder: Embedder) -> list[Finding]:
    """Report a page over the 8KB limit; only its first 8KB is embedded."""
    return [
        Finding(
            "size",
            str(path),
            f"{path.stat().st_size} bytes; split it, the limit is {field.PAGE_LIMIT}",
        )
        for directory in paths.iter_field_dirs(vault)
        for path in field.iter_pages(directory)
        if path.stat().st_size > field.PAGE_LIMIT
    ]


def _blank(value: object) -> bool:
    """Report whether a frontmatter value is missing or an empty/whitespace string."""
    return not (isinstance(value, str) and value.strip())


def malformed_frontmatter(vault: Path, _embedder: Embedder) -> list[Finding]:
    """Report a page whose frontmatter cannot be parsed, or a learning missing title or summary.

    A logs page with no frontmatter block is fine; a learnings page needs a title and a
    summary to be worth anything a search result shows, so it has nothing to be missing
    when it has no block at all.
    """
    findings = []
    for directory in paths.iter_field_dirs(vault):
        for path in field.iter_pages(directory):
            raw = path.read_bytes()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                findings.append(Finding("frontmatter", str(path), "not valid UTF-8"))
                continue
            try:
                meta, _body = parse(text)
            except MissingFrontmatterError:
                if path.parent.name == paths.LEARNINGS_DIR:
                    findings.append(Finding("frontmatter", str(path), "missing title or summary"))
                continue
            except FrontmatterError as error:
                findings.append(Finding("frontmatter", str(path), str(error)))
                continue
            if path.parent.name == paths.LEARNINGS_DIR and (
                _blank(meta.get("title")) or _blank(meta.get("summary"))
            ):
                findings.append(Finding("frontmatter", str(path), "missing title or summary"))
    return findings


def unquoted_datetimes(vault: Path, _embedder: Embedder) -> list[Finding]:
    """Report a page whose frontmatter carries a bare date or datetime.

    The spec requires quoting them: a YAML 1.1 parser types a bare value and a YAML 1.2
    parser leaves it a string, so what the page says would depend on which reads it. A
    block that cannot be parsed at all is left to the frontmatter check.
    """
    findings = []
    for directory in paths.iter_field_dirs(vault):
        for path in field.iter_pages(directory):
            try:
                keys = unquoted_datetime_keys(path.read_text(encoding="utf-8"))
            except (UnicodeDecodeError, FrontmatterError):
                continue
            if keys:
                message = f"{', '.join(keys)}: unquoted; quote the value"
                findings.append(Finding("datetime", str(path), message))
    return findings


def dead_projects(vault: Path, _embedder: Embedder) -> list[Finding]:
    """Report a registered project whose repository root no longer exists."""
    try:
        projects = registry.load(vault)
    except registry.RegistryError as error:
        return [Finding("registry", "", str(error))]
    return [
        Finding("project", slug, f"root {project.root} does not exist")
        for slug, project in sorted(projects.items())
        if project.root and not Path(project.root).is_dir()
    ]


_TICKED = ("- [x]", "- [X]")


def ticked_backlog_lines(vault: Path, _embedder: Embedder) -> list[Finding]:
    """Report a ticked top-level bullet in a backlog.

    A ticked line is finished work that should have been deleted. Every other top-level
    bullet counts as open, and an indented bullet is a note under an item.
    """
    findings = []
    for slug in paths.iter_project_slugs(vault):
        path = paths.backlog_path(vault, slug)
        if not path.is_file():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if line.startswith(_TICKED):
                message = f"line {number}: ticked; finished work is deleted, not ticked"
                findings.append(Finding("backlog", str(path), message))
    return findings


_REFERENCE_SOURCES = (
    paths.SPECS_DIR,
    paths.RUNBOOKS_DIR,
    paths.REFERENCE_DIR,
    paths.LEARNINGS_DIR,
)
_REFERENCE_TARGETS = (*_REFERENCE_SOURCES, paths.LOGS_DIR)


def _reference_pattern(vault: Path) -> re.Pattern[str]:
    """Build the pattern for a mention of a vault page, anchored to a word boundary.

    The lookbehind admits a mention only at a line start or after whitespace or an opening
    quote or bracket, so `docs/specs/x.md` and a placeholder like `specs/<slug>.md` are
    not mentions. `plans/` is not a target: a plan is scratch and may be gone by design.
    """
    folders = "|".join(_REFERENCE_TARGETS)
    return re.compile(
        rf"(?<![^\s`'\"(\[<])(?:{re.escape(str(vault))}/)?"
        rf"(?:projects/(?P<slug>[A-Za-z0-9._-]+)/)?"
        rf"(?P<folder>{folders})/(?P<name>[A-Za-z0-9._-]+\.md)(?![A-Za-z0-9_])"
    )


def dangling_references(vault: Path, _embedder: Embedder) -> list[Finding]:
    """Report a mention of a vault page that does not exist.

    Specs, runbooks, reference docs, and learnings are scanned line by line, fenced code
    included. A mention naming a project resolves there; any other resolves in the
    project that holds the mentioning file.
    """
    pattern = _reference_pattern(vault)
    findings = []
    for slug in paths.iter_project_slugs(vault):
        for folder in _REFERENCE_SOURCES:
            directory = paths.project_dir(vault, slug) / folder
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.md")):
                if not path.is_file() or field.is_debris(path.name):
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                for number, line in enumerate(text.splitlines(), start=1):
                    for match in pattern.finditer(line):
                        owner = match["slug"] or slug
                        target = paths.project_dir(vault, owner) / match["folder"] / match["name"]
                        if not target.is_file():
                            mention = match[0].removeprefix(f"{vault}/")
                            message = f"line {number}: names {mention}, which does not exist"
                            findings.append(Finding("reference", str(path), message))
    return findings


def spec_statuses(vault: Path, _embedder: Embedder) -> list[Finding]:
    """Report a spec whose status is unknown or unreadable, or superseded without a real successor.

    A spec with no status, or with no frontmatter block at all, is current and reports
    nothing. A block that cannot be parsed is reported here, since the frontmatter check
    reads only fields and a broken block silently makes the spec current.
    """
    findings = []
    for slug in paths.iter_project_slugs(vault):
        directory = paths.specs_dir(vault, slug)
        pages = field.iter_pages(directory) if directory.is_dir() else []
        siblings = {path.name for path in pages}
        for path in pages:
            try:
                meta, _body = parse(path.read_text(encoding="utf-8", errors="replace"))
            except MissingFrontmatterError:
                continue
            except FrontmatterError:
                message = "frontmatter cannot be parsed; status ignored"
                findings.append(Finding("spec", str(path), message))
                continue
            findings.extend(
                Finding("spec", str(path), problem)
                for problem in specs.status_problems(meta, filename=path.name, siblings=siblings)
            )
    return findings


def _diverged_message(item: localmd.Entry, *, ignored: bool) -> str | None:
    """Name what is wrong with a real file or folder where the link belongs."""
    try:
        match = localmd.contents_match(item)
    except OSError:
        return None
    if match is False:
        return f"{item.name} differs from the vault copy; {localmd.REPLACE_VAULT_COPY}"
    if match is None or ignored:
        return None
    return (
        f"{item.name} is not ignored by git; run local adopt {item.name} "
        "to replace it with an ignored link"
    )


def _state_message(item: localmd.Entry, *, ignored: bool) -> str | None:
    """Name what is wrong with an untracked entry at the link path, given its state."""
    name = item.name
    current = localmd.state(item)
    if current is localmd.State.DANGLING:
        return f"{name} is a dangling symlink; run local link to replace it"
    if current is localmd.State.DIVERGED:
        return _diverged_message(item, ignored=ignored)
    if ignored:
        return None
    if current is localmd.State.FOREIGN_LINK:
        return (
            f"{name} is not ignored by git, and a symlink to {item.link.resolve()} rather "
            "than the vault copy; remove it, then run local link"
        )
    return f"{name} is not ignored by git; run local link to add it to info/exclude"


def _checkout_message(
    co: localmd.Checkout, item: localmd.Entry, *, tracked: bool | None
) -> str | None:
    """Name what is wrong at one entry's link path, with advice that fits its state.

    A tracked entry and a diverged one each get one finding and no ignore advice, since
    `local link` refuses both and the first remedy is a different command. An entry
    behind a symlinked or non-folder `.claude` path is reported once, since git cannot
    be asked about it. An unreadable entry is not judged, and `tracked` is None when git
    could not be asked, which leaves only the checks that need no git.
    """
    if localmd.blocked(co, item):
        folder = item.name.partition("/")[0]
        return (
            f"{item.name} cannot be linked; .claude or .claude/{folder} is a symlink "
            "or not a folder"
        )
    if tracked is None or not (item.link.is_symlink() or item.link.exists()):
        return None
    if tracked:
        return (
            f"{item.name} is tracked by git; run git rm --cached {item.relpath} before adopting it"
        )
    try:
        return _state_message(item, ignored=localmd.ignored(co, item))
    except (localmd.LocalMdError, OSError):
        return None


def _repository_findings(item: localmd.Entry) -> list[Finding]:
    """Report a `.git` folder inside a vault skill or rule, which git commits as a gitlink."""
    if not item.target.is_dir() or item.target.is_symlink():
        return []
    try:
        inner = localtree.first_folder_named(item.target, frozenset({".git"}))
    except OSError:
        return []
    if inner is None:
        return []
    return [
        Finding(
            "local",
            str(inner),
            f"{item.name} holds a git repository, which the vault commits as an embedded "
            "repository rather than its files; move it out of the vault",
        )
    ]


def _import_findings(item: localmd.Entry) -> list[Finding]:
    """Report each relative `@` import in an entry that cannot resolve from the vault."""
    findings = []
    for source in localmd.import_sources(item, item.target):
        try:
            text = source.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        findings.extend(
            Finding(
                "local", str(source), f"@{target} resolves from the vault folder and will not load"
            )
            for target in localmd.relative_imports(text)
        )
    return findings


def _legacy_finding(vault: Path, slug: str) -> Finding | None:
    """Report a CLAUDE.local.md left at the project folder's top, with the remedy that fits."""
    legacy = paths.legacy_local_md_path(vault, slug)
    if not legacy.is_file():
        return None
    new = paths.local_dir(vault, slug) / paths.LOCAL_MD_FILE
    try:
        differs = new.exists() and new.read_bytes() != legacy.read_bytes()
    except OSError:
        differs = False
    if differs:
        message = (
            f"CLAUDE.local.md is at its old path and differs from {new}; keep one of them, "
            "delete the other, then run local link"
        )
    else:
        message = (
            f"CLAUDE.local.md is at its old path; run local link to move it into {paths.LOCAL_DIR}/"
        )
    return Finding("local", str(legacy), message)


def local_instructions(vault: Path, _embedder: Embedder) -> list[Finding]:
    """Report problems with a project's local entries kept in the vault.

    Covers a vault copy of CLAUDE.local.md left at its old path, a relative `@import`
    that cannot resolve from the vault folder, and at the registered root, per entry, a
    tracked path, a dangling link, a copy that has drifted from the vault, and a link or
    copy that git does not ignore.
    """
    try:
        projects = registry.load(vault)
    except registry.RegistryError:
        # dead_projects reports an unreadable registry.
        projects = {}
    findings = []
    for slug in paths.iter_project_slugs(vault):
        legacy = _legacy_finding(vault, slug)
        if legacy is not None:
            findings.append(legacy)
        project = projects.get(slug)
        root = Path(project.root) if project and project.root else None
        # With no registered root nothing below reaches git, so the vault is a harmless stand-in.
        co = localmd.Checkout(
            worktree=root or vault, vault=vault, slug=slug, local=paths.local_dir(vault, slug)
        )
        try:
            items = localmd.vault_entries(co)
            odd = localmd.misshapen(co)
        except OSError as err:
            findings.append(Finding("local", str(co.local), f"cannot be read: {err}"))
            continue
        findings.extend(
            Finding(
                "local",
                str(item.target),
                f"{item.name} in the vault is not a {localmd.SHAPES[item.kind]}; "
                "fix or remove it in the vault",
            )
            for item in odd
        )
        for item in items:
            findings.extend(_import_findings(item))
            findings.extend(_repository_findings(item))
        if root is None or not root.is_dir():
            continue
        try:
            tracked: set[str] | None = localmd.tracked_names(co, items)
        except localmd.LocalMdError:
            tracked = None
        for item in items:
            message = _checkout_message(
                co, item, tracked=None if tracked is None else item.name in tracked
            )
            if message is not None:
                findings.append(Finding("local", str(item.link), message))
    return findings


def _is_stale(directory: Path, embedder: Embedder) -> str | None:
    index = fieldindex.index_path(directory, embedder)
    if not index.is_file():
        return None
    try:
        conn = fieldindex.connect(index)
    except sqlite3.DatabaseError:
        return "index is not a readable database; run: sessionmemory reindex"
    try:
        stored = {
            row["filename"]: row["sha256_hash"]
            for row in conn.execute("SELECT filename, sha256_hash FROM pages")
        }
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    if directory.name == paths.SPECS_DIR and version != specs.INPUT_VERSION:
        return "index is behind its pages; run: sessionmemory reindex"
    current = {
        path.name: hashlib.sha256(path.read_bytes()).digest()
        for path in field.iter_pages(directory)
    }
    return None if stored == current else "index is behind its pages; run: sessionmemory reindex"


def stale_indexes(vault: Path, embedder: Embedder) -> list[Finding]:
    """Report a field or specs directory whose index is unreadable or behind its pages."""
    findings = []
    for directory in paths.iter_index_dirs(vault):
        message = _is_stale(directory, embedder)
        if message:
            findings.append(Finding("index", str(directory), message))
    return findings


CHECKS: tuple[Callable[[Path, Embedder], list[Finding]], ...] = (
    nonconformant_names,
    oversized_pages,
    malformed_frontmatter,
    unquoted_datetimes,
    dead_projects,
    ticked_backlog_lines,
    dangling_references,
    spec_statuses,
    local_instructions,
    stale_indexes,
)


def run(vault: Path, embedder: Embedder) -> list[Finding]:
    """Run every check and return what they found, one failing check never stopping the rest."""
    findings: list[Finding] = []
    for check in CHECKS:
        try:
            findings.extend(check(vault, embedder))
        # Doctor always exits 0, so a check that fails in any way is reported as a finding.
        except Exception as err:  # noqa: BLE001
            findings.append(
                Finding("doctor", str(vault), f"{getattr(check, '__name__', check)} failed: {err}")
            )
    return findings
