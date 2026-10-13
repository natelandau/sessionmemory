"""Know the shape of a spec: its title, preamble, and `## ` sections.

A spec is free-form markdown an agent wrote, so nothing here refuses one. Parsing reads
only what search needs and tolerates a missing H1, broken frontmatter, and CRLF endings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sessionmemory.lib import field, frontmatter

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path

STATUSES: tuple[str, ...] = ("draft", "shipped", "superseded", "abandoned")
RETIRED = frozenset({"superseded", "abandoned"})

PURPOSE_HEADINGS = frozenset(
    {"goal", "goals", "purpose", "intent", "summary", "overview", "executive summary"}
)
DECISION_HEADINGS = frozenset({"decision", "decisions", "key decisions", "approved decisions"})

# Bump whenever `embedding_input` changes what it derives from a spec. Freshness is by file
# hash, so without this an unchanged spec would keep a vector built under the old rule.
INPUT_VERSION = 2

_NUMBERING = re.compile(r"^\d+[.)]\s*")
_PART_LABEL = re.compile(r"^part \w+:\s*")
_ARTICLE = re.compile(r"^(the|our)\s+")
_TRAILING_PAREN = re.compile(r"\s*\([^)]*\)$")
_METADATA = re.compile(r"^(?:[-*]\s+)?\*{0,2}[A-Z][\w ]{0,20}:\*{0,2}(\s|$)")
_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_FENCES = ("```", "~~~")
# A leading `---` block through the next column-0 `---` line, whatever it holds.
_LEADING_BLOCK = re.compile(r"\A---\n.*?^---[ \t]*(?:\n|\Z)", re.DOTALL | re.MULTILINE)


@dataclass(frozen=True)
class Section:
    """One `## ` section of a spec, with its `###` subsections left in the body."""

    heading: str
    body: str


@dataclass(frozen=True)
class ParsedSpec:
    """A spec split into the parts search weighs differently."""

    title: str
    preamble: str
    sections: tuple[Section, ...]
    meta: dict[str, Any]


def normalize_heading(heading: str) -> str:
    """Reduce a heading to the bare name used to match the include lists.

    Numbering, a `Part X:` label, a leading article, and a trailing parenthetical are
    dropped, so `2) Intent` and `The decisions (locked)` compare as written names.

    Args:
        heading (str): The heading text without its `#` marker.

    Returns:
        str: The lowercase normalized name.
    """
    name = heading.lower().strip()
    name = _NUMBERING.sub("", name)
    name = _PART_LABEL.sub("", name)
    name = _ARTICLE.sub("", name)
    name = _TRAILING_PAREN.sub("", name)
    return name.strip(" :")


def heading_matches(heading: str, names: frozenset[str]) -> bool:
    """Report whether a heading names one of `names`, exactly or as a lead-in.

    A heading such as `Decision: start fresh` or `Goals and non-goals` leads with a listed
    name, while `Context and decision` does not, so only the opening words count.

    Args:
        heading (str): The heading text without its `#` marker.
        names (frozenset[str]): Normalized names to match against.

    Returns:
        bool: True when the normalized heading equals or opens with a name.
    """
    normalized = normalize_heading(heading)
    return any(
        normalized == name or normalized.startswith((f"{name}:", f"{name} and ")) for name in names
    )


def is_metadata_line(line: str) -> bool:
    """Report whether a line is a short `Label:` metadata line rather than prose.

    Args:
        line (str): One line of a spec's opening text.

    Returns:
        bool: True when the line opens with a short capitalized label and a colon.
    """
    return _METADATA.match(line.strip()) is not None


def _is_continuation(line: str, above: str) -> bool:
    """Report whether a line wraps the metadata entry above it rather than starting prose.

    A wrapped entry continues indented, in lowercase, or after a line ending in a comma or
    semicolon; otherwise a line opening with a capital is a fresh sentence.
    """
    return line[0].isspace() or not line[0].isupper() or above.rstrip().endswith((",", ";"))


def _clean_preamble(lines: list[str]) -> str:
    """Join the opening prose, dropping metadata entries, their wrapped lines, and rules."""
    kept: list[str] = []
    in_metadata = False
    above = ""
    for line in lines:
        if not line.strip():
            in_metadata = False
            if kept and kept[-1]:
                kept.append("")
        elif _RULE.match(line):
            continue
        elif is_metadata_line(line) or (in_metadata and _is_continuation(line, above)):
            in_metadata = True
            above = line
        else:
            in_metadata = False
            kept.append(line)
    return "\n".join(kept).strip()


def parse_spec(text: str, *, stem: str) -> ParsedSpec:
    """Split a spec into its title, preamble, and `## ` sections.

    The title comes from frontmatter, then the first H1, then `stem`. Lines inside a code
    fence never open a section or an H1, so a spec quoting markdown parses as written. A
    leading block that does not parse is dropped through its closing `---` line, and one
    that never closes is read as body, since where it ends cannot be known.

    Args:
        text (str): The full contents of the spec file.
        stem (str): The filename without its extension, used when no title is found.

    Returns:
        ParsedSpec: The title, the preamble without metadata lines, the sections, and
            the frontmatter mapping (empty when the block is absent or unparsable).
    """
    text = text.removeprefix(frontmatter.BOM).replace("\r\n", "\n")
    try:
        meta, body = frontmatter.parse(text)
    except frontmatter.MissingFrontmatterError:
        meta, body = {}, text
    except frontmatter.FrontmatterError:
        # A block that does not parse is still YAML, never prose to embed or show.
        meta, body = {}, _LEADING_BLOCK.sub("", text, count=1)

    h1: str | None = None
    preamble: list[str] = []
    headings: list[str] = []
    bodies: list[list[str]] = []
    in_fence = False

    for line in body.split("\n"):
        if line.lstrip().startswith(_FENCES):
            in_fence = not in_fence
        elif not in_fence and line.startswith("## "):
            headings.append(line[3:].strip())
            bodies.append([])
            continue
        elif not in_fence and h1 is None and not headings and line.startswith("# "):
            h1 = line[2:].strip()
            continue

        if headings:
            bodies[-1].append(line)
        else:
            preamble.append(line)

    meta_title = meta.get("title")
    title = meta_title.strip() if isinstance(meta_title, str) and meta_title.strip() else None
    return ParsedSpec(
        title=title or h1 or stem,
        preamble=_clean_preamble(preamble),
        sections=tuple(
            Section(heading=heading, body="\n".join(lines).strip())
            for heading, lines in zip(headings, bodies, strict=True)
        ),
        meta=meta,
    )


SUMMARY_LIMIT = 300

_HEADING = re.compile(r"^#{1,6}(?:\s|$)")
_NON_PROSE = re.compile(r"^(?:[-*+]\s|\d+[.)]\s|\||>|#{1,6}(?:\s|$))")


def _byte_cut(text: str, limit: int) -> str:
    """Return the first `limit` bytes of `text`, never splitting a character."""
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def cap_at_line(text: str, limit: int = field.PAGE_LIMIT) -> str:
    """Cut `text` to at most `limit` bytes, ending at a whole line when one fits.

    Args:
        text (str): The text to cap.
        limit (int): The byte budget.

    Returns:
        str: `text` unchanged when it fits, else its longest whole-line prefix, or a
            character-boundary cut when the first line alone exceeds the budget.
    """
    if len(text.encode("utf-8")) <= limit:
        return text
    head = _byte_cut(text, limit)
    cut = head.rfind("\n")
    return head[:cut] if cut != -1 else head


def embedding_input(text: str, *, stem: str) -> str:
    """Build the text a spec is embedded from.

    Parts run from most to least identifying: title, opening prose, frontmatter summary,
    purpose sections, every `## ` heading, then decision sections. Decisions come last so
    the byte cap shortens them rather than the parts that say what the spec is about.

    Args:
        text (str): The full contents of the spec file.
        stem (str): The filename without its extension, used when no title is found.

    Returns:
        str: The parts joined by blank lines, capped at `field.PAGE_LIMIT` bytes.
    """
    spec = parse_spec(text, stem=stem)
    summary = spec.meta.get("summary")
    parts = [
        spec.title,
        spec.preamble.strip(),
        summary.strip() if isinstance(summary, str) else "",
        *(s.body for s in spec.sections if heading_matches(s.heading, PURPOSE_HEADINGS)),
        "; ".join(s.heading for s in spec.sections),
        *(s.body for s in spec.sections if heading_matches(s.heading, DECISION_HEADINGS)),
    ]
    return cap_at_line("\n\n".join(part for part in parts if part))


def _first_paragraph(text: str) -> str:
    """Return the first prose paragraph of `text`, skipping lists, tables, quotes, and fences."""
    lines: list[str] = []
    in_fence = False
    for raw in text.split("\n"):
        line = raw.strip()
        if line.startswith(_FENCES):
            in_fence = not in_fence
            lines.append("")
        else:
            lines.append("" if in_fence else line)

    paragraph: list[str] = []
    for line in [*lines, ""]:
        if _HEADING.match(line) and paragraph and not _NON_PROSE.match(paragraph[0]):
            return " ".join(paragraph)
        if line:
            paragraph.append(line)
        elif paragraph:
            if not _NON_PROSE.match(paragraph[0]):
                return " ".join(paragraph)
            paragraph = []
    return ""


def _first_section_paragraph(sections: Iterable[Section]) -> str:
    """Return the first prose paragraph found across `sections`, in order."""
    return next((p for s in sections if (p := _first_paragraph(s.body))), "")


def hit_summary(text: str) -> str:
    """Pick the line of prose a search hit shows for a spec.

    Frontmatter `summary` wins, then the first prose paragraph of the purpose sections,
    then the first of the opening text, then the first of any other section
    except `Status`.

    Args:
        text (str): The full contents of the spec file.

    Returns:
        str: At most `SUMMARY_LIMIT` characters cut at a word boundary, or empty.
    """
    spec = parse_spec(text, stem="")
    summary = spec.meta.get("summary")
    result = summary.strip() if isinstance(summary, str) else ""
    if not result:
        purpose = (s for s in spec.sections if heading_matches(s.heading, PURPOSE_HEADINGS))
        result = _first_section_paragraph(purpose) or _first_paragraph(spec.preamble)
    if not result:
        others = (s for s in spec.sections if normalize_heading(s.heading) != "status")
        result = _first_section_paragraph(others)
    if len(result) > SUMMARY_LIMIT:
        result = result[:SUMMARY_LIMIT].rsplit(" ", 1)[0]
    return result


def is_retired(meta: Mapping[str, Any]) -> bool:
    """Return whether a spec's frontmatter marks it superseded or abandoned."""
    status = meta.get("status")
    return isinstance(status, str) and status in RETIRED


