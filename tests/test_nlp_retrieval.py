"""Hybrid retrieval: paraphrase variants, char-n-gram BM25, gap check, re-rank fallback."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from analyze_claude_sessions.nlp import (
    GAP_MARGIN,
    HYBRID_CONFIDENCE_THRESHOLD,
    CatalogRetriever,
    LexicalIndex,
    NlpExample,
    _catalog_cache_key,
    _correct_typos,
    apply_contrast_guard,
    load_examples,
)
from analyze_claude_sessions.nlp_stress import FakeHashBackend


def _ex(
    example_id: int, question: str, *paraphrases: str, description: str | None = None
) -> NlpExample:
    return NlpExample(
        example_id, question, "SELECT 1", description=description, paraphrases=paraphrases
    )


CATALOG = [
    _ex(1, "how many tokens were spent per model", "model by model token totals"),
    _ex(2, "how many sessions are there", "size of the session table"),
    _ex(3, "what is the average session duration"),
]


def test_row_scores_as_max_over_its_variants(tmp_path: Path) -> None:
    retriever = CatalogRetriever(CATALOG, FakeHashBackend(), cache_dir=tmp_path)
    ranked = retriever.search("size of the session table", top_k=3)
    assert ranked[0][0].example_id == 2
    assert ranked[0][1] > 0.99


def test_description_is_a_variant(tmp_path: Path) -> None:
    examples = [_ex(1, "alpha beta", description="gamma delta epsilon"), _ex(2, "zeta eta theta")]
    retriever = CatalogRetriever(examples, FakeHashBackend(), cache_dir=tmp_path)
    assert retriever.search("gamma delta epsilon", top_k=1)[0][0].example_id == 1


def test_char_ngram_bm25_ranks_typos_to_the_right_row() -> None:
    index = LexicalIndex([e.question for e in CATALOG])
    scores = index.scores("toatl tokns per modle")
    assert int(np.argmax(scores)) == 0
    assert scores[0] > scores[1] and scores[0] > scores[2]


def test_lexical_scores_are_bounded_and_low_for_unrelated_text() -> None:
    index = LexicalIndex([e.question for e in CATALOG])
    assert float(index.scores("how many tokens were spent per model").max()) <= 1.0
    assert float(index.scores("capital of france").max()) < 0.2


def test_hybrid_retriever_handles_typos(tmp_path: Path) -> None:
    retriever = CatalogRetriever(CATALOG, FakeHashBackend(), cache_dir=tmp_path, lexical_weight=0.7)
    assert retriever.search("how many sesions are there", top_k=1)[0][0].example_id == 2


def test_typo_correction_fixes_domain_words_only() -> None:
    vocab = {"sessions", "tokens", "per", "model"}
    assert _correct_typos("tokns per modle in lisbon", vocab) == "tokens per model in lisbon"
    assert _correct_typos("sesions", vocab) == "sessions"


def test_gap_check_refuses_near_ties(tmp_path: Path) -> None:
    retriever = CatalogRetriever(CATALOG, FakeHashBackend(), cache_dir=tmp_path, lexical_weight=0.7)
    a, b = CATALOG[0], CATALOG[1]
    hi = HYBRID_CONFIDENCE_THRESHOLD + 0.2
    assert retriever.accepts([(a, hi), (b, hi - GAP_MARGIN - 0.01)]) is True
    assert retriever.accepts([(a, hi), (b, hi - GAP_MARGIN / 2)]) is False
    assert retriever.accepts([(a, HYBRID_CONFIDENCE_THRESHOLD - 0.01), (b, 0.0)]) is False
    assert retriever.accepts([(a, hi)]) is True
    assert retriever.accepts([]) is False


class _BrokenReranker:
    def score(self, pairs: list[tuple[str, str]]) -> list[float]:
        raise OSError("model not cached and offline")


class _ReversingReranker:
    def score(self, pairs: list[tuple[str, str]]) -> list[float]:
        return [float(i) / len(pairs) for i in range(len(pairs))]


def test_missing_cross_encoder_falls_back_to_fused_scores_with_one_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plain = CatalogRetriever(CATALOG, FakeHashBackend(), cache_dir=tmp_path, lexical_weight=0.7)
    broken = CatalogRetriever(
        CATALOG,
        FakeHashBackend(),
        cache_dir=tmp_path,
        lexical_weight=0.7,
        reranker=_BrokenReranker(),
    )
    for _ in range(2):
        assert broken.search("how many sessions are there") == plain.search(
            "how many sessions are there"
        )
    assert broken.threshold == plain.threshold
    assert capsys.readouterr().err.count("re-ranker unavailable") == 1


def test_reranker_reorders_fused_top_hits(tmp_path: Path) -> None:
    retriever = CatalogRetriever(
        CATALOG, FakeHashBackend(), cache_dir=tmp_path, reranker=_ReversingReranker()
    )
    fused = CatalogRetriever(CATALOG, FakeHashBackend(), cache_dir=tmp_path).search("sessions")
    reranked = retriever.search("sessions")
    assert [e.example_id for e, _ in reranked] == [e.example_id for e, _ in fused][::-1]


def test_cache_key_changes_when_paraphrases_or_description_change() -> None:
    base = _catalog_cache_key("m", CATALOG)
    more = [*CATALOG[:2], _ex(3, "what is the average session duration", "mean runtime")]
    described = [*CATALOG[:2], _ex(3, "what is the average session duration", description="d")]
    assert len({base, _catalog_cache_key("m", more), _catalog_cache_key("m", described)}) == 3


def test_contrast_guard_demotes_the_opposite_row() -> None:
    top = _ex(1, "top 10 projects by tokens")
    other = _ex(2, "total tokens per project")
    ranked = apply_contrast_guard("fewest tokens per project", [(top, 0.9), (other, 0.8)])
    assert ranked[0][0] is other
    kept = apply_contrast_guard("top 5 projects by tokens", [(top, 0.9), (other, 0.8)])
    assert kept[0][0] is top


def test_contrast_guard_cost_vs_tokens_and_read_vs_write() -> None:
    tokens = _ex(1, "total tokens per day")
    cost = _ex(2, "what is the cost per day")
    by_money = apply_contrast_guard("what did I spend in dollars", [(tokens, 0.9), (cost, 0.8)])
    assert by_money[0][0] is cost
    read = _ex(1, "what is the overall cache read percentage")
    write = _ex(2, "what is the overall cache write percentage")
    by_write = apply_contrast_guard("cache write percent", [(read, 0.9), (write, 0.8)])
    assert by_write[0][0] is write
    # both sides asked: left alone
    both = apply_contrast_guard("what does a day cost me in tokens", [(tokens, 0.9), (cost, 0.8)])
    assert both[0][0] is tokens


def test_load_examples_reads_paraphrases() -> None:
    rows = [{"example_id": 1, "question": "q", "sql_text": "s", "paraphrases": ["a", "b"]}]
    assert load_examples(rows)[0].paraphrases == ("a", "b")
    assert load_examples([{"example_id": 2, "question": "q", "sql_text": "s"}])[0].paraphrases == ()
