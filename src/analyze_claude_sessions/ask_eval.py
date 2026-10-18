"""End-to-end eval of the `acp ask` decision: slots -> retrieval -> refusal.

A case is a question plus what `resolve_ask` should do with it: answer with one
named catalog row (optionally with specific bound filter values) or refuse.
Cases live in two frozen-by-convention splits under `tests/data/ask_eval/`:
`tune.yaml` is the only set a threshold may be chosen on; `test.yaml` is held
out and only scored, never tuned against.

Pure and model-agnostic: callers pass the catalog rows and a retriever factory
(real `LocalEmbeddingBackend` in `scripts/ask_eval.py`, a fake in unit tests).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml

from .db import connect, fetch_all
from .nlp import (
    LEXICAL_WEIGHT,
    AskDecision,
    Backend,
    CatalogRetriever,
    NlpExample,
    Reranker,
    UnboundFilterError,
    UnsafeQueryError,
    hybrid_retriever_factory,
    load_examples,
    resolve_ask,
)

Kind = Literal["paraphrase", "near_miss", "opposite", "ood", "typo", "slot", "unbindable"]
Verdict = Literal["correct", "correct_refusal", "wrong_accept", "over_refusal", "wrong_params"]

KINDS: tuple[str, ...] = (
    "paraphrase",
    "near_miss",
    "opposite",
    "ood",
    "typo",
    "slot",
    "unbindable",
)
VERDICTS: tuple[str, ...] = (
    "correct",
    "correct_refusal",
    "wrong_accept",
    "over_refusal",
    "wrong_params",
)
REFUSE = "refuse"

EVAL_DIR = Path(__file__).resolve().parents[2] / "tests" / "data" / "ask_eval"
TUNE_PATH = EVAL_DIR / "tune.yaml"
TEST_PATH = EVAL_DIR / "test.yaml"
BASELINE_PATH = EVAL_DIR / "baseline.json"
SQL_DIR = Path(__file__).resolve().parents[2] / "sql"

# The eval pins everything `acp ask` reads from the clock or the database.
EVAL_TODAY = date(2026, 10, 18)
EVAL_PROJECTS: tuple[str, ...] = ("billing-api", "dashboard")
EVAL_MODELS: tuple[str, ...] = ("claude-sonnet-5-5", "claude-opus-5-5")
EVAL_TOP_K = 3

# Matches `(\n    'question',` rows of `INSERT INTO claude.nlp_examples ... VALUES`.
_CATALOG_QUESTION_RE = re.compile(r"^\(\n    '([^']+)',\n", re.MULTILINE)
_CATALOG_SQL_FILES = (
    "005_nlp_catalog.sql",
    "018_nlp_optional_filters_and_cost.sql",
    "022_nlp_near_miss_rows.sql",
)
_TOKEN_RE = re.compile(r"[a-z0-9_]+")


@dataclass(frozen=True)
class EvalCase:
    """One labelled question.

    `expect` is an exact catalog `question` or `REFUSE`. `expect_params`, when
    set, lists the non-null filter values the accepted row must be bound with
    (ISO strings for dates, ints for limits); order and duplicates are ignored.
    """

    question: str
    expect: str
    kind: Kind
    expect_params: tuple[str | int, ...] | None = None
    note: str = ""


@dataclass(frozen=True)
class Scored:
    case: EvalCase
    verdict: Verdict
    decision: AskDecision


def load_cases(path: Path) -> list[EvalCase]:
    """Read a split file (a YAML list of case mappings), validating each case."""
    with path.open() as handle:
        raw: list[dict[str, Any]] = yaml.safe_load(handle) or []
    cases = []
    for item in raw:
        kind = item["kind"]
        if kind not in KINDS:
            raise ValueError(f"{path.name}: unknown kind {kind!r} for {item['question']!r}")
        params = item.get("expect_params")
        cases.append(
            EvalCase(
                question=str(item["question"]),
                expect=str(item["expect"]),
                kind=kind,
                expect_params=None if params is None else tuple(params),
                note=str(item.get("note", "")),
            )
        )
    return cases


def load_tune_cases() -> list[EvalCase]:
    """The only loader tuning uses; it never reads the held-out test split."""
    return load_cases(TUNE_PATH)


def catalog_questions_from_sql(sql_dir: Path = SQL_DIR) -> set[str]:
    """Catalog `question` values seeded by migrations 005, 018 and 022 (no database)."""
    questions: set[str] = set()
    for name in _CATALOG_SQL_FILES:
        questions.update(_CATALOG_QUESTION_RE.findall((sql_dir / name).read_text()))
    return questions


def fetch_catalog(database_url: str) -> list[NlpExample]:
    """The real `claude.nlp_examples` rows (slots and SQL included), as `acp ask` reads them."""
    with connect(database_url) as conn:
        rows = fetch_all(
            conn,
            "SELECT example_id, question, sql_text, description, tags, slots, paraphrases "
            "FROM claude.nlp_examples",
        )
    return load_examples(rows)


def catalog_hash(examples: Iterable[NlpExample]) -> str:
    """Stable fingerprint of what the catalog can answer (question, SQL, slots)."""
    rows = sorted(
        (
            e.question,
            e.sql_text,
            [(s.name, s.type, s.optional) for s in e.slots],
        )
        for e in examples
    )
    return hashlib.sha256(json.dumps(rows, default=str).encode()).hexdigest()[:16]


def token_jaccard(a: str, b: str) -> float:
    """Jaccard similarity of lowercase word-token sets (the paraphrase leak guard)."""
    ta, tb = set(_TOKEN_RE.findall(a.lower())), set(_TOKEN_RE.findall(b.lower()))
    if not ta and not tb:
        return 1.0
    return len(ta & tb) / len(ta | tb)


def bound_values(params: Sequence[Any]) -> set[str]:
    """Non-null bound params as strings, for order-insensitive comparison."""
    return {str(p) for p in params if p is not None}


def score_case(decision: AskDecision, case: EvalCase) -> Verdict:
    """Classify *decision* against *case*.

    A refusal is any non-`accept` outcome. `wrong_accept` covers both an
    answered refusal case and an answer from the wrong catalog row;
    `wrong_params` is the right row with the wrong bound filter values.
    """
    accepted = decision.outcome == "accept"
    if case.expect == REFUSE:
        return "wrong_accept" if accepted else "correct_refusal"
    if not accepted:
        return "over_refusal"
    if decision.example is None or decision.example.question != case.expect:
        return "wrong_accept"
    if case.expect_params is not None and bound_values(decision.params) != {
        str(p) for p in case.expect_params
    }:
        return "wrong_params"
    return "correct"


RetrieverFactory = Callable[[list[NlpExample]], CatalogRetriever]


def decide(
    question: str,
    examples: list[NlpExample],
    retriever_factory: RetrieverFactory,
    *,
    top_k: int = EVAL_TOP_K,
) -> AskDecision:
    """`resolve_ask` with the eval's pinned clock/projects/models.

    The two errors `resolve_ask` raises for a bad catalog binding are refusals
    here, as they are in the CLI (nothing is executed).
    """
    try:
        return resolve_ask(
            question,
            examples,
            retriever_factory,
            known_projects=list(EVAL_PROJECTS),
            known_models=list(EVAL_MODELS),
            top_k=top_k,
            today=EVAL_TODAY,
        )
    except (UnboundFilterError, UnsafeQueryError) as exc:
        return AskDecision("unbindable", unbindable=(str(exc),))


def run_cases(
    cases: Sequence[EvalCase],
    examples: list[NlpExample],
    retriever_factory: RetrieverFactory,
) -> list[Scored]:
    """Decide and score every case through the real `resolve_ask`."""
    return [
        Scored(case, score_case(d := decide(case.question, examples, retriever_factory), case), d)
        for case in cases
    ]


def default_retriever_factory(
    backend: Backend,
    *,
    rerank: bool = False,
    lexical_weight: float = LEXICAL_WEIGHT,
    contrast_guard: bool = True,
) -> RetrieverFactory:
    return hybrid_retriever_factory(
        backend, rerank=rerank, lexical_weight=lexical_weight, contrast_guard=contrast_guard
    )


# --- metrics ---------------------------------------------------------------


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a proportion; (0, 1) when there are no trials."""
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


