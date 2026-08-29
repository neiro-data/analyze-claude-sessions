"""Deterministic, model-free stress-testing helpers for `analyze_claude_projects.nlp`.

Everything here is pure stdlib + numpy: no network, no Postgres, no real
sentence-transformers model. It is shared by the fast test suite
(`tests/test_nlp_stress.py`), the slow suite, and the reporting script, so the
API favours composability (a named registry of pure perturbation functions,
a deterministic fake `Backend`, and plain dataclasses for corpora) over being
tailored to any single caller.

Every generator/perturbation takes an explicit `random.Random` seeded by the
caller, so a failing test can report its seed and be reproduced exactly.
"""

from __future__ import annotations

import hashlib
import random
import re
import statistics
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from numpy.typing import NDArray

from analyze_claude_projects.nlp import CatalogRetriever, NlpExample

#: Signature every entry in `PERTURBATIONS` implements: given input text and a
#: seeded RNG, return a perturbed copy. Pure function -- no hidden state.
Perturbation = Callable[[str, random.Random], str]

# --------------------------------------------------------------------------
# Character-level typo perturbations
# --------------------------------------------------------------------------

#: Adjacency map for a QWERTY keyboard, lowercase letters only. Digits and
#: punctuation are left untouched by the swap perturbation.
_QWERTY_NEIGHBORS: dict[str, str] = {
    "q": "wa",
    "w": "qeas",
    "e": "wrds",
    "r": "etdf",
    "t": "ryfg",
    "y": "tugh",
    "u": "yihj",
    "i": "uojk",
    "o": "ipkl",
    "p": "ol",
    "a": "qwsz",
    "s": "awedxz",
    "d": "serfcx",
    "f": "drtgvc",
    "g": "ftyhbv",
    "h": "gyujnb",
    "j": "huikmn",
    "k": "jiolm",
    "l": "kop",
    "z": "asx",
    "x": "zsdc",
    "c": "xdfv",
    "v": "cfgb",
    "b": "vghn",
    "n": "bhjm",
    "m": "njk",
}


def typo_adjacent_swap(text: str, rng: random.Random, rate: float = 0.1) -> str:
    """Replace some lowercase letters with an adjacent QWERTY key, at `rate` per char."""
    out = list(text)
    for i, ch in enumerate(out):
        lower = ch.lower()
        if lower in _QWERTY_NEIGHBORS and rng.random() < rate:
            replacement = rng.choice(_QWERTY_NEIGHBORS[lower])
            out[i] = replacement.upper() if ch.isupper() else replacement
    return "".join(out)


def typo_delete(text: str, rng: random.Random, rate: float = 0.1) -> str:
    """Drop some characters entirely, at `rate` per char."""
    return "".join(ch for ch in text if rng.random() >= rate)


def typo_duplicate(text: str, rng: random.Random, rate: float = 0.1) -> str:
    """Duplicate some characters, at `rate` per char."""
    out = []
    for ch in text:
        out.append(ch)
        if rng.random() < rate:
            out.append(ch)
    return "".join(out)


# --------------------------------------------------------------------------
# Casing
# --------------------------------------------------------------------------


def casing_upper(text: str, _rng: random.Random) -> str:
    """Upper-case the whole string."""
    return text.upper()


def casing_lower(text: str, _rng: random.Random) -> str:
    """Lower-case the whole string."""
    return text.lower()


def casing_title(text: str, _rng: random.Random) -> str:
    """Title-case the whole string."""
    return text.title()


# --------------------------------------------------------------------------
# Punctuation
# --------------------------------------------------------------------------

_PUNCTUATION_RE = re.compile(r"[.,!?;:]")
_EXTRA_PUNCTUATION = ("!", "?", "...", ".", ",")


def punctuation_strip(text: str, _rng: random.Random) -> str:
    """Remove common sentence punctuation."""
    return _PUNCTUATION_RE.sub("", text)


