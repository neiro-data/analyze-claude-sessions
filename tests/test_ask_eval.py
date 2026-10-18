"""Fast unit tests for the end-to-end `ask` eval corpus and scorer (no DB, no model)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from analyze_claude_sessions.ask_eval import (
    REFUSE,
    SQL_DIR,
    TEST_PATH,
    TUNE_PATH,
    EvalCase,
    Scored,
    baseline_mismatch,
    catalog_hash,
    catalog_questions_from_sql,
    compute_metrics,
    load_cases,
    redecide,
    score_case,
    token_jaccard,
    tune,
    wilson_interval,
)
from analyze_claude_sessions.nlp import AskDecision, NlpExample, SlotSpec
from analyze_claude_sessions.nlp_stress import FakeHashBackend

CATALOG = catalog_questions_from_sql()
TUNE = load_cases(TUNE_PATH)
TEST = load_cases(TEST_PATH)


def _example(question: str = "how many sessions are there") -> NlpExample:
    return NlpExample(example_id=1, question=question, sql_text="SELECT 1")


def _case(expect: str, params: tuple[str | int, ...] | None = None) -> EvalCase:
    return EvalCase("q", expect, "paraphrase", expect_params=params)


# --- corpus ----------------------------------------------------------------


def test_catalog_source_is_populated() -> None:
    assert "how many sessions are there" in CATALOG
    assert "which model is used least" in CATALOG  # seeded by 018
    assert len(CATALOG) >= 39


@pytest.mark.parametrize("cases", [TUNE, TEST], ids=["tune", "test"])
def test_every_expect_names_a_catalog_question_or_refuse(cases: list[EvalCase]) -> None:
    bad = [c.question for c in cases if c.expect != REFUSE and c.expect not in CATALOG]
    assert not bad
    assert not [c.question for c in cases if c.expect == REFUSE and c.expect_params]


@pytest.mark.parametrize("cases", [TUNE, TEST], ids=["tune", "test"])
def test_no_duplicate_questions_within_a_split(cases: list[EvalCase]) -> None:
    questions = [c.question for c in cases]
    assert len(questions) == len(set(questions))


def test_no_question_in_both_splits() -> None:
    assert not {c.question for c in TUNE} & {c.question for c in TEST}


def test_test_split_meets_size_targets() -> None:
    kinds = [c.kind for c in TEST]
    assert len(TEST) >= 150
    assert sum(k in ("ood", "near_miss", "opposite") for k in kinds) >= 40
    assert sum(k in ("slot", "unbindable") for k in kinds) >= 20


def test_test_split_has_the_review_failures_and_top_ten() -> None:
    questions = {c.question for c in TEST}
    assert {
        "what is the cache write percentage",
        "least used model",
        "how many sessions are NOT subagents",
        "average tokens per session",
        "how much money did I spend",
        "give me the top 10 sessions by tokens",
        "top 10 projects by tokens last month",
    } <= questions


@pytest.mark.parametrize("cases", [TUNE, TEST], ids=["tune", "test"])
def test_paraphrases_are_not_catalog_wording(cases: list[EvalCase]) -> None:
    paraphrases = [c for c in cases if c.kind == "paraphrase"]
    far = [c for c in paraphrases if token_jaccard(c.question, c.expect) < 0.6]
    assert len(far) / len(paraphrases) >= 0.8


def test_test_questions_do_not_leak_into_catalog_paraphrases() -> None:
    sql = (SQL_DIR / "020_nlp_paraphrases.sql").read_text().lower()
    paraphrases = set(re.findall(r"'([^']+)'", sql))
    leaked = {c.question.lower() for c in TEST} & paraphrases
    assert not leaked


# --- scorer ----------------------------------------------------------------


def test_score_case_verdicts() -> None:
    ex = _example()
    accept = AskDecision("accept", ex, 0.9, params=())
    low = AskDecision("low_confidence", ex, 0.2)
    other = AskDecision("accept", _example("how many projects are there"), 0.9)
    assert score_case(accept, _case(ex.question)) == "correct"
    assert score_case(low, _case(REFUSE)) == "correct_refusal"
    assert score_case(AskDecision("unbindable", ex, 0.9), _case(REFUSE)) == "correct_refusal"
    assert score_case(accept, _case(REFUSE)) == "wrong_accept"
    assert score_case(other, _case(ex.question)) == "wrong_accept"
    assert score_case(low, _case(ex.question)) == "over_refusal"


def test_score_case_params_ignore_nulls_order_and_duplicates() -> None:
    ex = _example()
    d = AskDecision("accept", ex, 0.9, params=("2026-09-01", "2026-09-01", None, 5))
    assert score_case(d, _case(ex.question, (5, "2026-09-01"))) == "correct"
    assert score_case(d, _case(ex.question, (10,))) == "wrong_params"
    assert score_case(d, _case(ex.question)) == "correct"


def test_wilson_interval() -> None:
    assert wilson_interval(0, 0) == (0.0, 1.0)
    lo, hi = wilson_interval(8, 10)
    assert 0.49 < lo < 0.50 and 0.94 < hi < 0.95
    assert wilson_interval(10, 10)[1] == 1.0


def test_compute_metrics_rates_and_recall() -> None:
    ex, nx = _example(), _example("how many projects are there")
    scored = [
        Scored(_case(ex.question), "correct", AskDecision("accept", ex, 0.9, ranked=((ex, 0.9),))),
        Scored(
            _case(ex.question),
            "wrong_accept",
            AskDecision("accept", nx, 0.8, ranked=((nx, 0.8), (ex, 0.7))),
        ),
        Scored(_case(REFUSE), "correct_refusal", AskDecision("low_confidence", ex, 0.1)),
        Scored(_case(ex.question), "over_refusal", AskDecision("low_confidence", ex, 0.2)),
    ]
    m = compute_metrics(scored)
    assert (m.n, m.accuracy, m.wrong_accept, m.over_refusal) == (4, 0.5, 0.25, 0.25)
    assert m.recall_at_1 == pytest.approx(1 / 3)
    assert m.recall_at_3 == pytest.approx(2 / 3)


def test_token_jaccard() -> None:
    assert token_jaccard("a b c", "a b c") == 1.0
    assert token_jaccard("a b", "c d") == 0.0


# --- baseline --------------------------------------------------------------


def test_baseline_mismatch_demands_rebaseline() -> None:
    base = {"model_name": "m1", "catalog_hash": "h1"}
    assert baseline_mismatch(base, model_name="m1", examples_hash="h1") == ""
    assert "re-baseline" in baseline_mismatch(base, model_name="m2", examples_hash="h1")
    assert "re-baseline" in baseline_mismatch(base, model_name="m1", examples_hash="h2")


def test_catalog_hash_tracks_sql_and_slots() -> None:
    a = _example()
    b = NlpExample(1, a.question, "SELECT 2")
    c = NlpExample(1, a.question, a.sql_text, slots=(SlotSpec("since", "since", optional=True),))
    assert len({catalog_hash([a]), catalog_hash([b]), catalog_hash([c])}) == 3
    assert catalog_hash([a]) == catalog_hash([a])


# --- tuning ----------------------------------------------------------------


def test_redecide_applies_threshold_to_floor_decision() -> None:
    ex = _example()
    floor = AskDecision("accept", ex, 0.6, params=())
    assert redecide(floor, 0.5).outcome == "accept"
    assert redecide(floor, 0.7).outcome == "low_confidence"
    refused = AskDecision("no_examples")
    assert redecide(refused, 0.9) is refused


def test_tune_reads_only_the_tune_split(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    opened: list[str] = []
    real_open = Path.open

    def spy(self: Path, *args: Any, **kwargs: Any) -> Any:
        opened.append(self.name)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", spy)
    examples = [_example(q) for q in sorted(CATALOG)[:12]]
    rows, chosen = tune(examples, FakeHashBackend(), (0.3, 0.5, 0.7), cache_dir=tmp_path)
    assert [r.threshold for r in rows] == [0.3, 0.5, 0.7]
    assert chosen is None or chosen[0] in (0.3, 0.5, 0.7)
    assert TUNE_PATH.name in opened
    assert TEST_PATH.name not in opened
