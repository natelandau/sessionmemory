"""Score search cutoffs against a fixture of labeled queries.

The thresholds in `lib/fieldindex.py` may change only by re-measuring, and this is the
measurement. The fixture names real vault pages, so it lives outside the repository and
its path is the one argument:

    uv run python scripts/search_bench.py FIXTURE [--sweep] [--json]

Each case is one query against one field of one project, with the filenames that answer
it (an empty list when nothing does):

    [[case]]
    set = "A"            # a label for grouping results; it changes nothing
    project = "invoice-api"
    field = "learnings"  # learnings | logs | specs
    form = "keywords"    # keywords | question | identifier
    query = "stripe webhook retries"
    answers = ["stripe-retries-webhooks-for-three-days.md"]

Every field the fixture names is copied to a temporary directory first, since a search
refreshes the index it reads and a benchmark must never write into the vault. The real
embedding model always runs, because the test stub's distances measure nothing.

Each query is ranked once. Every rule is then applied to that same ranking through
`fieldindex.cut`, the step `search` runs, so a sweep costs no extra embedding and
measures the shipped rule.
"""

from __future__ import annotations

import argparse
import shutil
import statistics
import tempfile
import time
import tomllib
from dataclasses import asdict, dataclass, fields
from itertools import product
from pathlib import Path

from nclutils import pp
from rich.table import Table

from sessionmemory.commands._common import emit_json
from sessionmemory.lib import config, embed, fieldindex, paths

FORMS = ("keywords", "question", "identifier")

# The grid the earlier measurements swept: margins 0.05 to 0.20, distances 0.35 to 0.55.
SWEEP_MARGINS = tuple(round(0.05 + step * 0.01, 2) for step in range(16))
SWEEP_DISTANCES = tuple(round(0.35 + step * 0.01, 2) for step in range(21))


@dataclass(frozen=True)
class Case:
    """One labeled query."""

    set: str
    project: str
    field: str
    form: str
    query: str
    answers: frozenset[str]


@dataclass(frozen=True)
class Rule:
    """One pair of cutoffs for `fieldindex.cut`."""

    max_distance: float
    min_margin: float

    @property
    def label(self) -> str:
        """Name the rule the way the spec tables do."""
        return f"margin >= {self.min_margin:.2f}, distance <= {self.max_distance:.2f}"


@dataclass(frozen=True)
class Score:
    """How one rule did on a group of cases, counted as the search specs count.

    A miss is an answered query that returned none of its answers, a false hit is an
    unanswered query that returned anything, wrong is every returned non-answer, and
    first is an answered query whose top hit is an answer.
    """

    answered: int
    unanswered: int
    miss: int
    false_hit: int
    wrong: int
    first: int


COUNTS = [count.name for count in fields(Score)]


@dataclass(frozen=True)
class Ranking:
    """One case ranked once against its copied field."""

    case: Case
    field_dir: Path
    pages: list[fieldindex.Ranked]
    seconds: float

    def hits(self, rule: Rule | None = None) -> list[str]:
        """Return what `search` returns for this case under `rule`, or its defaults, as filenames."""
        return [
            page.filename
            for page in fieldindex.cut(
                self.pages,
                self.field_dir,
                limit=fieldindex.DEFAULT_LIMIT,
                max_distance=None if rule is None else rule.max_distance,
                min_margin=None if rule is None else rule.min_margin,
                exclude=fieldindex.default_exclude(self.field_dir),
            )
        ]


class FixtureError(ValueError):
    """A fixture case that cannot be scored."""