class SpecError(ValueError):
    """Raised when a named spec is missing or its frontmatter cannot be read."""


def resolve_name(directory: Path, name: str) -> Path:
    """Find a spec by stem, filename, or a path ending in its filename.

    Raises:
        SpecError: If no such spec exists in `directory`.
    """
    last = name.replace("\\", "/").rsplit("/", 1)[-1]
    filename = last if last.endswith(".md") else f"{last}.md"
    path = directory / filename
    if not last or not path.is_file():
        msg = f"no spec named {name!r} in {directory}"
        raise SpecError(msg)
    return path


def load_for_edit(path: Path) -> tuple[dict[str, Any], str]:
    """Read a spec's frontmatter and body, treating an absent block as empty metadata.

    Decoding is strict because the body is written back: a replaced byte would be saved
    as U+FFFD and the original lost.

    Raises:
        SpecError: If the file is not valid UTF-8, or a frontmatter block exists but
            cannot be parsed.
    """
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as error:
        msg = f"{path.name} is not valid UTF-8: {error}"
        raise SpecError(msg) from error
    try:
        return frontmatter.parse(text)
    except frontmatter.MissingFrontmatterError:
        return {}, text.removeprefix(frontmatter.BOM)
    except frontmatter.FrontmatterError as error:
        msg = f"{path.name} has frontmatter that cannot be parsed: {error}"
        raise SpecError(msg) from error


