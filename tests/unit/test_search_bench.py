"""Tests for the search benchmark's fixture parsing and scoring."""

from __future__ import annotations

import pytest
from scripts.search_bench import Case, FixtureError, Score, load_cases, score


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
