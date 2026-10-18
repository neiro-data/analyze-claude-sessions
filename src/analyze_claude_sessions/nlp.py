"""Offline NLP retrieval layer: match natural-language questions to curated SQL.

No network access or API key is required. Retrieval works by embedding every
variant of each `claude.nlp_examples` row (question, paraphrases, description)
with a local sentence-transformers model, fusing the cosine similarity with a
typo-tolerant character n-gram BM25 score, and optionally re-ranking the top
hits with a local cross-encoder. A row's score is the max over its variants.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import tempfile
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from difflib import get_close_matches
from pathlib import Path
from typing import Any, Literal, Protocol

import numpy as np
from numpy.typing import NDArray
from pglast import parse_sql
from pglast.parser import ParseError
from pglast.visitors import Visitor

from analyze_claude_sessions.config import get_settings

#: Minimum cosine similarity for a top hit to be considered a confident match.
CONFIDENCE_THRESHOLD = 0.5

#: Hybrid (cosine + BM25) scale: threshold and minimum top1 - top2 gap for a hit to be
#: accepted (near-ties are refused). Chosen on tune.yaml only (`ask_eval.py --tune
#: --max-wrong-accept 0.10`).
HYBRID_CONFIDENCE_THRESHOLD = 0.5
GAP_MARGIN = 0.03

#: Weight of the embedding cosine in the fused score; the rest is char-n-gram BM25.
#: `CatalogRetriever` itself defaults to 1.0 (cosine only); `hybrid_retriever_factory` uses this.
LEXICAL_WEIGHT = 0.7

#: Cross-encoder used to re-rank the fused top hits (already in the `nlp` group).
DEFAULT_RERANK_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"

#: How many fused hits the cross-encoder re-ranks.
RERANK_TOP_K = 5

#: Acceptance constants on the re-ranker's sigmoid score scale (separate from the fused ones).
#: Opt-in only: on tune.yaml the ms-marco cross-encoder was worse than the fused scores
#: (no threshold met the 5% wrong-accept ceiling), so these are not tuned to a pick.
RERANK_CONFIDENCE_THRESHOLD = 0.6
RERANK_GAP_MARGIN = 0.05

#: Tag marking a catalog example as scoped to a single session_id.
SESSION_SCOPED_TAG = "session_scoped"

# Session ids are word characters and hyphens (e.g. a UUID). Restricting the
# capture to that set stops trailing sentence punctuation (`,` `.` `?`) from
# being swallowed into the id, which would otherwise fail the uuid cast.
_SESSION_ID_RE = re.compile(r"session_id\s*=([\w-]+)", re.IGNORECASE)

#: A bare pasted UUID, with no `session_id=` prefix required.
_BARE_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)

#: Explicit dates: `since 2026-01-31`, `since 2026-01` (day defaults to the 1st).
_SINCE_DATE_RE = re.compile(r"\bsince\s+(\d{4}-\d{2}(?:-\d{2})?)\b", re.IGNORECASE)

#: `last N days` / `last N day` -> a trailing rolling window.
_LAST_N_DAYS_RE = re.compile(r"\blast\s+(\d+)\s+days?\b", re.IGNORECASE)

#: `last month` -> the previous calendar month (see `_last_calendar_month_range`).
_LAST_MONTH_RE = re.compile(r"\blast\s+month\b", re.IGNORECASE)

#: `top N` -> a row-count limit.
_TOP_N_RE = re.compile(r"\btop\s+(\d+)\b", re.IGNORECASE)

#: Slot type names a catalog row may declare and `extract_slots` may emit.
#:
#: `until` is an exclusive end date, emitted only alongside `last month`; every
#: catalog row that declares `since` also declares `until`.
SLOT_TYPES = ("session_id", "since", "until", "project", "model", "limit")

#: Default local embedding model; small, CPU-friendly, good general recall.
DEFAULT_MODEL_NAME = "all-MiniLM-L6-v2"

#: Functions callable from a plain SELECT that have side effects or bypass
#: READ ONLY semantics (file/network access, DoS, session control). READ ONLY
#: transactions do not block these, so the guard must.
_FORBIDDEN_FUNCTIONS = (
    "pg_sleep",
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "lo_import",
    "lo_export",
    "dblink",
    "dblink_exec",
    "set_config",
    "pg_terminate_backend",
    "pg_cancel_backend",
    "pg_reload_conf",
)

#: Function-name prefixes blocked for the same reason: lock-taking, filesystem and
#: large-object access, query-from-string (bypasses the AST check), and replication.
_FORBIDDEN_FUNCTION_PREFIXES = (
    "pg_advisory",
    "pg_try_advisory",
    "pg_sleep",
    "pg_read_",
    "pg_ls_",
    "pg_stat_file",
    "pg_logdir_ls",
    "pg_file_",
    "pg_logical",
    "pg_replication",
    "pg_notify",
    "pg_switch_wal",
    "lo_",
    "loread",
    "lowrite",
    "dblink",
    "query_to_xml",
    "table_to_xml",
    "schema_to_xml",
    "database_to_xml",
    "cursor_to_xml",
)


class NlpDependencyError(RuntimeError):
    """Raised when the optional NLP dependency group is not installed."""


class UnsafeQueryError(ValueError):
    """Raised when a SQL string is not a single, read-only SELECT/WITH statement."""


MAX_LIMIT = 1000  # `top N` above this is clamped; far over any per-model/project cardinality


class InvalidSlotError(ValueError):
    """Raised when an extracted slot value is out of range (e.g. `top 0`)."""


class UnboundFilterError(ValueError):
    """Raised when the question carries a filter the matched template can't bind.

    Attributes:
        unbindable: slot names present in the question but not declared by
            the matched example (e.g. `["since"]`).
    """

    def __init__(self, unbindable: list[str]) -> None:
        self.unbindable = unbindable
        super().__init__(f"unbindable slots: {', '.join(unbindable)}")


@dataclass(frozen=True)
class SlotSpec:
    """A typed filter a catalog row's `sql_text` can bind, in placeholder order.

    An `optional` slot falls back to `default` (typically None, which the SQL
    turns into "no filter") when the question doesn't carry that filter.
    """

    name: str
    type: str
    default: Any = None
    optional: bool = False


class Backend(Protocol):
    """Structural type for anything that can embed text into vectors.

    Implemented locally by `LocalEmbeddingBackend`; an API-based backend
    could satisfy this protocol later without changing `CatalogRetriever`.
    """

    def encode(self, texts: list[str]) -> NDArray[np.float32]:
        """Return one embedding row per input text, shape (len(texts), dim)."""
        ...

    @property
    def model_name(self) -> str:
        """Identifier for the underlying embedding model (used as a cache key input)."""
        ...


class LocalEmbeddingBackend:
    """Wraps a local sentence-transformers model. Lazily imports/loads the model."""

    def __init__(self, model_name: str = DEFAULT_MODEL_NAME) -> None:
        self._model_name = model_name
        self._model: object | None = None

    def _load(self) -> object:
        if self._model is not None:
            return self._model
        # Honour the "fully offline" contract: once the model is cached, load it
        # without contacting the HF Hub. Otherwise sentence-transformers pings the
        # Hub on every load to check the snapshot, which emits an HF_TOKEN
        # rate-limit warning. huggingface_hub freezes HF_HUB_OFFLINE into a module
        # constant at import time, so this must run *before* the import below. On a
        # fresh machine the model isn't cached yet, so we leave the Hub reachable
        # for the one-time download. An explicit user-set HF_HUB_OFFLINE wins.
        if "HF_HUB_OFFLINE" not in os.environ and self._is_cached():
            os.environ["HF_HUB_OFFLINE"] = "1"
            os.environ["TRANSFORMERS_OFFLINE"] = "1"
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - exercised via missing dep
            raise NlpDependencyError(
                "The 'nlp' extra is required for `acp ask`. "
                "Install it once with: uv sync --group nlp"
            ) from exc
        self._model = SentenceTransformer(self._model_name)
        return self._model

    def _is_cached(self) -> bool:
        """True if the model snapshot is already in the local HF cache.

        Checks the filesystem directly so we don't import huggingface_hub (which
        would freeze HF_HUB_OFFLINE) before we've decided whether to set it.
        """
        repo_id = self._model_name
        if "/" not in repo_id:
            repo_id = f"sentence-transformers/{repo_id}"
        hf_home = os.environ.get("HF_HOME")
        cache_root = (
            Path(hf_home) / "hub" if hf_home else Path.home() / ".cache" / "huggingface" / "hub"
        )
        snapshots = cache_root / f"models--{repo_id.replace('/', '--')}" / "snapshots"
        return snapshots.is_dir() and any(snapshots.iterdir())

    def encode(self, texts: list[str]) -> NDArray[np.float32]:
        model = self._load()
        embeddings = model.encode(texts, convert_to_numpy=True)  # type: ignore[attr-defined]
        return np.asarray(embeddings, dtype=np.float32)

    @property
    def model_name(self) -> str:
        return self._model_name


@dataclass(frozen=True)
class NlpExample:
    """One row of the curated question -> SQL catalog."""

    example_id: int
    question: str
    sql_text: str
    description: str | None = None
    tags: tuple[str, ...] = ()
    slots: tuple[SlotSpec, ...] = ()
    paraphrases: tuple[str, ...] = ()


def _variant_texts(example: NlpExample) -> list[str]:
    """Every text a row can match on: question, paraphrases, then description."""
    texts = [example.question, *example.paraphrases]
    if example.description:
        texts.append(example.description)
    return texts


class Reranker(Protocol):
    """Scores (question, candidate text) pairs; higher means a better match."""

    def score(self, pairs: list[tuple[str, str]]) -> list[float]:
        """Return one relevance score in [0, 1] per input pair."""
        ...


class LocalCrossEncoder:
    """Wraps a local sentence-transformers cross-encoder. Lazily imports/loads it."""

    def __init__(self, model_name: str = DEFAULT_RERANK_MODEL) -> None:
        self._model_name = model_name
        self._model: object | None = None

    def is_available(self) -> bool:
        """True if the model can load without network (cached) or the Hub is reachable."""
        return LocalEmbeddingBackend(self._model_name)._is_cached()

    def _load(self) -> object:
        if self._model is not None:
            return self._model
        if "HF_HUB_OFFLINE" not in os.environ and self.is_available():
            os.environ["HF_HUB_OFFLINE"] = "1"
            os.environ["TRANSFORMERS_OFFLINE"] = "1"
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as exc:  # pragma: no cover - exercised via missing dep
            raise NlpDependencyError(
                "The 'nlp' extra is required for `acp ask`. "
                "Install it once with: uv sync --group nlp"
            ) from exc
        self._model = CrossEncoder(self._model_name)
        return self._model

    def score(self, pairs: list[tuple[str, str]]) -> list[float]:
        model = self._load()
        logits = np.asarray(model.predict(pairs), dtype=np.float64)  # type: ignore[attr-defined]
        return [float(s) for s in 1.0 / (1.0 + np.exp(-logits))]


_WORD_RE = re.compile(r"[a-z0-9_]+")

#: Score subtracted from a row whose question says the opposite of the user's question.
CONTRAST_PENALTY = 0.25

#: Opposing term sets for the optional contrast guard (see `apply_contrast_guard`).
_CONTRAST_PAIRS: tuple[tuple[frozenset[str], frozenset[str]], ...] = (
    (
        frozenset(
            "least fewest lowest minimum min smallest shortest bottom cheapest fewer lower".split()
        ),
        frozenset(
            "most highest maximum max largest longest biggest top priciest expensive more".split()
        ),
    ),
    (frozenset({"read", "reads"}), frozenset({"write", "writes", "written", "wrote"})),
    (frozenset({"input", "inputs"}), frozenset({"output", "outputs", "completion", "generated"})),
    (
        frozenset(
            {"cost", "costs", "money", "dollar", "dollars", "usd", "price", "priced", "bill"}
        ),
        frozenset({"token", "tokens"}),
    ),
)

#: Words shorter than this are never spell-corrected (too ambiguous).
_MIN_CORRECTABLE_LEN = 4
_TYPO_CUTOFF = 0.8


def _correct_typos(question: str, vocabulary: set[str]) -> str:
    """Replace words absent from *vocabulary* by their closest vocabulary word, if close enough.

    Catalog-domain spell check: "sesions" -> "sessions". Unknown words with no close
    match (e.g. "lisbon") are left alone, so out-of-catalog questions stay out-of-catalog.
    """

    def fix(match: re.Match[str]) -> str:
        word = match.group(0)
        if len(word) < _MIN_CORRECTABLE_LEN or word in vocabulary or word.isdigit():
            return word
        close = get_close_matches(word.lower(), vocabulary, n=1, cutoff=_TYPO_CUTOFF)
        return close[0] if close else word

    return _WORD_RE.sub(fix, question.lower())


def _contrast_conflict(question_words: set[str], row_words: set[str]) -> bool:
    """True if the question uses a term whose opposite (and not the term) is in the row."""
    for left, right in _CONTRAST_PAIRS:
        for mine, theirs in ((left, right), (right, left)):
            asks = question_words & mine
            if asks and not question_words & theirs and row_words & theirs and not row_words & mine:
                return True
    return False


def apply_contrast_guard(
    question: str,
    ranked: list[tuple[NlpExample, float]],
    *,
    penalty: float = CONTRAST_PENALTY,
) -> list[tuple[NlpExample, float]]:
    """Demote rows whose question holds the opposite of a contrast term in *question*.

    Targets "fewest tokens per project" matching "top 10 projects by tokens": embeddings
    treat opposites as near-synonyms. A row is demoted only when its question holds the
    opposite term and lacks the asked one; questions using both sides are left alone.
    """
    question_words = set(_WORD_RE.findall(question.lower()))
    guarded = [
        (
            example,
            score - penalty
            if _contrast_conflict(question_words, set(_WORD_RE.findall(example.question.lower())))
            else score,
        )
        for example, score in ranked
    ]
    return sorted(guarded, key=lambda pair: -pair[1])


def _char_ngrams(text: str, lo: int = 3, hi: int = 5) -> list[str]:
    """Character n-grams (lo..hi) of *text* with word boundaries kept as spaces."""
    padded = f" {' '.join(re.findall(r'[a-z0-9_]+', text.lower()))} "
    return [padded[i : i + n] for n in range(lo, hi + 1) for i in range(len(padded) - n + 1)]


class LexicalIndex:
    """BM25 over character 3-5-grams: tolerates typos that break word matching.

    Scores are divided by the best score a perfect match could reach for the
    query (every query gram present once in a document of average length), so
    they land in roughly [0, 1] on an absolute scale and an out-of-catalog
    question cannot look good just by being the least-bad hit.
    """

    K1 = 1.2
    B = 0.75

    def __init__(self, documents: list[str]) -> None:
        self._postings: dict[str, list[tuple[int, int]]] = {}
        self._lengths: list[int] = []
        for doc_id, document in enumerate(documents):
            grams = Counter(_char_ngrams(document))
            self._lengths.append(sum(grams.values()))
            for gram, tf in grams.items():
                self._postings.setdefault(gram, []).append((doc_id, tf))
        self._n_docs = len(documents)
        self._avg_len = (sum(self._lengths) / self._n_docs) if self._n_docs else 1.0

    def _idf(self, gram: str) -> float:
        df = len(self._postings.get(gram, ()))
        return math.log(1.0 + (self._n_docs - df + 0.5) / (df + 0.5))

    def scores(self, query: str) -> NDArray[np.float32]:
        """Normalised BM25 score of *query* against each document."""
        out = np.zeros(self._n_docs, dtype=np.float64)
        query_grams = set(_char_ngrams(query))
        best = 0.0
        for gram in query_grams:
            idf = self._idf(gram)
            best += idf * (self.K1 + 1.0)
            for doc_id, tf in self._postings.get(gram, ()):
                norm = self.K1 * (1.0 - self.B + self.B * self._lengths[doc_id] / self._avg_len)
                out[doc_id] += idf * tf * (self.K1 + 1.0) / (tf + norm)
        if best > 0.0:
            out /= best
        return np.asarray(np.clip(out, 0.0, 1.0), dtype=np.float32)


def _cosine_similarity(
    query_vec: NDArray[np.float32], corpus: NDArray[np.float32]
) -> NDArray[np.float32]:
    """Cosine similarity of a single query vector against each corpus row."""
    query_norm = query_vec / (np.linalg.norm(query_vec) + 1e-12)
    corpus_norm = corpus / (np.linalg.norm(corpus, axis=1, keepdims=True) + 1e-12)
    return np.asarray(corpus_norm @ query_norm, dtype=np.float32)


def _catalog_cache_key(model_name: str, examples: list[NlpExample]) -> str:
    """Stable hash of the model name plus the catalog's (id, variant texts) rows.

    Sorted by `example_id` so row order in the source query doesn't affect the
    key, and encoded deterministically so the same catalog always hashes the
    same way across processes/machines.
    """

    def _length_prefixed(chunk: bytes) -> bytes:
        return len(chunk).to_bytes(8, "big") + chunk

    hasher = hashlib.sha256()
    hasher.update(_length_prefixed(model_name.encode("utf-8")))
    for example in sorted(examples, key=lambda e: e.example_id):
        hasher.update(_length_prefixed(str(example.example_id).encode("utf-8")))
        for text in _variant_texts(example):
            hasher.update(_length_prefixed(text.encode("utf-8")))
        hasher.update(_length_prefixed(b"|"))
    return hasher.hexdigest()


def _load_cached_embeddings(cache_dir: Path, cache_key: str) -> NDArray[np.float32] | None:
    """Return cached embeddings if a valid, matching cache entry exists, else None.

    Any failure (missing files, corrupt JSON/npy, key mismatch, wrong shape)
    is treated as a cache miss rather than raised.
    """
    sidecar_path = cache_dir / f"{cache_key}.json"
    array_path = cache_dir / f"{cache_key}.npy"
    try:
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        if not isinstance(sidecar, dict) or sidecar.get("cache_key") != cache_key:
            return None
        embeddings: NDArray[np.float32] = np.load(array_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if embeddings.dtype != np.float32 or embeddings.ndim != 2:
        return None
    return embeddings


def _store_cached_embeddings(
    cache_dir: Path, cache_key: str, embeddings: NDArray[np.float32]
) -> None:
    """Write embeddings + sidecar atomically. Best-effort: failures are swallowed."""
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        array_path = cache_dir / f"{cache_key}.npy"
        sidecar_path = cache_dir / f"{cache_key}.json"
        array_fd, array_tmp_name = tempfile.mkstemp(dir=cache_dir, suffix=".npy.tmp")
        try:
            with os.fdopen(array_fd, "wb") as tmp_file:
                np.save(tmp_file, embeddings)
            os.replace(array_tmp_name, array_path)
        finally:
            Path(array_tmp_name).unlink(missing_ok=True)

        # Sidecar is written (atomically) only after the array lands, so a
        # reader never sees a sidecar whose matching array is missing.
        sidecar_fd, sidecar_tmp_name = tempfile.mkstemp(dir=cache_dir, suffix=".json.tmp")
        try:
            with os.fdopen(sidecar_fd, "w", encoding="utf-8") as tmp_file:
                tmp_file.write(json.dumps({"cache_key": cache_key}))
            os.replace(sidecar_tmp_name, sidecar_path)
        finally:
            Path(sidecar_tmp_name).unlink(missing_ok=True)
    except OSError:
        pass


class CatalogRetriever:
    """Ranks curated `NlpExample` rows against a free-text question.

    Each row is scored as the max over its variant texts of
    `w * cosine + (1 - w) * bm25`; with a *reranker* the top `RERANK_TOP_K`
    rows are re-scored by the cross-encoder on its own (sigmoid) scale, which
    has its own acceptance constants.
    """

    def __init__(
        self,
        examples: list[NlpExample],
        backend: Backend,
        *,
        cache_dir: Path | None = None,
        reranker: Reranker | None = None,
        lexical_weight: float = 1.0,
        contrast_guard: bool = False,
    ) -> None:
        self._examples = examples
        self._backend = backend
        self._reranker = reranker
        self._lexical_weight = lexical_weight
        self._contrast_guard = contrast_guard
        self._embeddings: NDArray[np.float32] | None = None
        self._variant_rows: NDArray[np.intp] = np.zeros(0, dtype=np.intp)
        self._variants: list[str] = []
        self._lexical: LexicalIndex | None = None
        self._vocabulary: set[str] = set()
        self._rerank_warned = False
        self._set_acceptance()
        if not examples:
            return

        rows: list[int] = []
        for row, example in enumerate(examples):
            for text in _variant_texts(example):
                self._variants.append(text)
                rows.append(row)
        self._variant_rows = np.asarray(rows, dtype=np.intp)
        self._lexical = LexicalIndex(self._variants)
        self._vocabulary = {w for v in self._variants for w in _WORD_RE.findall(v.lower())}

        resolved_cache_dir = cache_dir or get_settings().nlp_embeddings_cache_dir
        cache_key = _catalog_cache_key(backend.model_name, examples)
        cached = _load_cached_embeddings(resolved_cache_dir, cache_key)
        if cached is not None and cached.shape[0] == len(self._variants):
            self._embeddings = cached
            return

        self._embeddings = backend.encode(self._variants)
        _store_cached_embeddings(resolved_cache_dir, cache_key, self._embeddings)

    def _set_acceptance(self) -> None:
        """Pick the threshold/gap for the active scoring scale (cosine, hybrid or re-ranked)."""
        if self._reranker is not None:
            self.threshold, self.gap_margin = RERANK_CONFIDENCE_THRESHOLD, RERANK_GAP_MARGIN
        elif self._lexical_weight < 1.0:
            self.threshold, self.gap_margin = HYBRID_CONFIDENCE_THRESHOLD, GAP_MARGIN
        else:
            self.threshold, self.gap_margin = CONFIDENCE_THRESHOLD, 0.0

    def _fused_row_scores(self, question: str) -> tuple[NDArray[np.float32], NDArray[np.intp]]:
        """Per-row fused score and the index (into `_variants`) of each row's best variant."""
        if self._embeddings is None or self._lexical is None:
            return np.zeros(0, dtype=np.float32), np.zeros(0, dtype=np.intp)
        question = _correct_typos(question, self._vocabulary)
        query_vec = self._backend.encode([question])[0]
        cosine = _cosine_similarity(query_vec, self._embeddings)
        w = self._lexical_weight
        fused = w * cosine + (1.0 - w) * self._lexical.scores(question)
        scores = np.full(len(self._examples), -np.inf, dtype=np.float32)
        best = np.zeros(len(self._examples), dtype=np.intp)
        for variant, row in enumerate(self._variant_rows.tolist()):
            if fused[variant] > scores[row]:
                scores[row] = fused[variant]
                best[row] = variant
        return scores, best

    def search(self, question: str, top_k: int = 3) -> list[tuple[NlpExample, float]]:
        """Return up to *top_k* (example, score) pairs, best match first."""
        if not self._examples or self._embeddings is None:
            return []
        scores, best = self._fused_row_scores(question)
        order = np.argsort(-scores, kind="stable")
        ranked = [(self._examples[i], float(scores[i])) for i in order]
        if self._reranker is not None:
            head = order[: max(RERANK_TOP_K, top_k)]
            try:
                rescored = self._reranker.score([(question, self._variants[best[i]]) for i in head])
            except Exception as exc:  # model missing offline, download failure, ...
                self._warn_rerank_unavailable(exc)
                self._reranker = None
                self._set_acceptance()
            else:
                ranked = sorted(
                    ((self._examples[i], s) for i, s in zip(head, rescored, strict=True)),
                    key=lambda p: -p[1],
                )
        if self._contrast_guard:
            ranked = apply_contrast_guard(question, ranked)
        return ranked[:top_k]

    def _warn_rerank_unavailable(self, exc: Exception) -> None:
        if not self._rerank_warned:
            self._rerank_warned = True
            print(
                f"re-ranker unavailable ({type(exc).__name__}); using fused scores", file=sys.stderr
            )

    def is_confident(self, score: float) -> bool:
        """Whether *score* clears the confidence threshold of the active scoring scale."""
        return score >= self.threshold

    def accepts(
        self, ranked: list[tuple[NlpExample, float]] | tuple[tuple[NlpExample, float], ...]
    ) -> bool:
        """Accept iff the top score is confident and leads the runner-up by `gap_margin`."""
        if not ranked or not self.is_confident(ranked[0][1]):
            return False
        return len(ranked) < 2 or ranked[0][1] - ranked[1][1] >= self.gap_margin