@dataclass(frozen=True)
class Metrics:
    n: int
    accuracy: float
    wrong_accept: float
    over_refusal: float
    recall_at_1: float | None
    recall_at_3: float | None
    accuracy_ci: tuple[float, float] = field(default=(0.0, 1.0))
    wrong_accept_ci: tuple[float, float] = field(default=(0.0, 1.0))
    verdicts: dict[str, int] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "accuracy": round(self.accuracy, 4),
            "wrong_accept": round(self.wrong_accept, 4),
            "over_refusal": round(self.over_refusal, 4),
            "recall_at_1": None if self.recall_at_1 is None else round(self.recall_at_1, 4),
            "recall_at_3": None if self.recall_at_3 is None else round(self.recall_at_3, 4),
            "verdicts": self.verdicts,
        }


def compute_metrics(scored: Sequence[Scored]) -> Metrics:
    """Rates are over every case in the group; recall is over answerable cases only."""
    n = len(scored)
    counts: Counter[str] = Counter(s.verdict for s in scored)
    correct = counts["correct"] + counts["correct_refusal"]
    answerable = [s for s in scored if s.case.expect != REFUSE]
    hits1 = sum(
        1
        for s in answerable
        if s.decision.ranked and s.decision.ranked[0][0].question == s.case.expect
    )
    hits3 = sum(
        1
        for s in answerable
        if any(ex.question == s.case.expect for ex, _score in s.decision.ranked[:3])
    )
    return Metrics(
        n=n,
        accuracy=correct / n if n else 0.0,
        wrong_accept=counts["wrong_accept"] / n if n else 0.0,
        over_refusal=counts["over_refusal"] / n if n else 0.0,
        recall_at_1=hits1 / len(answerable) if answerable else None,
        recall_at_3=hits3 / len(answerable) if answerable else None,
        accuracy_ci=wilson_interval(correct, n),
        wrong_accept_ci=wilson_interval(counts["wrong_accept"], n),
        verdicts={v: counts[v] for v in VERDICTS if counts[v]},
    )


