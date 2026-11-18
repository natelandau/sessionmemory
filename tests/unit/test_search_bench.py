"""Tests for the search benchmark's fixture parsing and scoring."""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts.search_bench import Case, FixtureError, Ranking, Score, _sweep, load_cases, score

from sessionmemory.lib import fieldindex


def _case(answers: tuple[str, ...]) -> Case:
    return Case(
        set="A",
        project="invoice-api",
        field="learnings",
        form="keywords",
        query="q",
        answers=frozenset(answers),
    )


@pytest.mark.parametrize(
    ("results", "expected"),
    [
        pytest.param(
            [(("a.md",), ["a.md"])],
            Score(answered=1, unanswered=0, miss=0, false_hit=0, wrong=0, first=1),
            id="answer-first",
        ),
        pytest.param(
            [(("a.md",), ["b.md", "a.md"])],
            Score(answered=1, unanswered=0, miss=0, false_hit=0, wrong=1, first=0),
            id="answer-second-counts-one-wrong",
        ),
        pytest.param(
            [(("a.md", "b.md"), ["b.md"])],
            Score(answered=1, unanswered=0, miss=0, false_hit=0, wrong=0, first=1),
            id="any-listed-answer-counts",
        ),
        pytest.param(
            [(("a.md",), [])],
            Score(answered=1, unanswered=0, miss=1, false_hit=0, wrong=0, first=0),
            id="nothing-returned-is-a-miss",
        ),
        pytest.param(
            [(("a.md",), ["b.md", "c.md"])],
            Score(answered=1, unanswered=0, miss=1, false_hit=0, wrong=2, first=0),
            id="only-non-answers-is-a-miss",
        ),
        pytest.param(
            [((), ["b.md", "c.md"])],
            Score(answered=0, unanswered=1, miss=0, false_hit=1, wrong=2, first=0),
            id="unanswered-with-hits-is-one-false-hit",
        ),
        pytest.param(
            [((), [])],
            Score(answered=0, unanswered=1, miss=0, false_hit=0, wrong=0, first=0),
            id="unanswered-and-empty-is-right",
        ),
    ],
)
def test_score_counts_as_the_search_specs_do(results, expected):
    """Verify misses, false hits, wrong hits, and first-ranked answers are counted per case."""
    assert score([(_case(answers), hits) for answers, hits in results]) == expected


@pytest.mark.parametrize(
    ("case", "message"),
    [
        pytest.param(
            'project = "p"\nfield = "learnings"\nform = "keywords"\nquery = "q"',
            "lacks answers",
            id="missing-answers",
        ),
        pytest.param(
            'project = "p"\nfield = "plans"\nform = "keywords"\nquery = "q"\nanswers = []',
            "field 'plans'",
            id="unknown-field",
        ),
        pytest.param(
            'project = "p"\nfield = "logs"\nform = "keywords"\nquery = "q"\nanswers = "a.md"',
            "list of filenames",
            id="answers-not-a-list",
        ),
        pytest.param(
            'project = "p"\nfield = "logs"\nform = "phrase"\nquery = "q"\nanswers = []',
            "form 'phrase'",
            id="unknown-form",
        ),
    ],
)
def test_load_cases_rejects_a_case_it_cannot_score(case, message):
    """Verify a malformed case stops the run instead of being scored wrongly."""
    with pytest.raises(FixtureError, match=message):
        load_cases(f"[[case]]\n{case}\n")


def test_load_cases_reads_answers_as_a_set():
    """Verify a valid case loads with its answers and an empty list means no answer."""
    cases = load_cases(
        '[[case]]\nproject = "p"\nfield = "specs"\nform = "identifier"\nquery = "q"\n'
        'answers = ["a.md", "b.md"]\n\n'
        '[[case]]\nproject = "p"\nfield = "logs"\nform = "question"\nquery = "q2"\nanswers = []\n'
    )
    assert [(case.field, case.answers) for case in cases] == [
        ("specs", frozenset({"a.md", "b.md"})),
        ("logs", frozenset()),
    ]


def _ranking(answers: tuple[str, ...], distances: dict[str, float]) -> Ranking:
    pages = [
        fieldindex.Ranked(filename=name, frontmatter="{}", distance=distance)
        for name, distance in sorted(distances.items(), key=lambda item: item[1])
    ]
    return Ranking(case=_case(answers), field_dir=Path("learnings"), pages=pages, seconds=0.0)


def test_sweep_keeps_only_rules_that_trade_a_false_hit_for_fewer_misses():
    """Verify the frontier lists each false-hit count only when it buys fewer misses."""
    # Given an answer at 0.40 and an unanswered query whose nearest page sits at 0.38
    background = {f"bg{n}.md": 0.9 for n in range(10)}
    rankings = [
        _ranking(("a.md",), {"a.md": 0.40, **background}),
        _ranking((), {"b.md": 0.38, **background}),
    ]

    # When the grid is swept
    rows = _sweep(rankings)

    # Then the frontier runs from no false hit with a miss to one false hit with none
    assert [(row["false_hit"], row["miss"]) for row in rows] == [(0, 1), (1, 0)]


def test_sweep_of_no_cases_reports_no_rule():
    """Verify a group the fixture never names yields no frontier."""
    assert _sweep([]) == []