def mark_superseded(path: Path, *, by: str) -> None:
    """Mark a spec superseded by another, keeping every other key and the body.

    Raises:
        SpecError: If the spec is not valid UTF-8 or its frontmatter block cannot be parsed.
    """
    meta, body = load_for_edit(path)
    meta["status"] = "superseded"
    meta["superseded_by"] = by
    field.write_page(path, meta, body)


def status_problems(meta: Mapping[str, Any], *, filename: str, siblings: set[str]) -> list[str]:
    """List what is wrong with a spec's status, so `doctor` can suggest a fix.

    A missing status is fine: such a spec is current. `filename` is the spec's own name,
    which `superseded_by` must not name. `siblings` holds the filenames in the same
    `specs/` folder, which `superseded_by` must name, with or without `.md`.
    """
    if "status" not in meta:
        return []
    status = meta["status"]
    if not isinstance(status, str) or status not in STATUSES:
        return [f"status {status!r} is not one of {', '.join(STATUSES)}"]
    if status != "superseded":
        return []
    return _successor_problems(meta.get("superseded_by"), filename=filename, siblings=siblings)


def _successor_problems(successor: object, *, filename: str, siblings: set[str]) -> list[str]:
    """List what is wrong with a superseded spec's `superseded_by` value."""
    if not successor:
        return ["superseded without superseded_by"]
    name = str(successor)
    target = name if name.endswith(".md") else f"{name}.md"
    if target == filename:
        return ["superseded_by names this spec"]
    if target not in siblings:
        return [f"superseded_by {successor!r} names no spec in specs/"]
    return []