def metrics_by_kind(scored: Sequence[Scored]) -> dict[str, Metrics]:
    """`all` plus one entry per kind present in *scored*."""
    out = {"all": compute_metrics(scored)}
    for kind in KINDS:
        group = [s for s in scored if s.case.kind == kind]
        if group:
            out[kind] = compute_metrics(group)
    return out


def format_report(split: str, by_kind: dict[str, Metrics]) -> str:
    """Plain-text per-kind table for one split."""
    header = (
        f"{split:<11} {'n':>4} {'acc':>6} {'acc 95% CI':>13} {'wrong-acc':>9} "
        f"{'over-ref':>8} {'R@1':>6} {'R@3':>6}"
    )

    def pct(x: float | None) -> str:
        return "   n/a" if x is None else f"{x:6.3f}"

    lines = [header, "-" * len(header)]
    for kind, m in by_kind.items():
        lo, hi = m.accuracy_ci
        lines.append(
            f"{kind:<11} {m.n:>4} {m.accuracy:6.3f} {f'[{lo:.2f},{hi:.2f}]':>13} "
            f"{m.wrong_accept:9.3f} {m.over_refusal:8.3f} {pct(m.recall_at_1)} {pct(m.recall_at_3)}"
        )
    return "\n".join(lines)


def outcome_counts(scored: Sequence[Scored]) -> dict[str, int]:
    """Count of `AskDecision.outcome` values, for the stress report."""
    counts = Counter(s.decision.outcome for s in scored)
    return {k: counts[k] for k in ("accept", "low_confidence", "unbindable", "no_examples")}


# --- baseline --------------------------------------------------------------