def punctuation_add(text: str, rng: random.Random) -> str:
    """Append a random extra punctuation mark."""
    return text + rng.choice(_EXTRA_PUNCTUATION)


# --------------------------------------------------------------------------
# Conversational filler
# --------------------------------------------------------------------------

_FILLER_PREFIXES = ("um, ", "so, ", "hey, ", "quick question, ", "hmm, can you tell me ")
_FILLER_SUFFIXES = (" please", " thanks", " if you can", "?", " right now")


def filler_prefix(text: str, rng: random.Random) -> str:
    """Prepend a conversational filler phrase."""
    return rng.choice(_FILLER_PREFIXES) + text


def filler_suffix(text: str, rng: random.Random) -> str:
    """Append a conversational filler phrase."""
    return text + rng.choice(_FILLER_SUFFIXES)


# --------------------------------------------------------------------------
# Word order / length
# --------------------------------------------------------------------------


def word_order_shuffle(text: str, rng: random.Random) -> str:
    """Swap one randomly chosen pair of adjacent words, if there are >= 2 words."""
    words = text.split(" ")
    if len(words) < 2:
        return text
    i = rng.randrange(len(words) - 1)
    words[i], words[i + 1] = words[i + 1], words[i]
    return " ".join(words)


def truncate_60_percent(text: str, _rng: random.Random) -> str:
    """Keep only the first 60% of whitespace-separated tokens (at least one)."""
    words = text.split(" ")
    keep = max(1, round(len(words) * 0.6))
    return " ".join(words[:keep])


_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "do",
        "does",
        "did",
        "of",
        "for",
        "in",
        "on",
        "at",
        "to",
        "there",
        "we",
        "have",
        "please",
        "can",
        "you",
        "me",
        "tell",
    }
)


def stopword_drop(text: str, _rng: random.Random) -> str:
    """Drop common stopwords, keeping only content/keyword tokens."""
    words = [w for w in text.split(" ") if w.lower() not in _STOPWORDS]
    return " ".join(words) if words else text


# --------------------------------------------------------------------------
# Whitespace / unicode noise
# --------------------------------------------------------------------------

_ACCENTED_LOOKALIKES = {
    "a": "á",  # á
    "e": "é",  # é
    "i": "í",  # í
    "o": "ó",  # ó
    "u": "ú",  # ú
    "n": "ñ",  # ñ
}
_NBSP = " "
_ZERO_WIDTH_SPACE = "​"
_CURLY_QUOTES = {"'": "’", '"': "“"}


def whitespace_unicode_noise(text: str, rng: random.Random, rate: float = 0.15) -> str:
    """Inject NBSP/zero-width spaces, double spaces, curly quotes, accented look-alikes."""
    out: list[str] = []
    for ch in text:
        replacement = ch
        if ch == " " and rng.random() < rate:
            replacement = rng.choice((" " + " ", _NBSP, " " + _ZERO_WIDTH_SPACE))
        elif ch in _CURLY_QUOTES and rng.random() < rate:
            replacement = _CURLY_QUOTES[ch]
        elif ch.lower() in _ACCENTED_LOOKALIKES and rng.random() < rate:
            look = _ACCENTED_LOOKALIKES[ch.lower()]
            replacement = look.upper() if ch.isupper() else look
        out.append(replacement)
    return unicodedata.normalize("NFC", "".join(out))


#: Every perturbation, keyed by a short class name, uniformly `(text, rng) -> str`.
#: Iterating this dict lets a caller (report script, slow suite) apply every
#: class to a corpus and attribute degradation back to a specific class.
PERTURBATIONS: dict[str, Perturbation] = {
    "typo_adjacent_swap": typo_adjacent_swap,
    "typo_delete": typo_delete,
    "typo_duplicate": typo_duplicate,
    "casing_upper": casing_upper,
    "casing_lower": casing_lower,
    "casing_title": casing_title,
    "punctuation_strip": punctuation_strip,
    "punctuation_add": punctuation_add,
    "filler_prefix": filler_prefix,
    "filler_suffix": filler_suffix,
    "word_order_shuffle": word_order_shuffle,
    "truncate_60_percent": truncate_60_percent,
    "stopword_drop": stopword_drop,
    "whitespace_unicode_noise": whitespace_unicode_noise,
}


