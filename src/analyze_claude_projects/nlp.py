"""Offline NLP retrieval layer: match natural-language questions to curated SQL.

No network access or API key is required. Retrieval works by embedding the
`claude.nlp_examples` question corpus with a local sentence-transformers
model and ranking by cosine similarity against the user's question.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from numpy.typing import NDArray

#: Minimum cosine similarity for a top hit to be considered a confident match.
CONFIDENCE_THRESHOLD = 0.5

#: Tag marking a catalog example as scoped to a single session_id.
SESSION_SCOPED_TAG = "session_scoped"

# Session ids are word characters and hyphens (e.g. a UUID). Restricting the
# capture to that set stops trailing sentence punctuation (`,` `.` `?`) from
# being swallowed into the id, which would otherwise fail the uuid cast.
_SESSION_ID_RE = re.compile(r"session_id\s*=\s*([\w-]+)", re.IGNORECASE)

#: Default local embedding model; small, CPU-friendly, good general recall.
DEFAULT_MODEL_NAME = "all-MiniLM-L6-v2"

_LEADING_KEYWORDS = ("select", "with")
_FORBIDDEN_KEYWORDS = (
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "truncate",
    "grant",
    "revoke",
    "create",
    "call",
    "do",
    "copy",
    "vacuum",
    "merge",
    "into",  # blocks SELECT ... INTO <table> (a write)
)

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


class NlpDependencyError(RuntimeError):
    """Raised when the optional NLP dependency group is not installed."""


class UnsafeQueryError(ValueError):
    """Raised when a SQL string is not a single, read-only SELECT/WITH statement."""


class Backend(Protocol):
    """Structural type for anything that can embed text into vectors.

    Implemented locally by `LocalEmbeddingBackend`; an API-based backend
    could satisfy this protocol later without changing `CatalogRetriever`.
    """

    def encode(self, texts: list[str]) -> NDArray[np.float32]:
        """Return one embedding row per input text, shape (len(texts), dim)."""
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


@dataclass(frozen=True)
class NlpExample:
    """One row of the curated question -> SQL catalog."""

    example_id: int
    question: str
    sql_text: str
    description: str | None = None
    tags: tuple[str, ...] = ()


def _cosine_similarity(
    query_vec: NDArray[np.float32], corpus: NDArray[np.float32]
) -> NDArray[np.float32]:
    """Cosine similarity of a single query vector against each corpus row."""
    query_norm = query_vec / (np.linalg.norm(query_vec) + 1e-12)
    corpus_norm = corpus / (np.linalg.norm(corpus, axis=1, keepdims=True) + 1e-12)
    return np.asarray(corpus_norm @ query_norm, dtype=np.float32)


class CatalogRetriever:
    """Ranks curated `NlpExample` rows against a free-text question."""

    def __init__(self, examples: list[NlpExample], backend: Backend) -> None:
        self._examples = examples
        self._backend = backend
        self._embeddings: NDArray[np.float32] | None = None
        if examples:
            self._embeddings = backend.encode([example.question for example in examples])

    def search(self, question: str, top_k: int = 3) -> list[tuple[NlpExample, float]]:
        """Return up to *top_k* (example, score) pairs, best match first."""
        if not self._examples or self._embeddings is None:
            return []
        query_vec = self._backend.encode([question])[0]
        scores = _cosine_similarity(query_vec, self._embeddings)
        ranked = sorted(zip(self._examples, scores.tolist(), strict=True), key=lambda p: -p[1])
        return ranked[:top_k]

    def is_confident(self, score: float) -> bool:
        """Whether *score* clears `CONFIDENCE_THRESHOLD`."""
        return score >= CONFIDENCE_THRESHOLD


def load_examples(rows: list[dict[str, Any]]) -> list[NlpExample]:
    """Build `NlpExample` objects from raw dict rows (e.g. from `fetch_all`)."""
    examples = []
    for row in rows:
        tags = row.get("tags") or ()
        examples.append(
            NlpExample(
                example_id=int(row["example_id"]),
                question=str(row["question"]),
                sql_text=str(row["sql_text"]),
                description=None if row.get("description") is None else str(row["description"]),
                tags=tuple(tags),
            )
        )
    return examples


def extract_session_id(question: str) -> tuple[str | None, str]:
    """Pull a `session_id=<value>` token out of *question*, if present.

    Returns the extracted session id and the question with that token
    removed (leftover whitespace collapsed). Returns `(None, question)`
    unchanged when no such token is found.
    """
    match = _SESSION_ID_RE.search(question)
    if not match:
        return None, question
    session_id = match.group(1)
    cleaned = question[: match.start()] + question[match.end() :]
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return session_id, cleaned


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


def ensure_read_only(sql: str) -> None:
    """Reject anything that is not a single, leading SELECT/WITH statement.

    Raises:
        UnsafeQueryError: if the SQL contains multiple statements, DML/DDL
            keywords, or does not start with SELECT/WITH.
    """
    stripped = _strip_comments(sql).strip()
    if not stripped:
        raise UnsafeQueryError("Empty SQL statement")

    # Leading keyword is checked on the raw (stripped) text so a literal can't
    # masquerade as the first word.
    first_word_match = re.match(r"[A-Za-z]+", stripped)
    first_word = first_word_match.group(0).lower() if first_word_match else ""
    if first_word not in _LEADING_KEYWORDS:
        raise UnsafeQueryError("Only SELECT / WITH statements are allowed")

    # Structural checks (statement count, keyword/function denylists) run on a
    # copy with string literals blanked out, so a value like 'a;b' or
    # WHERE agent_type = 'call' is not mistaken for SQL syntax.
    scannable = _blank_string_literals(stripped)
    body = scannable[:-1] if scannable.rstrip().endswith(";") else scannable
    if ";" in body:
        raise UnsafeQueryError("Only a single SQL statement is allowed")

    lowered = body.lower()
    for keyword in _FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{re.escape(keyword)}\b", lowered):
            raise UnsafeQueryError(f"Forbidden keyword in query: {keyword}")
    for func in _FORBIDDEN_FUNCTIONS:
        if re.search(rf"\b{re.escape(func)}\b", lowered):
            raise UnsafeQueryError(f"Forbidden function in query: {func}")


def _strip_comments(sql: str) -> str:
    """Remove `--` line comments and `/* */` block comments."""
    without_block = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    without_line = re.sub(r"--[^\n]*", "", without_block)
    return without_line


def _blank_string_literals(sql: str) -> str:
    """Replace the contents of single-quoted string literals with a placeholder.

    Doubled quotes (`''`) are SQL's own escape for a literal quote, so they are
    collapsed first. This keeps semicolons and keywords that appear *inside*
    string values from being read as SQL syntax by the denylist scans.
    """
    without_escaped = sql.replace("''", "")
    return re.sub(r"'[^']*'", "''", without_escaped)
