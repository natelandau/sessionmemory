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

from sessionmemory.lib import (
    changes,
    commits,
    field,
    fieldindex,
    localmanifest,
    localmd,
    localtree,
    paths,
    registry,
    specs,
)
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
        f"{item.name} is not ignored by git; run local sync --adopt {item.name} "
        "to replace it with an ignored link"
    )


def _state_message(item: localmd.Entry, *, ignored: bool) -> str | None:
    """Name what is wrong with an untracked entry at the link path, given its state."""
    name = item.name
    current = localmd.state(item)
    if current is localmd.State.DANGLING:
        return f"{name} is a dangling symlink; run local sync to replace it"
    if current is localmd.State.DIVERGED:
        return _diverged_message(item, ignored=ignored)
    if ignored:
        return None
    if current is localmd.State.FOREIGN_LINK:
        return (
            f"{name} is not ignored by git, and a symlink to {item.link.resolve()} rather "
            "than the vault copy; remove it, then run local sync"
        )
    return f"{name} is not ignored by git; run local sync to add it to info/exclude"


def _checkout_message(
    co: localmd.Checkout, item: localmd.Entry, *, tracked: bool | None
) -> str | None:
    """Name what is wrong at one entry's link path, with advice that fits its state.

    A tracked entry and a diverged one each get one finding and no ignore advice, since
    `local sync` refuses to link both and the first remedy is a different command. An entry
    behind a symlinked or non-folder `.claude` path is reported once, since git cannot
    be asked about it. An unreadable entry is not judged, and `tracked` is None when git
    could not be asked, which leaves only the checks that need no git.
    """
    if localmd.blocked(co, item):
        if item.kind == "path":
            folder = localmd.blocking_folder(co, item)
            return f"{item.name} cannot be linked; {folder} is a symlink or not a folder"
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
            "delete the other, then run local sync"
        )
    else:
        message = (
            f"CLAUDE.local.md is at its old path; run local sync to move it into {paths.LOCAL_DIR}/"
        )
    return Finding("local", str(legacy), message)


_SETTINGS_MESSAGES: dict[localmd.CopyState, str] = {
    localmd.CopyState.DIFFERS: (
        "differs from the vault copy; run local sync to choose which copy to keep"
    ),
    localmd.CopyState.CHECKOUT_ONLY: "is only in the checkout; run local sync to keep a copy",
    localmd.CopyState.VAULT_ONLY: "is only in the vault; run local sync to keep a copy",
    localmd.CopyState.LINKED: (
        "is a symlink, which Claude Code cannot write through; replace it with a regular file"
    ),
}


def _settings_finding(co: localmd.Checkout, *, tracked: set[str] | None) -> Finding | None:
    """Report the settings copy when it waits on the person or cannot be copied.

    Nothing is reported when git could not say whether it tracks the file, since a
    tracked file would otherwise read as a difference.
    """
    if tracked is None:
        return None
    item = localmd.settings_entry(co)
    try:
        current = localmd.copy_state(co, item, tracked=item.name in tracked)
    except OSError:
        return None
    if current is localmd.CopyState.BLOCKED:
        message = (
            "cannot be synced; .claude is a symlink or not a folder"
            if localmd.blocked(co, item)
            else "cannot be synced; a copy is a folder or a symlink, not a regular file"
        )
    elif current is localmd.CopyState.TRACKED:
        message = f"is tracked by git; run git rm --cached {item.relpath} to keep it private"
    elif current is None or current not in _SETTINGS_MESSAGES:
        return None
    else:
        message = _SETTINGS_MESSAGES[current]
    return Finding("local", str(item.link), f"{item.name} {message}")


# Names under `local/` that entries found by location own, so they never need a manifest line.
_LOCATED = frozenset(
    {
        paths.LOCAL_MD_FILE,
        localmanifest.MANIFEST_NAME,
        "claude/skills",
        "claude/rules",
        f"claude/{localmd.SETTINGS_NAME}",
    }
)


def _unclaimed(local: Path, listed: set[str]) -> list[Path]:
    """List the topmost vault copies under `local/` that no entry names.

    A folder above a listed path is walked into rather than reported, since only part of
    it is claimed.
    """
    found: list[Path] = []

    def walk(folder: Path, prefix: str) -> None:
        for child in sorted(folder.iterdir()):
            rel = f"{prefix}{child.name}"
            if child.name.startswith(".") or rel in _LOCATED or rel in listed:
                continue
            above = rel == "claude" or any(other.startswith(f"{rel}/") for other in listed)
            if above and child.is_dir() and not child.is_symlink():
                walk(child, f"{rel}/")
            else:
                found.append(child)

    if local.is_dir():
        walk(local, "")
    return found