def hybrid_retriever_factory(
    backend: Backend,
    *,
    rerank: bool = False,
    lexical_weight: float = LEXICAL_WEIGHT,
    reranker: Reranker | None = None,
    contrast_guard: bool = True,
) -> Callable[[list[NlpExample]], CatalogRetriever]:
    """Retriever factory for `acp ask` and the eval: hybrid fusion, plus the re-ranker if wanted."""
    active = (reranker or LocalCrossEncoder()) if rerank else None
    return lambda scoped: CatalogRetriever(
        scoped,
        backend,
        reranker=active,
        lexical_weight=lexical_weight,
        contrast_guard=contrast_guard,
    )


def load_examples(rows: list[dict[str, Any]]) -> list[NlpExample]:
    """Build `NlpExample` objects from raw dict rows (e.g. from `fetch_all`)."""
    examples = []
    for row in rows:
        tags = row.get("tags") or ()
        raw_slots = row.get("slots") or ()
        slots = tuple(
            SlotSpec(
                name=str(s["name"]),
                type=str(s["type"]),
                default=s.get("default"),
                optional=bool(s.get("optional", False)),
            )
            for s in raw_slots
        )
        examples.append(
            NlpExample(
                example_id=int(row["example_id"]),
                question=str(row["question"]),
                sql_text=str(row["sql_text"]),
                description=None if row.get("description") is None else str(row["description"]),
                tags=tuple(tags),
                slots=slots,
                paraphrases=tuple(row.get("paraphrases") or ()),
            )
        )
    return examples