def load_cases(text: str) -> list[Case]:
    """Parse a fixture, rejecting a case the benchmark would score wrongly.

    Args:
        text: The fixture's TOML.

    Returns:
        list[Case]: Every case, in file order.

    Raises:
        FixtureError: If a case misses a key or names an unknown field or form.
    """
    cases = []
    for number, raw in enumerate(tomllib.loads(text).get("case", []), start=1):
        missing = [
            key for key in ("project", "field", "form", "query", "answers") if key not in raw
        ]
        if missing:
            msg = f"case {number} ({raw.get('query', '?')!r}) lacks {', '.join(missing)}"
            raise FixtureError(msg)
        if raw["field"] not in paths.INDEX_DIRS:
            msg = f"case {number} names field {raw['field']!r}; expected one of {paths.INDEX_DIRS}"
            raise FixtureError(msg)
        if raw["form"] not in FORMS:
            msg = f"case {number} names form {raw['form']!r}; expected one of {FORMS}"
            raise FixtureError(msg)
        cases.append(
            Case(
                set=str(raw.get("set", "")),
                project=raw["project"],
                field=raw["field"],
                form=raw["form"],
                query=raw["query"],
                answers=frozenset(raw["answers"]),
            )
        )
    return cases


def score(results: list[tuple[Case, list[str]]]) -> Score:
    """Count misses, false hits, wrong hits, and first-ranked answers.

    Args:
        results: Each case with the filenames the rule returned for it, best first.

    Returns:
        Score: The counts over every case given.
    """
    answered = [(case, hits) for case, hits in results if case.answers]
    unanswered = [hits for case, hits in results if not case.answers]
    return Score(
        answered=len(answered),
        unanswered=len(unanswered),
        miss=sum(1 for case, hits in answered if not case.answers.intersection(hits)),
        false_hit=sum(1 for hits in unanswered if hits),
        wrong=sum(1 for case, hits in results for hit in hits if hit not in case.answers),
        first=sum(1 for case, hits in answered if hits and hits[0] in case.answers),
    )


def _copy_fields(vault: Path, cases: list[Case], into: Path) -> dict[tuple[str, str], Path]:
    copies = {}
    for project, field in sorted({(case.project, case.field) for case in cases}):
        source = paths.project_dir(vault, project) / field
        if not source.is_dir():
            msg = f"{project}/{field} is not a directory in {vault}"
            raise FixtureError(msg)
        # The copy keeps the directory's name, since `fieldindex` picks the specs rules by it.
        target = into / project / field
        shutil.copytree(source, target)
        copies[project, field] = target
    return copies


def _check_answers(cases: list[Case], copies: dict[tuple[str, str], Path]) -> None:
    for case in cases:
        field_dir = copies[case.project, case.field]
        absent = sorted(name for name in case.answers if not (field_dir / name).is_file())
        if absent:
            msg = f"{case.query!r}: {case.project}/{case.field} has no {', '.join(absent)}"
            raise FixtureError(msg)


def _rank_all(cases: list[Case], copies: dict[tuple[str, str], Path]) -> list[Ranking]:
    embedder = embed.default_embedder()
    # Refresh every copy before timing, so no query pays for embedding the pages.
    for field_dir in copies.values():
        fieldindex.refresh(field_dir, embedder)
    rankings = []
    for case in cases:
        field_dir = copies[case.project, case.field]
        started = time.perf_counter()
        pages = fieldindex.rank(field_dir, embedder, case.query)
        rankings.append(
            Ranking(
                case=case,
                field_dir=field_dir,
                pages=pages,
                seconds=time.perf_counter() - started,
            )
        )
    return rankings


def _row(label: tuple[str, str, str], rankings: list[Ranking]) -> dict[str, object]:
    counts = score([(ranking.case, ranking.hits()) for ranking in rankings])
    median_ms = statistics.median(ranking.seconds for ranking in rankings) * 1000
    return {
        "set": label[0],
        "field": label[1],
        "form": label[2],
        **asdict(counts),
        "median_ms": round(median_ms, 1),
    }


def _default_rows(rankings: list[Ranking]) -> list[dict[str, object]]:
    """Score the default cutoffs per set, field, and form, then over every case."""
    groups: dict[tuple[str, str, str], list[Ranking]] = {}
    for ranking in rankings:
        case = ranking.case
        groups.setdefault((case.set, case.field, case.form), []).append(ranking)
    return [
        *(_row(label, groups[label]) for label in sorted(groups)),
        _row(("all", "", ""), rankings),
    ]


