"""Fast, deterministic, model-free stress tests.

No network, no Postgres, no real sentence-transformers model: retrieval
tests use `analyze_claude_projects.nlp_stress.FakeHashBackend`, a
hash-based deterministic stand-in for `Backend`. Every fuzz loop is seeded
with an explicit `random.Random(seed)` and reports that seed on failure so a
red test reproduces exactly.

See tests/test_nlp_stress_slow.py for the model-backed counterpart and
tests/data/nlp_stress.yaml for the shared out-of-catalog / perturbation-class
corpus.
"""

from __future__ import annotations

import random
import string

import pytest

from analyze_claude_projects.nlp import (
    CatalogRetriever,
    NlpExample,
    SlotSpec,
    UnsafeQueryError,
    check_bindable,
    count_placeholders,
    ensure_read_only,
    extract_slots,
    filter_examples,
)
from analyze_claude_projects.nlp_stress import (
    SQL_ADVERSARIAL_CORPUS,
    FakeHashBackend,
    generate_slot_questions,
)

_KNOWN_PROJECTS = ["api-gateway", "api", "billing", "main", "data", "top"]
_KNOWN_MODELS = ["claude-opus-4", "claude-haiku", "since", "last"]


# --------------------------------------------------------------------------
# extract_slots: fuzz invariants
# --------------------------------------------------------------------------


def _random_text(rng: random.Random, length: int) -> str:
    """Arbitrary printable + unicode noise, for the "never raises" fuzz."""
    alphabet = string.printable + "éñ日本語😀 $$''--;\t"
    return "".join(rng.choice(alphabet) for _ in range(length))