def extract_session_id(question: str) -> tuple[str | None, str]:
    """Pull a `session_id=<value>` token, or a bare pasted UUID, out of *question*.

    Thin wrapper over `extract_slots` kept for existing callers/tests that
    only care about the session_id slot. Returns the extracted session id and
    the question with that token removed (leftover whitespace collapsed), or
    `(None, question)` unchanged when no such token is found.
    """
    slots, cleaned = extract_slots(question)
    return slots.get("session_id"), cleaned


def _last_calendar_month_range(today: date) -> tuple[date, date]:
    """`(since, until)` for the calendar month before *today*'s month.

    "last month" means the previous calendar month, not a trailing 30-day
    window (that's `last N days`). `until` is exclusive: on any day in March
    this returns `(Feb 1st, Mar 1st)`.
    """
    first_of_this_month = today.replace(day=1)
    last_day_of_prev_month = first_of_this_month - timedelta(days=1)
    return last_day_of_prev_month.replace(day=1), first_of_this_month


def extract_slots(
    question: str,
    *,
    known_projects: list[str] | None = None,
    known_models: list[str] | None = None,
    today: date | None = None,
) -> tuple[dict[str, Any], str]:
    """Pull typed filters out of *question*, most specific pattern first.

    Recognises, in this order (each match is removed from the question before
    the next pattern runs, so ranking later sees clean residual text):
    a bare UUID or `session_id=<id>` -> `session_id`; an explicit
    `since YYYY-MM[-DD]` -> `since` (a `date`); `last N days` -> `since`
    (`today - N days`); `last month` -> `since` + `until` (the 1st of the
    previous calendar month and the 1st of this one, see
    `_last_calendar_month_range`); `top N` -> `limit` (an int,
    extracted before the known-value passes below so a project/model
    literally named "top" can't swallow the token first); a whole-token
    match against *known_projects* -> `project`; a whole-token match against
    *known_models* -> `model`.

    Returns `(slots, cleaned_question)`. `slots` maps slot type name (one of
    `SLOT_TYPES`) to its extracted value; a type absent from the question is
    simply absent from the dict. `cleaned_question` has every matched span
    removed and whitespace collapsed.
    """
    today = today or date.today()
    known_projects = known_projects or []
    known_models = known_models or []
    slots: dict[str, Any] = {}
    remaining = question

    explicit_match = _SESSION_ID_RE.search(remaining)
    if explicit_match:
        slots["session_id"] = explicit_match.group(1)
        remaining = _remove_span(remaining, explicit_match.start(), explicit_match.end())
    else:
        bare_match = _BARE_UUID_RE.search(remaining)
        if bare_match:
            slots["session_id"] = bare_match.group(0)
            remaining = _remove_span(remaining, bare_match.start(), bare_match.end())

    since_match = _SINCE_DATE_RE.search(remaining)
    if since_match:
        raw = since_match.group(1)
        parts = [int(p) for p in raw.split("-")]
        year, month = parts[0], parts[1]
        day = parts[2] if len(parts) > 2 else 1
        try:
            since_date: date | None = date(year, month, day)
        except ValueError:
            # Shape matched (\d{4}-\d{2}[-\d{2}]) but isn't a real calendar
            # date (e.g. 2026-13-45, 2026-02-30). Treat as no `since` slot so
            # the question refuses through the normal unbindable-filter path
            # instead of crashing the CLI with a bare ValueError.
            since_date = None
        if since_date is not None:
            slots["since"] = since_date
            remaining = _remove_span(remaining, since_match.start(), since_match.end())
    else:
        last_n_match = _LAST_N_DAYS_RE.search(remaining)
        if last_n_match:
            days = int(last_n_match.group(1))
            slots["since"] = today - timedelta(days=days)
            remaining = _remove_span(remaining, last_n_match.start(), last_n_match.end())
        else:
            last_month_match = _LAST_MONTH_RE.search(remaining)
            if last_month_match:
                slots["since"], slots["until"] = _last_calendar_month_range(today)
                remaining = _remove_span(
                    remaining, last_month_match.start(), last_month_match.end()
                )

    top_n_match = _TOP_N_RE.search(remaining)
    if top_n_match:
        slots["limit"] = int(top_n_match.group(1))
        remaining = _remove_span(remaining, top_n_match.start(), top_n_match.end())

    project_match = _match_known_value(remaining, known_projects)
    if project_match:
        value, start, end = project_match
        slots["project"] = value
        remaining = _remove_span(remaining, start, end)

    model_match = _match_known_value(remaining, known_models)
    if model_match:
        value, start, end = model_match
        slots["model"] = value
        remaining = _remove_span(remaining, start, end)

    cleaned = re.sub(r"\s+", " ", remaining).strip()
    return slots, cleaned


