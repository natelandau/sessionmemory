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

Each query is ranked once. Every rule is then applied to that same ranking, so a sweep
of the cutoffs costs no extra embedding.
"""

from __future__ import annotations

import argparse
import shutil
import statistics
import tempfile
import time
import tomllib
from dataclasses import asdict, dataclass
from itertools import product
from pathlib import Path
from typing import Literal

from nclutils import pp
from rich.table import Table

from sessionmemory.commands._common import emit_json
from sessionmemory.lib import config, embed, fieldindex, paths, specs

Field = Literal["learnings", "logs", "specs"]
FIELDS: tuple[Field, ...] = ("learnings", "logs", "specs")
FORMS = ("keywords", "question", "identifier")

# The CLI's default --limit, so "wrong" counts what a caller would actually see.
LIMIT = 10

# The grid the earlier measurements swept: margins 0.05 to 0.20, distances 0.35 to 0.55.
SWEEP_MARGINS = tuple(round(0.05 + step * 0.01, 2) for step in range(16))
SWEEP_DISTANCES = tuple(round(0.35 + step * 0.01, 2) for step in range(21))


@dataclass(frozen=True)
class Case:
    """One labeled query."""

    set: str
    project: str
    field: Field
    form: str
    query: str
    answers: frozenset[str]


@dataclass(frozen=True)
class Rule:
    """One pair of cutoffs for `fieldindex.admit`."""

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
        if raw["field"] not in FIELDS:
            msg = f"case {number} names field {raw['field']!r}; expected one of {FIELDS}"
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


def apply_rule(case: Case, ranking: list[fieldindex.Ranked], rule: Rule) -> list[str]:
    """Return what `search` would return for `case` under `rule`, as filenames."""
    admitted = fieldindex.admit(ranking, max_distance=rule.max_distance, min_margin=rule.min_margin)
    exclude = specs.is_retired if case.field == "specs" else None
    return [
        page.filename for page in fieldindex.select_hits(admitted, limit=LIMIT, exclude=exclude)
    ]


def default_rule(field: Field) -> Rule:
    """Return the cutoffs `search` uses for `field` today."""
    max_distance, min_margin = fieldindex.default_cutoffs(Path(field))
    return Rule(max_distance=max_distance, min_margin=min_margin)


def _copy_fields(vault: Path, cases: list[Case], into: Path) -> dict[tuple[str, str], Path]:
    copies = {}
    for project, field in sorted({(case.project, case.field) for case in cases}):
        source = paths.project_dir(vault, project) / field
        if not source.is_dir():
            msg = f"{project}/{field} is not a directory in {vault}"
            raise FixtureError(msg)
        # The copy keeps the field's directory name, since `specs` selects the specs rules.
        target = into / project / field
        shutil.copytree(source, target)
        copies[project, field] = target
    return copies


def _check_answers(cases: list[Case], copies: dict[tuple[str, str], Path]) -> None:
    for case in cases:
        absent = sorted(
            name for name in case.answers if not (copies[case.project, case.field] / name).is_file()
        )
        if absent:
            msg = f"{case.query!r}: {case.project}/{case.field} has no {', '.join(absent)}"
            raise FixtureError(msg)


def _rank_all(
    cases: list[Case], copies: dict[tuple[str, str], Path]
) -> tuple[list[list[fieldindex.Ranked]], list[float]]:
    embedder = embed.default_embedder()
    # Refresh every copy before timing, so no query pays for embedding the pages.
    for directory in copies.values():
        fieldindex.refresh(directory, embedder)
    rankings, seconds = [], []
    for case in cases:
        started = time.perf_counter()
        rankings.append(fieldindex.rank(copies[case.project, case.field], embedder, case.query))
        seconds.append(time.perf_counter() - started)
    return rankings, seconds


def _group_key(case: Case) -> tuple[str, str, str]:
    return (case.set, case.field, case.form)


def _rows(results: list[tuple[Case, list[str]]], seconds: list[float]) -> list[dict[str, object]]:
    groups: dict[tuple[str, str, str], list[int]] = {}
    for index, (case, _hits) in enumerate(results):
        groups.setdefault(_group_key(case), []).append(index)
    rows = []
    for key in sorted(groups):
        members = groups[key]
        counts = score([results[index] for index in members])
        rows.append(
            {
                "set": key[0],
                "field": key[1],
                "form": key[2],
                **asdict(counts),
                "median_ms": round(
                    statistics.median(seconds[index] for index in members) * 1000, 1
                ),
            }
        )
    total = score(results)
    rows.append(
        {
            "set": "all",
            "field": "",
            "form": "",
            **asdict(total),
            "median_ms": round(statistics.median(seconds) * 1000, 1),
        }
    )
    return rows


def _sweep(
    cases: list[Case], rankings: list[list[fieldindex.Ranked]], *, specs_only: bool
) -> list[dict[str, object]]:
    """Score the cutoff grid on one constant group, the fields or specs, and keep its frontier.

    For each false-hit count, keep the rule with the fewest misses, then the fewest
    wrong hits, and drop it when a rule with fewer false hits misses no more. What is
    left is the whole trade-off between misses and false hits.
    """
    chosen = [
        (case, ranking)
        for case, ranking in zip(cases, rankings, strict=True)
        if (case.field == "specs") is specs_only
    ]
    if not chosen:
        return []
    best: dict[int, tuple[Rule, Score]] = {}
    for max_distance, min_margin in product(SWEEP_DISTANCES, SWEEP_MARGINS):
        rule = Rule(max_distance=max_distance, min_margin=min_margin)
        counts = score([(case, apply_rule(case, ranking, rule)) for case, ranking in chosen])
        held = best.get(counts.false_hit)
        if held is None or (counts.miss, counts.wrong) < (held[1].miss, held[1].wrong):
            best[counts.false_hit] = (rule, counts)
    rows = []
    fewest_misses = None
    for false_hits in sorted(best):
        rule, counts = best[false_hits]
        if fewest_misses is not None and counts.miss >= fewest_misses:
            continue
        fewest_misses = counts.miss
        rows.append({"rule": rule.label, **asdict(counts)})
    return rows


def _case_rows(
    cases: list[Case], rankings: list[list[fieldindex.Ranked]], results: list[list[str]]
) -> list[dict[str, object]]:
    """Describe each case under the default rule, for finding why a query missed."""
    rows: list[dict[str, object]] = []
    for case, ranking, returned in zip(cases, rankings, results, strict=True):
        positions = [index for index, page in enumerate(ranking) if page.filename in case.answers]
        nearest = ranking[positions[0]] if positions else None
        rows.append(
            {
                "set": case.set,
                "project": case.project,
                "field": case.field,
                "form": case.form,
                "query": case.query,
                "answers": sorted(case.answers),
                "returned": returned,
                "answer_rank": positions[0] + 1 if positions else None,
                "answer_distance": round(nearest.distance, 3) if nearest else None,
                "median_distance": round(statistics.median(page.distance for page in ranking), 3)
                if ranking
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
        rankings, seconds = _rank_all(cases, copies)

    results = [
        (case, apply_rule(case, ranking, default_rule(case.field)))
        for case, ranking in zip(cases, rankings, strict=True)
    ]
    defaults = _rows(results, seconds)
    sweeps: dict[str, list[dict[str, object]]] = {}
    if arguments.sweep:
        sweeps["sweep_fields"] = _sweep(cases, rankings, specs_only=False)
        sweeps["sweep_specs"] = _sweep(cases, rankings, specs_only=True)

    if arguments.json:
        emit_json(
            {
                "defaults": defaults,
                **sweeps,
                "cases": _case_rows(cases, rankings, [hits for _case, hits in results]),
            }
        )
        return
    counts = ["answered", "unanswered", "miss", "false_hit", "wrong", "first"]
    pp.console().print(_table("Defaults", defaults, ["set", "field", "form", *counts, "median_ms"]))
    titles = {"sweep_fields": "Sweep: learnings and logs", "sweep_specs": "Sweep: specs"}
    for key, rows in sweeps.items():
        if rows:
            pp.console().print()
            pp.console().print(_table(titles[key], rows, ["rule", *counts]))


if __name__ == "__main__":
    main()