def build_baseline(
    *, model_name: str, examples: list[NlpExample], threshold: float, test: Sequence[Scored]
) -> dict[str, Any]:
    return {
        "model_name": model_name,
        "catalog_hash": catalog_hash(examples),
        "threshold": threshold,
        "metrics": {"test": {k: m.to_json() for k, m in metrics_by_kind(test).items()}},
    }


def load_baseline(path: Path = BASELINE_PATH) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def baseline_mismatch(baseline: dict[str, Any], *, model_name: str, examples_hash: str) -> str:
    """A re-baseline message when the baseline was recorded on another model or catalog."""
    if baseline["model_name"] != model_name:
        return (
            f"baseline was recorded with model {baseline['model_name']!r}, running "
            f"{model_name!r}: re-baseline with `make ask-eval ARGS=--update-baseline`"
        )
    if baseline["catalog_hash"] != examples_hash:
        return (
            "the catalog changed since the baseline was recorded "
            f"({baseline['catalog_hash']} -> {examples_hash}): "
            "re-baseline with `make ask-eval ARGS=--update-baseline`"
        )
    return ""


# --- tuning ----------------------------------------------------------------


class _FloorRetriever(CatalogRetriever):
    """Confident about everything, so one run exposes every candidate's raw score."""

    def is_confident(self, score: float) -> bool:
        return True

    def accepts(self, ranked: Sequence[tuple[NlpExample, float]]) -> bool:
        return True


def redecide(floor: AskDecision, threshold: float, gap_margin: float = 0.0) -> AskDecision:
    """What *floor* (a decision made with no threshold) becomes at *threshold* and *gap_margin*."""
    gap = floor.ranked[0][1] - floor.ranked[1][1] if len(floor.ranked) > 1 else math.inf
    if floor.outcome in ("accept", "unbindable") and (floor.score < threshold or gap < gap_margin):
        return AskDecision(
            "low_confidence",
            floor.example,
            floor.score,
            floor.ranked,
            floor.slots,
            floor.clean_question,
        )
    return floor


@dataclass(frozen=True)
class SweepRow:
    threshold: float
    gap_margin: float
    accuracy: float
    wrong_accept: float
    over_refusal: float


def tune(
    examples: list[NlpExample],
    backend: Backend,
    thresholds: Sequence[float],
    *,
    cases: Sequence[EvalCase] | None = None,
    max_wrong_accept: float = 0.05,
    cache_dir: Path | None = None,
    gap_margins: Sequence[float] = (0.0,),
    reranker: Reranker | None = None,
    lexical_weight: float = LEXICAL_WEIGHT,
    contrast_guard: bool = True,
) -> tuple[list[SweepRow], tuple[float, float] | None]:
    """Sweep the confidence threshold on the tune split and pick one.

    Reads only `tune.yaml` (via `load_tune_cases`). Retrieval runs once per case
    at a floor threshold; each candidate threshold is then applied to that
    decision. The pick is the threshold with the best accuracy among those whose
    wrong-accept rate is <= *max_wrong_accept* (ties go to the higher threshold);
    `None` when no candidate qualifies. Never writes code: the caller prints it.
    """
    tune_cases = list(cases) if cases is not None else load_tune_cases()

    def floor_factory(scoped: list[NlpExample]) -> CatalogRetriever:
        return _FloorRetriever(
            scoped,
            backend,
            cache_dir=cache_dir,
            reranker=reranker,
            lexical_weight=lexical_weight,
            contrast_guard=contrast_guard,
        )

    floors = [(case, decide(case.question, examples, floor_factory)) for case in tune_cases]
    rows: list[SweepRow] = []
    for threshold in thresholds:
        for gap_margin in gap_margins:
            scored = [
                Scored(case, score_case(d := redecide(floor, threshold, gap_margin), case), d)
                for case, floor in floors
            ]
            m = compute_metrics(scored)
            rows.append(SweepRow(threshold, gap_margin, m.accuracy, m.wrong_accept, m.over_refusal))
    eligible = [r for r in rows if r.wrong_accept <= max_wrong_accept]
    best = max(eligible, key=lambda r: (r.accuracy, r.threshold, r.gap_margin), default=None)
    return rows, None if best is None else (best.threshold, best.gap_margin)