def _remove_span(text: str, start: int, end: int) -> str:
    """Delete `text[start:end]`, collapsing the gap it leaves."""
    return text[:start] + text[end:]


def _match_known_value(text: str, known_values: list[str]) -> tuple[str, int, int] | None:
    """Find the longest whole-token, case-insensitive match of *known_values* in *text*.

    Longest-first avoids a short value (e.g. "api") shadowing a longer one
    that contains it (e.g. "api-gateway") when both are known values.
    """
    for value in sorted(known_values, key=len, reverse=True):
        if not value:
            continue
        # \b only fires between a word char and a non-word char, so it can
        # never match next to a value whose own edge char is already
        # non-word (e.g. "c++", "(beta)") -- the whole pattern would then
        # silently fail to match at all. Only assert \b on edges where the
        # value's own char is a word char; a non-word edge char already
        # can't blend into a surrounding word, so no assertion is needed
        # there.
        left = r"\b" if value[0].isalnum() or value[0] == "_" else ""
        right = r"\b" if value[-1].isalnum() or value[-1] == "_" else ""
        pattern = re.compile(rf"{left}{re.escape(value)}{right}", re.IGNORECASE)
        match = pattern.search(text)
        if match:
            return value, match.start(), match.end()
    return None


def check_bindable(example: NlpExample, slots: dict[str, Any]) -> list[str]:
    """Return the slot names *example* cannot bind, given the extracted *slots*.

    A slot name present in *slots* but not among the types the example
    declares in its `slots` (`SlotSpec.type`) is unbindable -- executing the
    example's template would silently drop that filter.
    """
    declared_types = {spec.type for spec in example.slots}
    return [name for name in slots if name not in declared_types]