# --------------------------------------------------------------------------
# Deterministic fake Backend (satisfies `analyze_claude_projects.nlp.Backend`)
# --------------------------------------------------------------------------

_FAKE_EMBEDDING_DIM = 64


def _stable_token_hash(token: str) -> int:
    """A `hash()`-alike that is stable across processes/runs (unlike builtin `hash`)."""
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


class FakeHashBackend:
    """Deterministic, model-free stand-in for `LocalEmbeddingBackend`.

    Encodes each text as a fixed-size bag-of-words vector: every whitespace
    token is hashed (via blake2b, stable across processes) into one of
    `_FAKE_EMBEDDING_DIM` buckets, incrementing that bucket. Same text always
    produces the same vector; no model, network, or randomness involved.
    """

    def __init__(self, dim: int = _FAKE_EMBEDDING_DIM) -> None:
        self._dim = dim

    @property
    def model_name(self) -> str:
        return f"fake-hash-backend-{self._dim}"

    def encode(self, texts: list[str]) -> NDArray[np.float32]:
        """Return one bag-of-hashed-tokens vector per input text."""
        matrix = np.zeros((len(texts), self._dim), dtype=np.float32)
        for row, text in enumerate(texts):
            for token in text.lower().split():
                matrix[row, _stable_token_hash(token) % self._dim] += 1.0
        return matrix


# --------------------------------------------------------------------------
# Adversarial SQL corpus (shared with the report script / slow suite)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SqlCase:
    """One `ensure_read_only` adversarial test case."""

    case_id: str
    sql: str
    expect_allow: bool
    note: str = ""


SQL_ADVERSARIAL_CORPUS: tuple[SqlCase, ...] = (
    SqlCase(
        "dollar_quote_semicolon",
        "SELECT $$ ; DROP TABLE t $$",
        True,
        "A real PostgreSQL parse understands dollar-quoting: the ';' and "
        "'drop' inside the literal are just string content, so this is a "
        "single, harmless SELECT.",
    ),
    SqlCase(
        "dollar_quote_tag_semicolon",
        "SELECT $tag$ ; DROP TABLE t $tag$",
        True,
        "Same, with a tagged dollar-quote.",
    ),
    SqlCase(
        "dollar_quote_apostrophe_bypass",
        "SELECT $$it's$$ ; DROP TABLE t ; SELECT $$won't$$",
        False,
        "FIXED (was a CRITICAL real bypass under the regex guard): the real "
        "grammar parses this as three separate statements -- two harmless "
        "SELECTs and a genuine DROP TABLE in between -- and ensure_read_only "
        "now correctly rejects it as multi-statement.",
    ),
    SqlCase(
        "comment_split_into",
        "SELECT/**/INTO x FROM t",
        False,
        "A real parse sees `SELECT INTO x FROM t`: a genuine SELECT INTO "
        "write, rejected for the right reason (a non-empty intoClause).",
    ),
    SqlCase(
        "comment_split_delete",
        "DEL--\nETE FROM t",
        False,
        "Not valid SQL under any comment interpretation; the parser raises "
        "a syntax error and ensure_read_only rejects it.",
    ),
    SqlCase(
        "nested_block_comment",
        "SELECT * FROM t /* /* nested */ comment */",
        True,
        "PostgreSQL's grammar (unlike ANSI SQL) nests block comments, so the "
        "whole `/* /* nested */ comment */` span is one comment and this is "
        "a single, harmless SELECT.",
    ),
    SqlCase(
        "data_modifying_cte",
        "WITH x AS (INSERT INTO t VALUES(1) RETURNING *) SELECT * FROM x",
        False,
        "Correctly rejected: the INSERT nested inside the CTE is found by "
        "walking the full parse tree for any non-SELECT statement node.",
    ),
    SqlCase(
        "e_string_backslash_escape",
        "SELECT E'\\'; DROP TABLE t; --'",
        True,
        "FIXED (was a false positive under the regex guard): the real "
        "grammar understands the E'' backslash-quote escape, so this parses "
        "as one legitimate (if odd) single-statement SELECT.",
    ),
    SqlCase(
        "u_ampersand_string",
        "SELECT U&'\\0044ROP'",
        True,
        "U&'' unicode-escape string is allowed; no keyword surfaces from the "
        "escape sequence itself.",
    ),
    SqlCase(
        "keyword_as_column_alias",
        "SELECT id AS copy FROM t",
        True,
        "FIXED (was a false positive under the regex guard): the parse tree "
        "has no COPY statement node here, only a column alias named 'copy', "
        "so this is correctly allowed.",
    ),
    SqlCase(
        "pg_sleep_qualified",
        "SELECT pg_catalog.pg_sleep(5)",
        False,
        "Schema-qualified pg_sleep is still caught by the bare word-boundary "
        "search for 'pg_sleep'.",
    ),
    SqlCase(
        "current_setting_allowed",
        "select current_setting('x')",
        True,
        "current_setting is read-only and not in the forbidden function list; "
        "allowed as intended (set_config, the writer, is denylisted).",
    ),
    SqlCase(
        "trailing_comment_after_semicolon",
        "SELECT * FROM t; -- trailing comment",
        True,
        "A single trailing semicolon plus a comment is accepted (comment is "
        "stripped first, then the sole trailing ';' is trimmed before the "
        "multi-statement check).",
    ),
    SqlCase(
        "trailing_whitespace_semicolon",
        "SELECT * FROM t;   ",
        True,
        "Trailing whitespace after the terminating semicolon is tolerated.",
    ),
    SqlCase(
        "comment_only",
        "-- just a comment",
        False,
        "Comment-only input strips to empty and is rejected as empty SQL.",
    ),
    SqlCase("empty_string", "", False, "Empty input is rejected as empty SQL."),
)