def _manifest_findings(co: localmd.Checkout) -> list[Finding]:
    """Report a manifest that cannot be used, lines with no vault copy, and unlisted copies."""
    manifest = localmd.manifest(co)
    findings = [
        Finding("local", str(co.local / localmanifest.MANIFEST_NAME), problem)
        for problem in manifest.problems
    ]
    for relpath in manifest.paths:
        item = localmd.path_entry(co, relpath)
        if not (item.target.is_symlink() or item.target.exists()):
            findings.append(
                Finding(
                    "local",
                    str(item.target),
                    f"{relpath} is listed in entries.toml but the vault holds no copy; "
                    "remove the line from entries.toml",
                )
            )
    # A line that cannot be used would make its own copy look unclaimed.
    if manifest.problems:
        return findings
    listed = {localmanifest.vault_relpath(relpath) for relpath in manifest.paths}
    try:
        unclaimed = _unclaimed(co.local, listed)
    except OSError as err:
        return [*findings, Finding("local", str(co.local), f"cannot be read: {err}")]
    findings.extend(
        Finding(
            "local",
            str(path),
            f"{path.relative_to(co.local).as_posix()} in the vault is not an entry; adopt it "
            "again from a checkout, or add its path to entries.toml",
        )
        for path in unclaimed
    )
    return findings


def _project_local_findings(vault: Path, slug: str, root: Path | None) -> list[Finding]:
    """Report one project's local entries, checking the checkout only when `root` exists."""
    findings = []
    legacy = _legacy_finding(vault, slug)
    if legacy is not None:
        findings.append(legacy)
    # With no registered root nothing below reaches git, so the vault is a harmless stand-in.
    co = localmd.Checkout(
        worktree=root or vault, vault=vault, slug=slug, local=paths.local_dir(vault, slug)
    )
    findings.extend(_manifest_findings(co))
    try:
        items = localmd.vault_entries(co)
        odd = localmd.misshapen(co)
    except OSError as err:
        return [*findings, Finding("local", str(co.local), f"cannot be read: {err}")]
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
        return findings
    try:
        tracked: set[str] | None = localmd.tracked_names(co, [*items, localmd.settings_entry(co)])
    except localmd.LocalMdError:
        tracked = None
    settings = _settings_finding(co, tracked=tracked)
    if settings is not None:
        findings.append(settings)
    for item in items:
        message = _checkout_message(
            co, item, tracked=None if tracked is None else item.name in tracked
        )
        if message is not None:
            findings.append(Finding("local", str(item.link), message))
    return findings


def local_instructions(vault: Path, _embedder: Embedder) -> list[Finding]:
    """Report problems with a project's local entries kept in the vault.

    Covers a vault copy of CLAUDE.local.md left at its old path, a relative `@import`
    that cannot resolve from the vault folder, and at the registered root, per entry, a
    tracked path, a dangling link, a copy that has drifted from the vault, and a link or
    copy that git does not ignore, plus a settings copy waiting on the person or one that
    cannot be copied. Also covers `entries.toml`: a file it cannot use, a listed path with
    no vault copy, and a vault copy no entry names.
    """
    try:
        projects = registry.load(vault)
    except registry.RegistryError:
        # dead_projects reports an unreadable registry.
        projects = {}
    findings = []
    for slug in paths.iter_project_slugs(vault):
        project = projects.get(slug)
        root = Path(project.root) if project and project.root else None
        findings.extend(_project_local_findings(vault, slug, root))
    return findings


def _log_shas(logs: Path) -> dict[Path, list[str]]:
    """Map each log that lists commits to its shas."""
    found: dict[Path, list[str]] = {}
    for log in field.iter_pages(logs):
        try:
            _meta, body = parse(log.read_text(encoding="utf-8", errors="replace"))
        except FrontmatterError:
            # malformed_frontmatter reports it.
            continue
        shas = [line.sha for line in changes.parse(body)]
        if shas:
            found[log] = shas
    return found


def orphaned_commits(vault: Path, _embedder: Embedder) -> list[Finding]:
    """Report logs that list commits no branch, remote-tracking ref, or tag contains.

    Local only: no fetch and no `gh`. A project whose root is missing or is not a
    repository's top-level directory, or where git cannot answer, is skipped. So is a
    shallow clone or one with a narrowed fetch refspec, since commits below the cut or on
    an unfetched branch may still exist and reconcile leaves them unchecked. An ambiguous
    short sha may be on trunk, so it is not counted.
    """
    try:
        projects = registry.load(vault)
    except registry.RegistryError:
        # dead_projects reports an unreadable registry.
        return []
    findings = []
    for slug in paths.iter_project_slugs(vault):
        project = projects.get(slug)
        root = Path(project.root) if project and project.root else None
        if root is None or not root.is_dir():
            continue
        listed = _log_shas(paths.logs_dir(vault, slug))
        if not listed:
            continue
        repo = commits.Repository(root)
        try:
            if not repo.is_repository() or repo.is_shallow():
                continue
            if repo.origin_url() is not None and not repo.fetches_all_branches():
                continue
            expansion = repo.expand({sha for shas in listed.values() for sha in shas})
            reachable = repo.ref_commits()
        except commits.CommitsError:
            continue
        for log, shas in listed.items():
            count = sum(
                1
                for sha in shas
                if sha not in expansion.ambiguous and expansion.full.get(sha) not in reachable
            )
            if count:
                plural = "s" if count != 1 else ""
                findings.append(
                    Finding(
                        "commits",
                        str(log),
                        f"lists {count} commit{plural} no branch contains; run: sessionmemory reconcile",
                    )
                )
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
    orphaned_commits,
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