def build_params(example: NlpExample, slots: dict[str, Any]) -> tuple[Any, ...]:
    """Bind *slots* to *example*'s placeholders, in declared slot order.

    Each spec takes the extracted value for its type; an `optional` spec with
    none extracted takes its `default`. A required spec with no value is a
    catalog authoring / user error, raised as `UnboundFilterError` rather than
    a bare KeyError. A `limit` below 1 raises `InvalidSlotError`; one above
    `MAX_LIMIT` is clamped.
    """
    params: list[Any] = []
    missing: list[str] = []
    for spec in example.slots:
        if spec.type in slots:
            value = slots[spec.type]
            if spec.type == "limit":
                if value < 1:
                    raise InvalidSlotError(f"top N needs N \u2265 1 (got {value})")
                value = min(value, MAX_LIMIT)
            params.append(value)
        elif spec.optional:
            params.append(spec.default)
        else:
            missing.append(spec.type)
    if missing:
        raise UnboundFilterError(missing)
    return tuple(params)


def count_placeholders(sql: str) -> int:
    """Count `%s` bind placeholders in *sql* (used to size query params).

    String literals are blanked first so a literal `%s` inside a quoted value
    (e.g. a `LIKE '%s%'` pattern) is not miscounted as a bind marker.
    """
    return _blank_string_literals(sql).count("%s")


