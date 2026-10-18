"""Slow, real-model ask stress suite for `CatalogRetriever`.

Opt-in, marked `slow`: loads the real SentenceTransformer model (no network
once cached, but a real model load/embed), so it must never run in the
default `uv run pytest` path. Run explicitly with:

    uv run --group nlp pytest -m slow tests/test_nlp_stress_slow.py -s

Reuses the shared, deterministic perturbation/backend/corpus helpers from
`analyze_claude_sessions.nlp_stress` -- see that module's docstring. This
file adds the real-model measurements: perturbation robustness, a
confidence-threshold sweep, scale/latency, and determinism checks. See also
`scripts/ask_stress.py`, which runs the same battery and writes a report.
"""

from __future__ import annotations

import random
import time
from pathlib import Path
from typing import Any

import pytest

from analyze_claude_sessions.nlp import CatalogRetriever, LocalEmbeddingBackend
from analyze_claude_sessions.nlp_stress import (
    CONFIDENCE_SWEEP,
    PERTURBATIONS,
    FakeHashBackend,
    build_catalog_examples,
    load_yaml,
    near_miss_cases,
    operating_point,
    paraphrase_pairs,
    percentiles,
    score_pairs,
    synthetic_catalog,
)

pytestmark = pytest.mark.slow

EVAL_PATH = Path(__file__).parent / "data" / "nlp_eval.yaml"
STRESS_PATH = Path(__file__).parent / "data" / "nlp_stress.yaml"

SEED = 20260829


@pytest.fixture(scope="module")
def eval_data() -> dict[str, Any]:
    return load_yaml(EVAL_PATH)


@pytest.fixture(scope="module")
def stress_data() -> dict[str, Any]:
    return load_yaml(STRESS_PATH)


@pytest.fixture(scope="module")
def retriever(eval_data: dict[str, Any]) -> CatalogRetriever:
    catalog_questions = list(eval_data["catalog"].keys())
    examples = build_catalog_examples(catalog_questions)
    return CatalogRetriever(examples, LocalEmbeddingBackend())


# --------------------------------------------------------------------------
# Perturbation robustness with the real model
# --------------------------------------------------------------------------


# Perturbation classes that preserve meaning and should barely move recall.
_LOSSLESS_CLASSES = frozenset(
    {
        "casing_upper",
        "casing_lower",
        "casing_title",
        "punctuation_strip",
        "punctuation_add",
        "whitespace_unicode_noise",
    }
)


def test_perturbation_robustness(retriever: CatalogRetriever, eval_data: dict[str, Any]) -> None:
    pairs = paraphrase_pairs(eval_data)
    baseline_r1, baseline_r3, baseline_score = score_pairs(retriever, pairs)
    print(
        f"\nbaseline: recall@1={baseline_r1:.3f} recall@3={baseline_r3:.3f} "
        f"mean_top1_score={baseline_score:.3f} (n={len(pairs)})"
    )
    # Measured 2026-08: recall@1=0.923, recall@3=1.000. Guard with headroom.
    assert baseline_r1 >= 0.85
    assert baseline_r3 >= 0.90

    results: dict[str, tuple[float, float, float]] = {}
    for name, perturbation in PERTURBATIONS.items():
        rng = random.Random(SEED)  # noqa: S311 - deterministic, not crypto
        perturbed_pairs = [
            (catalog_question, perturbation(query, rng)) for catalog_question, query in pairs
        ]
        r1, r3, mean_score = score_pairs(retriever, perturbed_pairs)
        results[name] = (r1, r3, mean_score)
        print(
            f"{name:26s} recall@1={r1:.3f} (Δ{r1 - baseline_r1:+.3f})  "
            f"recall@3={r3:.3f} (Δ{r3 - baseline_r3:+.3f})  "
            f"mean_score={mean_score:.3f} (Δ{mean_score - baseline_score:+.3f})"
        )
        # Real regression guards, floors derived from measured 2026-08 numbers
        # with generous headroom (worst observed class was truncate_60_percent
        # at 0.449, i.e. baseline - 0.474).
        assert r1 >= baseline_r1 - 0.60, f"{name} collapsed: recall@1={r1:.3f}"
        assert r3 >= r1
        if name in _LOSSLESS_CLASSES:
            # These perturbations preserve meaning; the encoder should be
            # essentially indifferent to them (measured delta was 0.000).
            assert r1 >= baseline_r1 - 0.10, f"{name} should be near-lossless: {r1:.3f}"