@pytest.mark.parametrize("seed", range(25))
def test_extract_slots_never_raises_on_generated_input(seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311 - deterministic fuzz, not crypto
    for case in generate_slot_questions(rng, count=10):
        try:
            slots, cleaned = extract_slots(
                case.question,
                known_projects=list(case.known_projects),
                known_models=list(case.known_models),
                today=case.today,
            )
        except Exception as exc:  # noqa: BLE001 - the invariant under test
            pytest.fail(f"seed={seed} question={case.question!r} raised {exc!r}")
        assert isinstance(slots, dict)
        assert isinstance(cleaned, str)


@pytest.mark.parametrize("seed", range(25))
def test_extract_slots_never_raises_on_arbitrary_noise(seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311 - deterministic fuzz, not crypto
    for _ in range(10):
        text = _random_text(rng, rng.randint(0, 80))
        try:
            extract_slots(text, known_projects=_KNOWN_PROJECTS, known_models=_KNOWN_MODELS)
        except Exception as exc:  # noqa: BLE001 - the invariant under test
            pytest.fail(f"seed={seed} text={text!r} raised {exc!r}")


@pytest.mark.parametrize(
    "question",
    [
        "check session_id=abc-123",
        "count for api-gateway export",
        "usage for claude-opus-4 model",
        "top 7 sessions",
    ],
)
def test_extract_slots_cleaned_has_no_extracted_literal_span(question: str) -> None:
    slots, cleaned = extract_slots(
        question, known_projects=_KNOWN_PROJECTS, known_models=_KNOWN_MODELS
    )
    for name in ("session_id", "project", "model"):
        value = slots.get(name)
        if value is not None:
            assert value.lower() not in cleaned.lower()


@pytest.mark.parametrize(
    "question",
    [
        "check session_id=abc-123",
        "count for api-gateway export",
        "usage for claude-opus-4 model",
        "top 7 sessions",
        "since 2026-01-01 report",
        "last month report",
    ],
)
def test_extract_slots_idempotent_on_single_occurrence_input(question: str) -> None:
    slots, cleaned = extract_slots(
        question, known_projects=_KNOWN_PROJECTS, known_models=_KNOWN_MODELS
    )
    slots_again, cleaned_again = extract_slots(
        cleaned, known_projects=_KNOWN_PROJECTS, known_models=_KNOWN_MODELS
    )
    # Re-running on the already-cleaned text must not re-discover a slot type
    # that was already extracted (its literal span is gone).
    assert not (set(slots) & set(slots_again))
    assert cleaned_again == cleaned


def test_extract_slots_today_injection_is_deterministic() -> None:
    from datetime import date

    fixed_today = date(2026, 3, 15)
    slots_a, _ = extract_slots("last 10 days", today=fixed_today)
    slots_b, _ = extract_slots("last 10 days", today=fixed_today)
    assert slots_a == slots_b == {"since": date(2026, 3, 5)}

    slots_month, _ = extract_slots("last month", today=fixed_today)
    assert slots_month == {"since": date(2026, 2, 1)}


# --------------------------------------------------------------------------
# extract_slots: edge cases
# --------------------------------------------------------------------------


def test_extract_slots_invalid_month_raises() -> None:
    # DOCUMENTED DEFECT (nlp.py:319, high): `since 2026-13-45` builds
    # date(2026, 13, 45) with no validation, so `date()` raises ValueError
    # instead of extract_slots treating it as "no since slot found". Any
    # caller (e.g. the CLI) that doesn't wrap extract_slots in a try/except
    # will crash on this user input instead of failing gracefully.
    with pytest.raises(ValueError, match="month must be in 1..12"):
        extract_slots("since 2026-13-45")


def test_extract_slots_invalid_day_raises() -> None:
    # Same class of defect (nlp.py:319) for an out-of-range day in a
    # calendar-valid month.
    with pytest.raises(ValueError, match="day is out of range for month"):
        extract_slots("since 2026-02-30")


def test_extract_slots_last_zero_days() -> None:
    from datetime import date

    slots, cleaned = extract_slots("last 0 days", today=date(2026, 1, 15))
    assert slots == {"since": date(2026, 1, 15)}
    assert cleaned == ""


def test_extract_slots_last_huge_days_does_not_raise() -> None:
    slots, _ = extract_slots("last 99999 days")
    assert "since" in slots  # underflows to a very old (but valid) date


def test_extract_slots_top_n_shadowed_by_project_name_collision() -> None:
    # DOCUMENTED DEFECT (nlp.py:335-350, medium): project matching runs
    # before the `top N` pattern. If "top" is itself a known project name,
    # "top 0" is parsed as project="top" with the "0" left dangling in the
    # cleaned text, and the `limit` slot is never extracted at all.
    slots, cleaned = extract_slots("top 0", known_projects=["top"])
    assert slots == {"project": "top"}
    assert "limit" not in slots
    assert cleaned == "0"


def test_extract_slots_top_n_leading_zeros() -> None:
    slots, cleaned = extract_slots("top 000012")
    assert slots == {"limit": 12}
    assert cleaned == ""


def test_extract_slots_multiple_session_ids_keeps_first_only() -> None:
    slots, cleaned = extract_slots("session_id=abc session_id=def")
    assert slots["session_id"] == "abc"
    assert "session_id=def" in cleaned


def test_extract_slots_empty_session_id_value_swallows_next_token() -> None:
    # DOCUMENTED DEFECT (nlp.py:29, medium): `_SESSION_ID_RE` requires
    # `[\w-]+` (one or more), so "session_id=" with nothing after it is not
    # a match on its own -- but if whitespace precedes an unrelated word, the
    # regex reaches past the whitespace and swallows that word as the
    # session_id, silently misattributing unrelated text as an id.
    slots, cleaned = extract_slots("session_id= foo")
    assert slots["session_id"] == "foo"
    assert cleaned == ""


def test_extract_slots_uuid_adjacent_to_punctuation() -> None:
    question = "check 11111111-1111-1111-1111-111111111111, please"
    slots, cleaned = extract_slots(question)
    assert slots["session_id"] == "11111111-1111-1111-1111-111111111111"
    assert cleaned == "check , please"


@pytest.mark.parametrize("project", ["c++", "(beta)"])
def test_extract_slots_project_with_trailing_non_word_metachar_not_matched(
    project: str,
) -> None:
    # DOCUMENTED DEFECT (nlp.py:370, medium): `_match_known_value` wraps the
    # value in `\b...\b`. `\b` only fires at a transition between a word char
    # and a non-word char. Values that both start and end on a non-word
    # character (here "+" and ")") never get a trailing `\b`, so they are
    # never matched at all, even when surrounded by spaces.
    slots, cleaned = extract_slots(f"count for {project}", known_projects=[project])
    assert "project" not in slots
    assert cleaned == f"count for {project}"


@pytest.mark.parametrize("project", ["a.b", "x|y"])
def test_extract_slots_project_with_word_char_ending_metachar_matches(
    project: str,
) -> None:
    slots, cleaned = extract_slots(f"count for {project}", known_projects=[project])
    assert slots["project"] == project
    assert cleaned == "count for"


def test_extract_slots_project_common_english_word() -> None:
    slots, cleaned = extract_slots("show me main data", known_projects=["main", "data"])
    assert slots["project"] in {"main", "data"}
    assert cleaned != "show me main data"


def test_extract_slots_longest_project_wins_over_substring() -> None:
    slots, cleaned = extract_slots(
        "count for api-gateway export", known_projects=["api", "api-gateway"]
    )
    assert slots["project"] == "api-gateway"
    assert "api" not in cleaned


def test_extract_slots_model_colliding_with_since_last_tokens() -> None:
    slots, cleaned = extract_slots("model since here", known_models=["since", "last"])
    assert slots["model"] == "since"
    assert cleaned == "model here"


def test_extract_slots_all_five_slots_in_one_question() -> None:
    from datetime import date

    question = "all five slots session_id=abc since 2026-01-01 main top-model top 5"
    slots, cleaned = extract_slots(question, known_projects=["main"], known_models=["top-model"])
    assert slots == {
        "session_id": "abc",
        "since": date(2026, 1, 1),
        "project": "main",
        "model": "top-model",
        "limit": 5,
    }
    assert cleaned == "all five slots"


# --------------------------------------------------------------------------
# ensure_read_only: adversarial corpus
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case", SQL_ADVERSARIAL_CORPUS, ids=[c.case_id for c in SQL_ADVERSARIAL_CORPUS]
)
def test_ensure_read_only_adversarial_corpus(case) -> None:  # type: ignore[no-untyped-def]
    if case.expect_allow:
        ensure_read_only(case.sql)  # must not raise
    else:
        with pytest.raises(UnsafeQueryError):
            ensure_read_only(case.sql)


def test_ensure_read_only_dollar_quote_apostrophe_bypass_is_fixed() -> None:
    # Was a CRITICAL real bypass under the old regex guard (single-quote
    # blanking mis-paired apostrophes living inside unrelated `$$...$$`
    # dollar-quoted strings, hiding a genuine 3-statement injection). The
    # real-grammar parser sees this as three statements and rejects it.
    sql = "SELECT $$it's$$ ; DROP TABLE t ; SELECT $$won't$$"
    with pytest.raises(UnsafeQueryError, match="single SQL statement"):
        ensure_read_only(sql)


def test_ensure_read_only_keyword_alias_is_allowed() -> None:
    # Was a false positive under the old regex keyword denylist (no
    # syntactic position awareness). The parse tree has no COPY statement
    # node here, only a column alias, so this is correctly allowed.
    ensure_read_only("SELECT id AS copy FROM t")


def test_ensure_read_only_e_string_escape_is_allowed() -> None:
    # Was a false positive under the old regex guard (backslash-quote
    # escapes inside a Postgres E'' string were not understood by the
    # blanking regex). The real grammar parses this as one legitimate
    # single-statement SELECT.
    ensure_read_only("SELECT E'\\'; DROP TABLE t; --'")


# --------------------------------------------------------------------------
# count_placeholders
# --------------------------------------------------------------------------


def test_count_placeholders_in_like_literal_not_counted() -> None:
    assert count_placeholders("SELECT * FROM t WHERE x LIKE '%s%'") == 0


def test_count_placeholders_doubled_percent_outside_literal() -> None:
    assert count_placeholders("SELECT %%s") == 1  # %%s contains a "%s" substring


def test_count_placeholders_real_bind_marker() -> None:
    assert count_placeholders("SELECT * FROM t WHERE id = %s AND name = %s") == 2


def test_count_placeholders_inside_dollar_quote_is_blanked() -> None:
    # Was a defect: dollar-quoted content was not recognised as a string
    # literal, so a literal "%s" inside `$$ %s $$` was miscounted as a real
    # bind placeholder. `_blank_string_literals` now blanks dollar-quotes
    # first, so it is correctly not counted.
    assert count_placeholders("SELECT $$ %s $$") == 0


# --------------------------------------------------------------------------
# check_bindable / filter_examples
# --------------------------------------------------------------------------

_EXAMPLE_NO_SLOTS = NlpExample(1, "q1", "SELECT 1", slots=())
_EXAMPLE_SESSION_SLOT = NlpExample(
    2, "q2", "SELECT 1", slots=(SlotSpec("session_id", "session_id"),), tags=("session_scoped",)
)
_EXAMPLE_ALL_SLOTS = NlpExample(
    3,
    "q3",
    "SELECT 1",
    slots=tuple(SlotSpec(t, t) for t in ("session_id", "since", "project", "model", "limit")),
)


@pytest.mark.parametrize(
    ("example", "slots", "expected_unbindable"),
    [
        (_EXAMPLE_NO_SLOTS, {}, []),
        (_EXAMPLE_NO_SLOTS, {"since": "x"}, ["since"]),
        (_EXAMPLE_SESSION_SLOT, {"session_id": "a"}, []),
        (_EXAMPLE_SESSION_SLOT, {"session_id": "a", "limit": 5}, ["limit"]),
        (_EXAMPLE_ALL_SLOTS, {"since": "x", "limit": 1}, []),
    ],
)
def test_check_bindable_generated_examples(
    example: NlpExample, slots: dict[str, object], expected_unbindable: list[str]
) -> None:
    assert sorted(check_bindable(example, slots)) == sorted(expected_unbindable)


def test_filter_examples_session_scoped_split() -> None:
    examples = [_EXAMPLE_NO_SLOTS, _EXAMPLE_SESSION_SLOT, _EXAMPLE_ALL_SLOTS]
    scoped = filter_examples(examples, session_scoped=True)
    unscoped = filter_examples(examples, session_scoped=False)
    assert scoped == [_EXAMPLE_SESSION_SLOT]
    assert unscoped == [_EXAMPLE_NO_SLOTS, _EXAMPLE_ALL_SLOTS]
    assert set(scoped) | set(unscoped) == set(examples)


def test_filter_examples_empty_list() -> None:
    assert filter_examples([], session_scoped=True) == []
    assert filter_examples([], session_scoped=False) == []


# --------------------------------------------------------------------------
# CatalogRetriever with FakeHashBackend
# --------------------------------------------------------------------------


def _make_examples(n: int) -> list[NlpExample]:
    return [NlpExample(i, f"question number {i} about sessions", f"SELECT {i}") for i in range(n)]


def test_catalog_retriever_empty_catalog_returns_empty() -> None:
    retriever = CatalogRetriever([], FakeHashBackend())
    assert retriever.search("anything") == []


def test_catalog_retriever_single_row() -> None:
    examples = _make_examples(1)
    retriever = CatalogRetriever(examples, FakeHashBackend())
    results = retriever.search("question number 0 about sessions")
    assert len(results) == 1
    assert results[0][0] is examples[0]


def test_catalog_retriever_top_k_larger_than_catalog() -> None:
    examples = _make_examples(3)
    retriever = CatalogRetriever(examples, FakeHashBackend())
    results = retriever.search("question number 1 about sessions", top_k=100)
    assert len(results) == 3


def test_catalog_retriever_top_k_one() -> None:
    examples = _make_examples(5)
    retriever = CatalogRetriever(examples, FakeHashBackend())
    results = retriever.search("question number 2 about sessions", top_k=1)
    assert len(results) == 1


def test_catalog_retriever_duplicate_identical_questions_tie_break_stable() -> None:
    examples = [
        NlpExample(1, "same question text", "SELECT 1"),
        NlpExample(2, "same question text", "SELECT 2"),
        NlpExample(3, "same question text", "SELECT 3"),
    ]
    retriever = CatalogRetriever(examples, FakeHashBackend())
    first = retriever.search("same question text", top_k=3)
    second = retriever.search("same question text", top_k=3)
    # Python's sort is stable, so ties (identical scores) preserve input order
    # across repeated runs against the same, deterministic backend.
    assert [ex.example_id for ex, _ in first] == [1, 2, 3]
    assert [ex.example_id for ex, _ in first] == [ex.example_id for ex, _ in second]


def test_catalog_retriever_very_long_question_does_not_raise() -> None:
    examples = _make_examples(3)
    retriever = CatalogRetriever(examples, FakeHashBackend())
    long_question = "sessions " * 2000
    results = retriever.search(long_question)
    assert len(results) == 3


def test_catalog_retriever_ranking_is_byte_identical_across_repeated_runs() -> None:
    examples = _make_examples(20)
    retriever = CatalogRetriever(examples, FakeHashBackend())
    baseline = retriever.search("question number 7 about sessions", top_k=20)
    for _ in range(10):
        again = retriever.search("question number 7 about sessions", top_k=20)
        assert again == baseline


def test_catalog_retriever_is_confident_threshold() -> None:
    retriever = CatalogRetriever(_make_examples(1), FakeHashBackend())
    assert retriever.is_confident(0.5) is True
    assert retriever.is_confident(0.49999) is False