def filter_examples(examples: list[NlpExample], *, session_scoped: bool) -> list[NlpExample]:
    """Keep only session-scoped examples, or only non-session-scoped ones.

    When *session_scoped* is True, keep examples tagged `SESSION_SCOPED_TAG`;
    otherwise keep examples that lack that tag.
    """
    if session_scoped:
        return [example for example in examples if SESSION_SCOPED_TAG in example.tags]
    return [example for example in examples if SESSION_SCOPED_TAG not in example.tags]


class _ReadOnlyInspector(Visitor):
    """Walks a parsed SQL tree, collecting anything `ensure_read_only` forbids.

    Any AST node whose class name ends in ``Stmt`` other than the outer
    ``SelectStmt``/``RawStmt`` wrapper is a nested write (e.g. an `INSERT`
    hidden inside a CTE), regardless of where in the tree it appears. A
    `SelectStmt` with a non-empty `intoClause` is `SELECT ... INTO <table>`,
    itself a write. Every `FuncCall` name is collected so callers can check
    it against `_FORBIDDEN_FUNCTIONS`.
    """

    _ALLOWED_STMT_NODES = ("SelectStmt", "RawStmt")

    def __init__(self) -> None:
        self.forbidden_stmt_types: list[str] = []
        self.has_select_into = False
        self.func_names: list[str] = []

    def visit(self, ancestors: object, node: object) -> None:
        name = type(node).__name__
        if name == "FuncCall":
            self.func_names.append(
                ".".join(part.sval for part in node.funcname)  # type: ignore[attr-defined]
            )
        elif name == "SelectStmt":
            if node.intoClause is not None:  # type: ignore[attr-defined]
                self.has_select_into = True
        elif name.endswith("Stmt") and name not in self._ALLOWED_STMT_NODES:
            self.forbidden_stmt_types.append(name)
        return None


