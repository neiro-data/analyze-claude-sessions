"""End-to-end `ask` eval against the live catalog and `tests/data/ask_eval/baseline.json`.

Opt-in (`slow and live_db`): loads the real SentenceTransformer model and reads the real
`claude.nlp_examples` rows, so run `make up && make init-db` first, then:

    uv run --group nlp pytest -m "slow and live_db" tests/test_nlp_eval.py -s

`-s` prints the per-kind tables for both splits. The assertions use the held-out test
split only; the tune split is for picking thresholds (`make ask-eval ARGS=--tune`).
A baseline recorded on another model or catalog fails with a re-baseline message rather
than passing silently.
"""

from __future__ import annotations

import pytest

from analyze_claude_sessions import nlp
from analyze_claude_sessions.ask_eval import (
    TEST_PATH,
    TUNE_PATH,
    Scored,
    baseline_mismatch,
    catalog_hash,
    default_retriever_factory,
    fetch_catalog,
    format_report,
    load_baseline,
    load_cases,
    metrics_by_kind,
    run_cases,
)
from analyze_claude_sessions.config import get_settings
from analyze_claude_sessions.nlp import LocalEmbeddingBackend, NlpExample

pytestmark = [pytest.mark.slow, pytest.mark.live_db]

ACCURACY_TOLERANCE = 0.02
WRONG_ACCEPT_TOLERANCE = 0.02


@pytest.fixture(scope="module")
def examples() -> list[NlpExample]:
    return fetch_catalog(get_settings().database_url)


@pytest.fixture(scope="module")
def backend() -> LocalEmbeddingBackend:
    return LocalEmbeddingBackend()


@pytest.fixture(scope="module")
def test_scored(examples: list[NlpExample], backend: LocalEmbeddingBackend) -> list[Scored]:
    scored = run_cases(load_cases(TEST_PATH), examples, default_retriever_factory(backend))
    tune_scored = run_cases(load_cases(TUNE_PATH), examples, default_retriever_factory(backend))
    print("\n" + format_report("tune", metrics_by_kind(tune_scored)))
    print("\n" + format_report("test", metrics_by_kind(scored)))
    return scored


def test_baseline_matches_model_and_catalog(
    examples: list[NlpExample], backend: LocalEmbeddingBackend
) -> None:
    message = baseline_mismatch(
        load_baseline(), model_name=backend.model_name, examples_hash=catalog_hash(examples)
    )
    assert not message, message


def test_threshold_matches_baseline() -> None:
    assert load_baseline()["threshold"] == nlp.CONFIDENCE_THRESHOLD, (
        "CONFIDENCE_THRESHOLD changed: re-baseline with `make ask-eval ARGS=--update-baseline`"
    )


def test_test_split_does_not_regress(test_scored: list[Scored]) -> None:
    baseline = load_baseline()["metrics"]["test"]["all"]
    current = metrics_by_kind(test_scored)["all"]
    assert current.accuracy >= baseline["accuracy"] - ACCURACY_TOLERANCE, (
        f"accuracy {current.accuracy:.3f} < baseline {baseline['accuracy']:.3f} - "
        f"{ACCURACY_TOLERANCE}"
    )
    assert current.wrong_accept <= baseline["wrong_accept"] + WRONG_ACCEPT_TOLERANCE, (
        f"wrong-accept {current.wrong_accept:.3f} > baseline {baseline['wrong_accept']:.3f} + "
        f"{WRONG_ACCEPT_TOLERANCE}"
    )
