"""Offline NLP retrieval layer: match natural-language questions to curated SQL.

No network access or API key is required. Retrieval works by embedding the
`claude.nlp_examples` question corpus with a local sentence-transformers
model and ranking by cosine similarity against the user's question.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
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


class NlpDependencyError(RuntimeError):
    """Raised when the optional NLP dependency group is not installed."""


class UnsafeQueryError(ValueError):
    """Raised when a SQL string is not a single, read-only SELECT/WITH statement."""


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


def _cosine_similarity(
    query_vec: NDArray[np.float32], corpus: NDArray[np.float32]
) -> NDArray[np.float32]:
    """Cosine similarity of a single query vector against each corpus row."""
    query_norm = query_vec / (np.linalg.norm(query_vec) + 1e-12)
    corpus_norm = corpus / (np.linalg.norm(corpus, axis=1, keepdims=True) + 1e-12)
    return np.asarray(corpus_norm @ query_norm, dtype=np.float32)


def _catalog_cache_key(model_name: str, examples: list[NlpExample]) -> str:
    """Stable hash of the model name plus the catalog's (id, question) rows.

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
        hasher.update(_length_prefixed(example.question.encode("utf-8")))
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
    """Ranks curated `NlpExample` rows against a free-text question."""

    def __init__(
        self,
        examples: list[NlpExample],
        backend: Backend,
        *,
        cache_dir: Path | None = None,
    ) -> None:
        self._examples = examples
        self._backend = backend
        self._embeddings: NDArray[np.float32] | None = None
        if not examples:
            return

        resolved_cache_dir = cache_dir or get_settings().nlp_embeddings_cache_dir
        cache_key = _catalog_cache_key(backend.model_name, examples)
        cached = _load_cached_embeddings(resolved_cache_dir, cache_key)
        if cached is not None and cached.shape[0] == len(examples):
            self._embeddings = cached
            return

        self._embeddings = backend.encode([example.question for example in examples])
        _store_cached_embeddings(resolved_cache_dir, cache_key, self._embeddings)

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
    a bare KeyError.
    """
    params: list[Any] = []
    missing: list[str] = []
    for spec in example.slots:
        if spec.type in slots:
            params.append(slots[spec.type])
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
        if unqualified in _FORBIDDEN_FUNCTIONS:
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

    if not retriever.is_confident(top_score):
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
