"""Retrieval eval harness for `CatalogRetriever` against `tests/data/nlp_eval.yaml`.

Opt-in and marked `slow`: it loads the real SentenceTransformer model (no
network once cached, but a real model load), so it must not run in the
default `uv run pytest` path. Run explicitly with:

    uv run pytest -m slow tests/test_nlp_eval.py -s

`-s` shows the printed recall@1 / recall@3 / false-accept summary; keep the
current run's numbers as the baseline for comparing future retrieval changes
against (see plan-nlp-slot-aware-matching.md).

The `near_miss` section of the eval YAML is not read here; it is consumed by
`scripts/rag_stress.py`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from analyze_claude_sessions.nlp import CatalogRetriever, LocalEmbeddingBackend, NlpExample

pytestmark = pytest.mark.slow

EVAL_PATH = Path(__file__).parent / "data" / "nlp_eval.yaml"


def _load_eval_data() -> dict[str, Any]:
    with EVAL_PATH.open() as f:
        data: dict[str, Any] = yaml.safe_load(f)
    return data


def _build_catalog_examples(questions: list[str]) -> list[NlpExample]:
    return [
        NlpExample(example_id=i, question=q, sql_text="SELECT 1")
        for i, q in enumerate(questions, start=1)
    ]


def test_retrieval_recall_and_false_accept() -> None:
    data = _load_eval_data()
    catalog_questions = list(data["catalog"].keys())
    out_of_catalog = data["out_of_catalog"]

    examples = _build_catalog_examples(catalog_questions)
    retriever = CatalogRetriever(examples, LocalEmbeddingBackend())

    total_paraphrases = 0
    hits_at_1 = 0
    hits_at_3 = 0
    for catalog_question, entry in data["catalog"].items():
        for paraphrase in entry["paraphrases"]:
            total_paraphrases += 1
            ranked = retriever.search(paraphrase, top_k=3)
            ranked_questions = [example.question for example, _score in ranked]
            if ranked_questions and ranked_questions[0] == catalog_question:
                hits_at_1 += 1
            if catalog_question in ranked_questions:
                hits_at_3 += 1

    false_accepts = 0
    for question in out_of_catalog:
        ranked = retriever.search(question, top_k=1)
        if ranked and retriever.is_confident(ranked[0][1]):
            false_accepts += 1

    recall_at_1 = hits_at_1 / total_paraphrases
    recall_at_3 = hits_at_3 / total_paraphrases
    false_accept_rate = false_accepts / len(out_of_catalog)

    print(
        f"\nrecall@1={recall_at_1:.3f} ({hits_at_1}/{total_paraphrases})  "
        f"recall@3={recall_at_3:.3f} ({hits_at_3}/{total_paraphrases})  "
        f"false_accept_rate={false_accept_rate:.3f} ({false_accepts}/{len(out_of_catalog)})"
    )

    # Sanity floors, not the point of this test -- the point is the printed
    # numbers, tracked as a baseline across retrieval changes. See the plan's
    # acceptance criteria: recall@1 must not regress below the recorded
    # baseline (compare the printed number here against the PR description).
    assert recall_at_1 > 0.5
    assert recall_at_3 > 0.5