@pytest.mark.parametrize("rate", [0.05, 0.10])
def test_typo_rate_sweep(
    retriever: CatalogRetriever, eval_data: dict[str, Any], rate: float
) -> None:
    """typo_adjacent_swap at ~5% and ~10% per-char rates."""
    from analyze_claude_sessions.nlp_stress import typo_adjacent_swap

    pairs = paraphrase_pairs(eval_data)
    rng = random.Random(SEED)  # noqa: S311
    perturbed_pairs = [
        (catalog_question, typo_adjacent_swap(query, rng, rate=rate))
        for catalog_question, query in pairs
    ]
    r1, r3, mean_score = score_pairs(retriever, perturbed_pairs)
    print(f"\ntypo_adjacent_swap rate={rate}: recall@1={r1:.3f} recall@3={r3:.3f}")
    # Measured 2026-08: 0.718 @5%, 0.590 @10%.
    floor = 0.55 if rate <= 0.05 else 0.42
    assert r1 >= floor, f"typo rate={rate} recall@1={r1:.3f} below floor {floor}"
    assert mean_score > 0.0


# --------------------------------------------------------------------------
# Confidence threshold sweep
# --------------------------------------------------------------------------


def test_confidence_threshold_sweep(
    retriever: CatalogRetriever, eval_data: dict[str, Any], stress_data: dict[str, Any]
) -> None:
    pairs = paraphrase_pairs(eval_data)
    out_of_catalog = [item["question"] for item in stress_data["out_of_catalog"]]

    in_catalog_scores = []
    for catalog_question, query in pairs:
        ranked = retriever.search(query, top_k=1)
        if ranked:
            correct = ranked[0][0].question == catalog_question
            in_catalog_scores.append((ranked[0][1], correct))

    out_of_catalog_scores = []
    for question in out_of_catalog:
        ranked = retriever.search(question, top_k=1)
        out_of_catalog_scores.append(ranked[0][1] if ranked else -1.0)

    print("\nthreshold  accept_rate(in-catalog, correct-only)  false_accept_rate(out-of-catalog)")
    default_row: tuple[float, float, float] | None = None
    for threshold in CONFIDENCE_SWEEP:
        accepted_correct = sum(
            1 for score, correct in in_catalog_scores if score >= threshold and correct
        )
        accept_rate = accepted_correct / len(in_catalog_scores)
        false_accepts = sum(1 for score in out_of_catalog_scores if score >= threshold)
        false_accept_rate = false_accepts / len(out_of_catalog_scores)
        print(
            f"{threshold:.2f}       {accept_rate:.3f}"
            f"                              {false_accept_rate:.3f}"
        )
        if threshold == 0.5:
            default_row = (threshold, accept_rate, false_accept_rate)

    assert default_row is not None
    # Measured 2026-08 at threshold 0.50: accept_rate=0.910, false_accept=0.091.
    assert default_row[1] >= 0.80, f"accept_rate at 0.5 regressed: {default_row[1]:.3f}"
    assert default_row[2] <= 0.35, f"false_accept_rate at 0.5 regressed: {default_row[2]:.3f}"


def test_false_accept_rate_at_default_threshold(
    retriever: CatalogRetriever, stress_data: dict[str, Any]
) -> None:
    out_of_catalog = [item["question"] for item in stress_data["out_of_catalog"]]
    false_accepts = 0
    for question in out_of_catalog:
        ranked = retriever.search(question, top_k=1)
        if ranked and retriever.is_confident(ranked[0][1]):
            false_accepts += 1
    rate = false_accepts / len(out_of_catalog)
    print(f"\nfalse_accept_rate@default={rate:.3f} ({false_accepts}/{len(out_of_catalog)})")
    # Measured 2026-08: 0.091 (1/11). Guard with headroom.
    assert rate <= 0.35, f"false-accept rate regressed to {rate:.3f}"


# --------------------------------------------------------------------------
# Scale / latency
# --------------------------------------------------------------------------