def ensure_read_only(sql: str) -> None:
    """Reject anything that is not a single, read-only SELECT/WITH statement.

    Parses *sql* with the real PostgreSQL grammar (`pglast`, backed by
    `libpg_query`) rather than pattern-matching the text, so dollar-quoted
    strings, nested comments, and escape sequences the grammar itself
    understands can never be mistaken for statement boundaries or keywords.

    Raises:
        UnsafeQueryError: if the SQL fails to parse, contains more than one
            statement, is not a top-level SELECT (optionally `WITH`-prefixed),
            writes via `SELECT ... INTO`, embeds a data-modifying statement
            (e.g. an `INSERT` inside a CTE), or calls a forbidden function.
    """
    # `%s` is our own psycopg-style bind placeholder, not valid PostgreSQL
    # grammar. Substitute a harmless literal before parsing so templated
    # queries (e.g. `WHERE session_id = %s`) can still be structurally
    # validated; `count_placeholders` (not this function) is what counts
    # the real placeholders in the original, unmodified SQL.
    parseable_sql = sql.replace("%s", "NULL")
    try:
        statements = parse_sql(parseable_sql)
    except ParseError as exc:
        raise UnsafeQueryError(f"SQL failed to parse: {exc}") from exc

    if not statements:
        raise UnsafeQueryError("Empty SQL statement")
    if len(statements) > 1:
        raise UnsafeQueryError("Only a single SQL statement is allowed")

    top_statement = statements[0].stmt
    if type(top_statement).__name__ != "SelectStmt":
        raise UnsafeQueryError("Only SELECT / WITH statements are allowed")

    inspector = _ReadOnlyInspector()
    inspector(top_statement)

    if inspector.has_select_into:
        raise UnsafeQueryError("SELECT ... INTO is not allowed")
    if inspector.forbidden_stmt_types:
        raise UnsafeQueryError(
            f"Forbidden statement type in query: {inspector.forbidden_stmt_types[0]}"
        )
    for func_name in inspector.func_names:
        unqualified = func_name.rsplit(".", 1)[-1].lower()
        if unqualified in _FORBIDDEN_FUNCTIONS or unqualified.startswith(
            _FORBIDDEN_FUNCTION_PREFIXES
        ):
            raise UnsafeQueryError(f"Forbidden function in query: {func_name}")