# --------------------------------------------------------------------------
# Seeded slot-input generator
# --------------------------------------------------------------------------

_SAMPLE_PROJECTS = ("api-gateway", "api", "billing", "main", "data", "c++", "a.b", "x|y", "(beta)")
_SAMPLE_MODELS = ("claude-opus-4", "since-model", "last-model", "top-model")


@dataclass(frozen=True)
class SlotQuestionCase:
    """A generated question plus the pool of known projects/models used to build it."""

    question: str
    known_projects: tuple[str, ...]
    known_models: tuple[str, ...]
    today: date


def generate_slot_questions(
    rng: random.Random,
    count: int,
    *,
    today: date = date(2026, 1, 15),
) -> list[SlotQuestionCase]:
    """Generate `count` seeded synthetic questions that exercise slot extraction.

    Mixes session ids, since/last-N-days/last-month dates, known project/model
    tokens (including regex-metacharacter and substring-collision names), and
    `top N` limits, in random combinations, so callers can fuzz
    `extract_slots` without hand-writing every combination.
    """
    fragments: list[Callable[[], str]] = [
        lambda: f"session_id={rng.randrange(10**6):06d}",
        lambda: str(_random_uuid(rng)),
        lambda: f"since {today.year}-{rng.randint(1, 12):02d}",
        lambda: f"last {rng.randint(0, 99999)} days",
        lambda: "last month",
        lambda: rng.choice(_SAMPLE_PROJECTS),
        lambda: rng.choice(_SAMPLE_MODELS),
        lambda: f"top {rng.randint(0, 100)}",
    ]
    stems = ("how many sessions", "show me calls", "count tokens for", "list transcripts")
    cases = []
    for _ in range(count):
        stem = rng.choice(stems)
        n_fragments = rng.randint(0, 3)
        chosen = [rng.choice(fragments)() for _ in range(n_fragments)]
        question = " ".join([stem, *chosen])
        cases.append(
            SlotQuestionCase(
                question=question,
                known_projects=_SAMPLE_PROJECTS,
                known_models=_SAMPLE_MODELS,
                today=today,
            )
        )
    return cases