@pytest.mark.parametrize("size", [100, 1_000, 10_000])
def test_scale_latency_fake_backend(size: int) -> None:
    questions = synthetic_catalog(size, SEED)
    examples = build_catalog_examples(questions)

    build_start = time.perf_counter()
    retriever = CatalogRetriever(examples, FakeHashBackend())
    build_time = time.perf_counter() - build_start

    latencies = []
    rng = random.Random(SEED)  # noqa: S311
    for _ in range(30):
        query = rng.choice(questions)
        start = time.perf_counter()
        retriever.search(query, top_k=3)
        latencies.append(time.perf_counter() - start)
    p50, p95 = percentiles(latencies)
    print(
        f"\nsize={size:>6d} build={build_time:.4f}s "
        f"search_p50={p50 * 1000:.3f}ms p95={p95 * 1000:.3f}ms"
    )

    # Generous smoke guard, not a microbenchmark: build must complete well
    # within a bound that scales with size, and search must stay sub-second
    # even at 10k rows on a loaded laptop.
    assert build_time < max(5.0, size * 0.01)
    assert p95 < 1.0


def test_scale_latency_real_backend(retriever: CatalogRetriever) -> None:
    """Smaller (~1k) real-model run: build time and search p50/p95."""
    questions = synthetic_catalog(1_000, SEED)
    examples = build_catalog_examples(questions)

    build_start = time.perf_counter()
    real_retriever = CatalogRetriever(examples, LocalEmbeddingBackend())
    build_time = time.perf_counter() - build_start

    latencies = []
    rng = random.Random(SEED)  # noqa: S311
    for _ in range(15):
        query = rng.choice(questions)
        start = time.perf_counter()
        real_retriever.search(query, top_k=3)
        latencies.append(time.perf_counter() - start)
    p50, p95 = percentiles(latencies)
    print(
        f"\nreal-backend size=1000 build={build_time:.3f}s "
        f"search_p50={p50 * 1000:.3f}ms p95={p95 * 1000:.3f}ms"
    )
    assert build_time < 120.0
    assert p95 < 5.0


# --------------------------------------------------------------------------
# Real-backend determinism
# --------------------------------------------------------------------------


def test_real_backend_deterministic_ranking(retriever: CatalogRetriever) -> None:
    query = "how many sessions do we have"
    first = retriever.search(query, top_k=3)
    for _ in range(4):
        again = retriever.search(query, top_k=3)
        assert [q.example_id for q, _ in again] == [q.example_id for q, _ in first]
        assert [s for _, s in again] == [s for _, s in first]


def test_real_backend_batch_vs_single_encode_consistent() -> None:
    backend = LocalEmbeddingBackend()
    texts = ["how many sessions are there", "total number of projects", "count all transcripts"]
    batch = backend.encode(texts)
    singles = [backend.encode([t])[0] for t in texts]
    for batch_row, single_row in zip(batch, singles, strict=True):
        assert batch_row == pytest.approx(single_row, abs=1e-4)


# --------------------------------------------------------------------------
# Near-miss wrong-accept rate (report only, no quality gate)
# --------------------------------------------------------------------------


def test_near_miss_report(
    retriever: CatalogRetriever, eval_data: dict[str, Any], stress_data: dict[str, Any]
) -> None:
    """Prints the near-miss wrong-accept rate at 0.5 and at t*; asserts it computes only."""
    cases = near_miss_cases(eval_data)
    assert cases
    nm_scores = [retriever.search(c.question, top_k=1)[0][1] for c in cases]
    ooc_scores = [
        ranked[0][1] if (ranked := retriever.search(item["question"], top_k=1)) else -1.0
        for item in stress_data["out_of_catalog"]
    ]
    t_star = operating_point([], ooc_scores)
    rate_default = sum(1 for sc in nm_scores if retriever.is_confident(sc)) / len(nm_scores)
    rate_op = sum(1 for sc in nm_scores if sc >= t_star) / len(nm_scores)
    print(f"\nnear-miss wrong-accept: at 0.5={rate_default:.3f}  at t*={t_star:.3f}: {rate_op:.3f}")
    assert 0.0 <= rate_default <= 1.0
    assert 0.0 <= rate_op <= 1.0