def _sweep(rankings: list[Ranking]) -> list[dict[str, object]]:
    """Score the cutoff grid on one constant group, the fields or specs, and keep its frontier.

    For each false-hit count, keep the rule with the fewest misses, then the fewest
    wrong hits, and drop it when a rule with fewer false hits misses no more. What is
    left is the whole trade-off between misses and false hits.
    """
    best: dict[int, tuple[Rule, Score]] = {}
    for max_distance, min_margin in product(SWEEP_DISTANCES, SWEEP_MARGINS):
        rule = Rule(max_distance=max_distance, min_margin=min_margin)
        counts = score([(ranking.case, ranking.hits(rule)) for ranking in rankings])
        held = best.get(counts.false_hit)
        if held is None or (counts.miss, counts.wrong) < (held[1].miss, held[1].wrong):
            best[counts.false_hit] = (rule, counts)
    rows: list[dict[str, object]] = []
    fewest_misses = None
    for false_hits in sorted(best):
        rule, counts = best[false_hits]
        if fewest_misses is not None and counts.miss >= fewest_misses:
            continue
        fewest_misses = counts.miss
        rows.append({"rule": rule.label, **asdict(counts)})
    return rows


def _case_rows(rankings: list[Ranking]) -> list[dict[str, object]]:
    """Describe each case under the default cutoffs, for finding why a query missed."""
    rows = []
    for ranking in rankings:
        case, pages = ranking.case, ranking.pages
        first_answer = next(
            (rank for rank, page in enumerate(pages, start=1) if page.filename in case.answers),
            None,
        )
        rows.append(
            {
                **asdict(case),
                "answers": sorted(case.answers),
                "returned": ranking.hits(),
                "answer_rank": first_answer,
                "answer_distance": round(pages[first_answer - 1].distance, 3)
                if first_answer
                else None,
                "median_distance": round(statistics.median(page.distance for page in pages), 3)
                if pages
                else None,
            }
        )
    return rows


_HEADERS = {"answered": "ans", "unanswered": "none", "false_hit": "false", "median_ms": "ms"}


def _table(title: str, rows: list[dict[str, object]], columns: list[str]) -> Table:
    table = Table(title=title, box=None, pad_edge=False, header_style="bold", title_justify="left")
    for column in columns:
        table.add_column(
            _HEADERS.get(column, column),
            justify="left" if column in {"set", "field", "form", "rule"} else "right",
            no_wrap=True,
        )
    for row in rows:
        table.add_row(*(str(row[column]) for column in columns))
    return table


def main() -> None:
    """Rank every fixture query once, then report each rule's counts."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("fixture", type=Path, help="The fixture TOML.")
    parser.add_argument("--sweep", action="store_true", help="Also score the whole cutoff grid.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of tables.")
    arguments = parser.parse_args()

    cases = load_cases(arguments.fixture.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="search-bench-") as scratch:
        copies = _copy_fields(config.vault_root(), cases, Path(scratch))
        _check_answers(cases, copies)
        rankings = _rank_all(cases, copies)

        defaults = _default_rows(rankings)
        case_rows = _case_rows(rankings)
        # Each group shares one pair of constants: `DEFAULT_*` for the fields, `SPECS_*` for specs.
        sweeps = {
            title: _sweep([r for r in rankings if (r.case.field == paths.SPECS_DIR) is for_specs])
            for title, for_specs in (("Sweep: learnings and logs", False), ("Sweep: specs", True))
            if arguments.sweep
        }

    if arguments.json:
        emit_json({"defaults": defaults, "sweeps": sweeps, "cases": case_rows})
        return
    pp.console().print(_table("Defaults", defaults, ["set", "field", "form", *COUNTS, "median_ms"]))
    for title, rows in sweeps.items():
        if rows:
            pp.console().print()
            pp.console().print(_table(title, rows, ["rule", *COUNTS]))


if __name__ == "__main__":
    main()