def _random_uuid(rng: random.Random) -> str:
    """A syntactically valid, seeded-random UUID-shaped string."""
    hex_chars = "0123456789abcdef"

    def group(n: int) -> str:
        return "".join(rng.choice(hex_chars) for _ in range(n))

    return f"{group(8)}-{group(4)}-{group(4)}-{group(4)}-{group(12)}"


# ---------------------------------------------------------------------------
# Shared measurement helpers
#
# Used by BOTH tests/test_nlp_stress_slow.py and scripts/rag_stress.py. They
# live here rather than being copy-pasted into each caller so the test suite and
# the generated report can never disagree about what "recall@1" means.
# ---------------------------------------------------------------------------

CONFIDENCE_SWEEP: tuple[float, ...] = tuple(round(0.30 + 0.05 * i, 2) for i in range(10))
"""Confidence thresholds swept by the stress battery: 0.30 .. 0.75 step 0.05."""


def load_yaml(path: Path) -> dict[str, Any]:
    """Load a corpus YAML file."""
    with path.open() as handle:
        data: dict[str, Any] = yaml.safe_load(handle)
    return data


def build_catalog_examples(questions: list[str]) -> list[NlpExample]:
    """Wrap plain question strings as `NlpExample`s with a dummy SQL body."""
    return [
        NlpExample(example_id=i, question=q, sql_text="SELECT 1")
        for i, q in enumerate(questions, start=1)
    ]


def paraphrase_pairs(eval_data: dict[str, Any]) -> list[tuple[str, str]]:
    """(catalog_question, paraphrase) pairs from an eval corpus."""
    return [
        (catalog_question, paraphrase)
        for catalog_question, entry in eval_data["catalog"].items()
        for paraphrase in entry["paraphrases"]
    ]


def score_pairs(
    retriever: CatalogRetriever, pairs: list[tuple[str, str]]
) -> tuple[float, float, float]:
    """Return (recall@1, recall@3, mean top-1 score) over the given pairs.

    Raises `ValueError` on an empty corpus: silently reporting 0.0 would hide a
    misconfigured corpus behind a plausible-looking "quality regression".
    """
    if not pairs:
        raise ValueError("cannot score an empty pair list -- check the corpus YAML")
    hits_at_1 = 0
    hits_at_3 = 0
    scores: list[float] = []
    for catalog_question, query in pairs:
        ranked = retriever.search(query, top_k=3)
        ranked_questions = [example.question for example, _score in ranked]
        if ranked_questions and ranked_questions[0] == catalog_question:
            hits_at_1 += 1
        if catalog_question in ranked_questions:
            hits_at_3 += 1
        if ranked:
            scores.append(ranked[0][1])
    n = len(pairs)
    return hits_at_1 / n, hits_at_3 / n, statistics.fmean(scores) if scores else 0.0


def percentiles(samples: list[float]) -> tuple[float, float]:
    """Return (p50, p95) of `samples`, in the same unit as the input."""
    if not samples:
        raise ValueError("cannot compute percentiles of an empty sample list")
    ordered = sorted(samples)
    p50 = statistics.median(ordered)
    p95_index = min(len(ordered) - 1, int(len(ordered) * 0.95))
    return p50, ordered[p95_index]


def synthetic_catalog(n: int, seed: int) -> list[str]:
    """Generate `n` deterministic synthetic catalog questions for scale tests."""
    rng = random.Random(seed)  # noqa: S311 - deterministic fixture data, not crypto
    topics = ["sessions", "projects", "transcripts", "tokens", "models", "calls", "cache"]
    verbs = ["how many", "count", "total number of", "list", "show me"]
    return [f"{rng.choice(verbs)} {rng.choice(topics)} #{i}" for i in range(n)]