def _blank_string_literals(sql: str) -> str:
    """Replace the contents of string literals with a placeholder.

    Dollar-quoted strings (`$$...$$`, `$tag$...$tag$`) are blanked first, so
    an apostrophe or semicolon living inside one can't be mis-paired with a
    single quote elsewhere in the text. Doubled quotes (`''`) are SQL's own
    escape for a literal quote inside a single-quoted string, so they are
    collapsed next. This keeps content that appears *inside* string values
    from being read as SQL syntax by `count_placeholders`.
    """
    without_dollar_quotes = re.sub(r"\$(\w*)\$.*?\$\1\$", "$$", sql, flags=re.DOTALL)
    without_escaped = without_dollar_quotes.replace("''", "")
    return re.sub(r"'[^']*'", "''", without_escaped)


@dataclass(frozen=True)
class AskDecision:
    """What `acp ask` decided to do with a question, before touching the database.

    `outcome` is `"accept"` (run `example` with `params`), `"low_confidence"`
    (best `score` under the threshold; `ranked` holds the candidates),
    `"unbindable"` (the question's filters `unbindable` aren't declared by
    `example`), or `"no_examples"` (nothing to match against).
    """

    outcome: Literal["accept", "low_confidence", "unbindable", "no_examples"]
    example: NlpExample | None = None
    score: float = 0.0
    ranked: tuple[tuple[NlpExample, float], ...] = ()
    slots: dict[str, Any] = field(default_factory=dict)
    clean_question: str = ""
    params: tuple[Any, ...] = ()
    unbindable: tuple[str, ...] = ()


def resolve_ask(
    question: str,
    examples: list[NlpExample],
    retriever_factory: Callable[[list[NlpExample]], CatalogRetriever],
    *,
    known_projects: list[str],
    known_models: list[str],
    top_k: int,
    today: date | None = None,
) -> AskDecision:
    """Pure core of `acp ask`: slots -> retrieval -> confidence -> bindability -> params.

    No database or console access; *retriever_factory* builds a retriever over
    the (session-scoped or not) example subset so callers choose the backend.

    Raises:
        UnboundFilterError: a required slot of the accepted example has no value.
        UnsafeQueryError: the accepted example's slot count disagrees with its
            placeholder count.
    """
    slots, clean_question = extract_slots(
        question, known_projects=known_projects, known_models=known_models, today=today
    )
    scoped = filter_examples(examples, session_scoped="session_id" in slots)
    if not scoped:
        return AskDecision("no_examples", slots=slots, clean_question=clean_question)

    retriever = retriever_factory(scoped)
    ranked = tuple(retriever.search(clean_question, top_k=top_k))
    if not ranked:
        return AskDecision("no_examples", slots=slots, clean_question=clean_question)

    top_example, top_score = ranked[0]

    def decide(
        outcome: Literal["accept", "low_confidence", "unbindable"], **extra: Any
    ) -> AskDecision:
        return AskDecision(
            outcome,
            top_example,
            top_score,
            ranked,
            slots,
            clean_question,
            **extra,
        )

    if not retriever.accepts(ranked):
        return decide("low_confidence")

    unbindable = check_bindable(top_example, slots)
    if unbindable:
        return decide("unbindable", unbindable=tuple(unbindable))

    params = build_params(top_example, slots)
    n_placeholders = count_placeholders(top_example.sql_text)
    if len(params) != n_placeholders:
        raise UnsafeQueryError(
            f"catalog row {top_example.example_id!r} declares "
            f"{len(params)} slot(s) but sql_text has {n_placeholders} placeholder(s)"
        )
    return decide("accept", params=params)
