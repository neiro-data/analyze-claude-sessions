from __future__ import annotations

import os
import re
import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import psycopg
import pytest
from typer.testing import CliRunner

from analyze_claude_projects import cli as cli_mod
from analyze_claude_projects import nlp as nlp_mod
from analyze_claude_projects.nlp import (
    CatalogRetriever,
    LocalEmbeddingBackend,
    NlpDependencyError,
    NlpExample,
    SlotSpec,
    UnboundFilterError,
    UnsafeQueryError,
    check_bindable,
    count_placeholders,
    ensure_read_only,
    extract_session_id,
    extract_slots,
    filter_examples,
    load_examples,
)

runner = CliRunner()


# --------------------------------------------------------------------------
# ensure_read_only
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select 1",
        "  select 1  ",
        "WITH x AS (SELECT 1) SELECT * FROM x",
        "-- a comment\nSELECT 1",
        "SELECT 1 -- trailing comment",
        "/* block */ SELECT 1",
        "SELECT 1;",
        # Semicolons / banned words inside string literals are not SQL syntax.
        "SELECT 'a;b' AS s",
        "SELECT model FROM claude.api_calls WHERE agent_type = 'call'",
    ],
)
def test_ensure_read_only_accepts(sql: str) -> None:
    ensure_read_only(sql)  # must not raise


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET a = 1",
        "DELETE FROM t",
        "DROP TABLE t",
        "ALTER TABLE t ADD COLUMN a int",
        "CREATE TABLE t (a int)",
        "TRUNCATE t",
        "SELECT 1; SELECT 2",
        "SELECT 1; DROP TABLE t;",
        "SELECT * INTO leaked FROM claude.api_calls",
        "SELECT pg_sleep(600)",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT dblink_exec('...', '...')",
        "",
        "   ",
    ],
)
def test_ensure_read_only_rejects(sql: str) -> None:
    with pytest.raises(UnsafeQueryError):
        ensure_read_only(sql)


def test_ensure_read_only_rejects_trailing_statement_after_select() -> None:
    with pytest.raises(UnsafeQueryError):
        ensure_read_only("SELECT 1; SELECT 2;")


def test_ensure_read_only_accepts_sql_with_placeholder() -> None:
    ensure_read_only("SELECT * FROM claude.sessions WHERE session_id = %s")  # must not raise


# --------------------------------------------------------------------------
# extract_session_id
# --------------------------------------------------------------------------


def test_extract_session_id_extracts_and_cleans() -> None:
    session_id, cleaned = extract_session_id("cache report session_id=abc-123")

    assert session_id == "abc-123"
    assert cleaned == "cache report"


def test_extract_session_id_handles_space_before_equals() -> None:
    session_id, cleaned = extract_session_id("cache report session_id =abc-123")

    assert session_id == "abc-123"
    assert cleaned == "cache report"


def test_extract_session_id_space_after_equals_does_not_bind() -> None:
    """`session_id= foo` must not bind "foo" -- a space after `=` means the
    following token wasn't pasted as the value, so this is not a match."""
    session_id, cleaned = extract_session_id("cache report session_id= foo")

    assert session_id is None
    assert cleaned == "cache report session_id= foo"


def test_extract_session_id_case_insensitive() -> None:
    session_id, cleaned = extract_session_id("cache report SESSION_ID=abc-123")

    assert session_id == "abc-123"
    assert cleaned == "cache report"


def test_extract_session_id_returns_none_when_absent() -> None:
    question = "how many sessions do we have?"

    session_id, cleaned = extract_session_id(question)

    assert session_id is None
    assert cleaned == question


def test_extract_session_id_no_leftover_double_spaces() -> None:
    _, cleaned = extract_session_id("show me the session_id=xyz stats please")

    assert "  " not in cleaned
    assert cleaned == "show me the stats please"


def test_extract_session_id_excludes_trailing_comma() -> None:
    session_id, cleaned = extract_session_id("cache report session_id=abc-123, please")

    assert session_id == "abc-123"
    assert "," not in session_id
    assert "abc-123" not in cleaned


@pytest.mark.parametrize("trailing", ["?", "."])
def test_extract_session_id_excludes_trailing_punctuation(trailing: str) -> None:
    session_id, _cleaned = extract_session_id(f"cache report session_id=abc-123{trailing}")

    assert session_id == "abc-123"
    assert trailing not in session_id


# --------------------------------------------------------------------------
# extract_slots
# --------------------------------------------------------------------------


def test_extract_slots_bare_uuid_no_prefix_required() -> None:
    slots, cleaned = extract_slots("11111111-1111-1111-1111-111111111111 cache report")

    assert slots["session_id"] == "11111111-1111-1111-1111-111111111111"
    assert cleaned == "cache report"


def test_extract_slots_explicit_session_id_still_works() -> None:
    slots, cleaned = extract_slots("cache report session_id=abc-123")

    assert slots["session_id"] == "abc-123"
    assert cleaned == "cache report"


def test_extract_slots_since_explicit_date() -> None:
    slots, cleaned = extract_slots("tokens per model since 2026-01-15")

    assert slots["since"] == date(2026, 1, 15)
    assert "since" not in cleaned


def test_extract_slots_since_explicit_month_defaults_to_first_day() -> None:
    slots, _cleaned = extract_slots("tokens per model since 2026-01")

    assert slots["since"] == date(2026, 1, 1)


def test_extract_slots_last_n_days() -> None:
    slots, cleaned = extract_slots("tokens per model in the last 7 days", today=date(2026, 3, 10))

    assert slots["since"] == date(2026, 3, 3)
    assert "last 7 days" not in cleaned


def test_extract_slots_last_month_is_previous_calendar_month() -> None:
    slots, cleaned = extract_slots("tokens per model last month", today=date(2026, 3, 15))

    assert slots["since"] == date(2026, 2, 1)
    assert "last month" not in cleaned


def test_extract_slots_last_month_across_year_boundary() -> None:
    slots, _cleaned = extract_slots("tokens last month", today=date(2026, 1, 15))

    assert slots["since"] == date(2025, 12, 1)


def test_extract_slots_project_whole_token_match() -> None:
    slots, cleaned = extract_slots(
        "total tokens for project billing", known_projects=["billing", "web"]
    )

    assert slots["project"] == "billing"
    assert "billing" not in cleaned


def test_extract_slots_model_whole_token_match() -> None:
    slots, cleaned = extract_slots(
        "tokens spent per model using claude-sonnet-5",
        known_models=["claude-sonnet-5", "claude-haiku-4-5"],
    )

    assert slots["model"] == "claude-sonnet-5"
    assert "claude-sonnet-5" not in cleaned


def test_extract_slots_project_does_not_match_substring() -> None:
    """A known project name must match a whole token, not a substring of an
    unrelated word (e.g. a project named "api" inside "api calls")."""
    slots, cleaned = extract_slots("how many api calls are there", known_projects=["api"])

    assert slots["project"] == "api"
    assert cleaned == "how many calls are there"


def test_extract_slots_top_n_is_limit() -> None:
    slots, cleaned = extract_slots("top 5 sessions by total tokens")

    assert slots["limit"] == 5
    assert "top 5" not in cleaned


def test_extract_slots_invalid_month_since_date_is_ignored() -> None:
    """An unparseable calendar date (bad month) must not crash -- no `since`
    slot is extracted and the question is left to refuse normally."""
    slots, cleaned = extract_slots("tokens since 2026-13-45")

    assert "since" not in slots
    assert cleaned == "tokens since 2026-13-45"


def test_extract_slots_invalid_day_since_date_is_ignored() -> None:
    slots, _cleaned = extract_slots("tokens since 2026-02-30")

    assert "since" not in slots


def test_extract_slots_valid_since_date_still_binds() -> None:
    slots, cleaned = extract_slots("tokens since 2026-02-28")

    assert slots["since"] == date(2026, 2, 28)
    assert "since" not in cleaned


def test_extract_slots_top_n_survives_project_named_top() -> None:
    """A project literally named "top" must not swallow the `top N` limit
    token before the limit pattern gets a chance to match."""
    slots, cleaned = extract_slots("top 10 sessions", known_projects=["top"])

    assert slots["limit"] == 10
    assert "project" not in slots
    assert "top 10" not in cleaned


def test_extract_slots_known_value_matches_non_word_edge() -> None:
    """A known project/model value like "c++" has a non-word edge char, so
    the whole-token match must not require a \\b there."""
    slots, cleaned = extract_slots("show me tokens for c++", known_projects=["c++"])

    assert slots["project"] == "c++"
    assert "c++" not in cleaned


def test_extract_slots_known_value_no_substring_false_positive() -> None:
    """ "c++" must not falsely match inside an unrelated token like "cc++"."""
    slots, cleaned = extract_slots("tokens for cc++", known_projects=["c++"])

    assert "project" not in slots
    assert cleaned == "tokens for cc++"


def test_extract_slots_no_filters_present() -> None:
    slots, cleaned = extract_slots("how many sessions are there")

    assert slots == {}
    assert cleaned == "how many sessions are there"


# --------------------------------------------------------------------------
# check_bindable / UnboundFilterError
# --------------------------------------------------------------------------


def test_check_bindable_no_unbindable_slots_when_declared() -> None:
    example = NlpExample(
        example_id=1,
        question="cache report",
        sql_text="SELECT 1 WHERE session_id = %s",
        slots=(SlotSpec(name="session_id", type="session_id"),),
    )

    assert check_bindable(example, {"session_id": "abc-123"}) == []


def test_check_bindable_flags_undeclared_since_filter() -> None:
    """The core regression this PR fixes: a "last month" filter on a
    template with no time slot must be refused, not silently dropped."""
    example = NlpExample(
        example_id=1,
        question="how many tokens were spent per model",
        sql_text="SELECT model, sum(output_tokens) FROM claude.api_calls GROUP BY model",
        slots=(),
    )

    unbindable = check_bindable(example, {"since": date(2026, 2, 1)})

    assert unbindable == ["since"]


def test_check_bindable_flags_multiple_undeclared_slots() -> None:
    example = NlpExample(example_id=1, question="q", sql_text="SELECT 1", slots=())

    unbindable = check_bindable(example, {"since": date(2026, 2, 1), "project": "billing"})

    assert set(unbindable) == {"since", "project"}


def test_unbound_filter_error_carries_slot_names() -> None:
    exc = UnboundFilterError(["since", "project"])

    assert exc.unbindable == ["since", "project"]


# --------------------------------------------------------------------------
# count_placeholders
# --------------------------------------------------------------------------


def test_count_placeholders_none() -> None:
    assert count_placeholders("SELECT 1") == 0


def test_count_placeholders_one() -> None:
    assert count_placeholders("SELECT * FROM t WHERE a = %s") == 1


def test_count_placeholders_multiple() -> None:
    assert count_placeholders("SELECT * FROM t WHERE a = %s AND b = %s AND c = %s") == 3


def test_count_placeholders_ignores_literal_inside_string() -> None:
    sql = "SELECT * FROM t WHERE x = %s AND y LIKE '%s%'"

    assert count_placeholders(sql) == 1


# --------------------------------------------------------------------------
# filter_examples
# --------------------------------------------------------------------------


def _mixed_examples() -> list[NlpExample]:
    return [
        NlpExample(
            example_id=1,
            question="calls for this session",
            sql_text="SELECT 1",
            tags=("session_scoped",),
        ),
        NlpExample(
            example_id=2,
            question="total sessions",
            sql_text="SELECT 2",
            tags=(),
        ),
        NlpExample(
            example_id=3,
            question="tokens for this session",
            sql_text="SELECT 3",
            tags=("session_scoped", "tokens"),
        ),
        NlpExample(
            example_id=4,
            question="total projects",
            sql_text="SELECT 4",
            tags=("projects",),
        ),
    ]


def test_filter_examples_session_scoped_true_keeps_tagged() -> None:
    filtered = filter_examples(_mixed_examples(), session_scoped=True)

    assert [example.example_id for example in filtered] == [1, 3]


def test_filter_examples_session_scoped_false_keeps_untagged() -> None:
    filtered = filter_examples(_mixed_examples(), session_scoped=False)

    assert [example.example_id for example in filtered] == [2, 4]


# --------------------------------------------------------------------------
# CatalogRetriever.search
# --------------------------------------------------------------------------


class _FakeBackend:
    """Deterministic fake encoder: maps known strings to fixed unit vectors."""

    def __init__(self, vectors: dict[str, list[float]], default: list[float] | None = None) -> None:
        self._vectors = vectors
        self._default = default or [0.0, 0.0, 1.0]

    def encode(self, texts: list[str]) -> np.ndarray:
        rows = [self._vectors.get(text, self._default) for text in texts]
        return np.asarray(rows, dtype=np.float32)


def _examples() -> list[NlpExample]:
    return [
        NlpExample(example_id=1, question="how many sessions", sql_text="SELECT 1", tags=("a",)),
        NlpExample(example_id=2, question="how many projects", sql_text="SELECT 2", tags=("b",)),
        NlpExample(example_id=3, question="total tokens used", sql_text="SELECT 3", tags=()),
    ]


def test_catalog_retriever_ranks_by_similarity() -> None:
    vectors = {
        "how many sessions": [1.0, 0.0, 0.0],
        "how many projects": [0.9, 0.1, 0.0],
        "total tokens used": [0.0, 1.0, 0.0],
        "sessions count?": [1.0, 0.0, 0.0],
    }
    backend = _FakeBackend(vectors)
    retriever = CatalogRetriever(_examples(), backend)

    ranked = retriever.search("sessions count?", top_k=3)

    assert [example.example_id for example, _ in ranked] == [1, 2, 3]
    assert ranked[0][1] == pytest.approx(1.0, abs=1e-5)


def test_catalog_retriever_top_k_truncation() -> None:
    vectors = {
        "how many sessions": [1.0, 0.0, 0.0],
        "how many projects": [0.9, 0.1, 0.0],
        "total tokens used": [0.0, 1.0, 0.0],
        "sessions count?": [1.0, 0.0, 0.0],
    }
    backend = _FakeBackend(vectors)
    retriever = CatalogRetriever(_examples(), backend)

    ranked = retriever.search("sessions count?", top_k=1)

    assert len(ranked) == 1
    assert ranked[0][0].example_id == 1


def test_catalog_retriever_confidence_threshold() -> None:
    vectors = {
        "how many sessions": [1.0, 0.0, 0.0],
        "how many projects": [0.0, 1.0, 0.0],
        "total tokens used": [0.0, 0.0, 1.0],
        "completely unrelated gibberish": [-1.0, -1.0, -1.0],
    }
    backend = _FakeBackend(vectors)
    retriever = CatalogRetriever(_examples(), backend)

    ranked = retriever.search("completely unrelated gibberish", top_k=3)
    top_score = ranked[0][1]

    assert retriever.is_confident(top_score) is False
    assert retriever.is_confident(0.99) is True


def test_catalog_retriever_empty_examples_returns_empty() -> None:
    retriever = CatalogRetriever([], _FakeBackend({}))
    assert retriever.search("anything") == []


# --------------------------------------------------------------------------
# LocalEmbeddingBackend
# --------------------------------------------------------------------------


def test_local_embedding_backend_raises_when_dependency_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins

    real_import = builtins.__import__

    def _fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "sentence_transformers":
            raise ImportError("no module named sentence_transformers")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    backend = LocalEmbeddingBackend()
    with pytest.raises(NlpDependencyError):
        backend.encode(["hello"])


def _seed_hf_cache(hf_home: Path, repo_id: str) -> None:
    snapshots = hf_home / "hub" / f"models--{repo_id.replace('/', '--')}" / "snapshots"
    (snapshots / "deadbeef").mkdir(parents=True)


def test_is_cached_true_when_snapshot_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    _seed_hf_cache(tmp_path, "sentence-transformers/all-MiniLM-L6-v2")

    assert LocalEmbeddingBackend()._is_cached() is True


def test_is_cached_false_when_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path))

    assert LocalEmbeddingBackend()._is_cached() is False


def test_load_sets_offline_env_when_model_cached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the model is cached, _load() forces HF offline mode before importing
    sentence_transformers, so no HF Hub request (and its HF_TOKEN warning) fires."""
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)
    _seed_hf_cache(tmp_path, "sentence-transformers/all-MiniLM-L6-v2")

    seen: dict[str, str | None] = {}

    class _FakeSentenceTransformer:
        def __init__(self, model_name: str) -> None:
            seen["HF_HUB_OFFLINE"] = os.environ.get("HF_HUB_OFFLINE")

    fake_module = type("M", (), {"SentenceTransformer": _FakeSentenceTransformer})
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module)

    LocalEmbeddingBackend()._load()

    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["TRANSFORMERS_OFFLINE"] == "1"
    assert seen["HF_HUB_OFFLINE"] == "1"  # set before the import saw it


def test_load_leaves_hub_reachable_when_model_uncached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """On a fresh machine (no cached snapshot) offline mode is not forced, so the
    one-time download can proceed."""
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)

    class _FakeSentenceTransformer:
        def __init__(self, model_name: str) -> None:
            pass

    fake_module = type("M", (), {"SentenceTransformer": _FakeSentenceTransformer})
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module)

    LocalEmbeddingBackend()._load()

    assert "HF_HUB_OFFLINE" not in os.environ


# --------------------------------------------------------------------------
# load_examples
# --------------------------------------------------------------------------


def test_load_examples_maps_rows() -> None:
    rows = [
        {
            "example_id": 1,
            "question": "how many sessions",
            "sql_text": "SELECT count(*) FROM claude.sessions",
            "description": "count sessions",
            "tags": ["sessions", "count"],
        },
        {
            "example_id": "2",
            "question": "total tokens",
            "sql_text": "SELECT sum(output_tokens) FROM claude.api_calls",
            "description": None,
            "tags": None,
        },
    ]

    examples = load_examples(rows)

    assert examples == [
        NlpExample(
            example_id=1,
            question="how many sessions",
            sql_text="SELECT count(*) FROM claude.sessions",
            description="count sessions",
            tags=("sessions", "count"),
        ),
        NlpExample(
            example_id=2,
            question="total tokens",
            sql_text="SELECT sum(output_tokens) FROM claude.api_calls",
            description=None,
            tags=(),
        ),
    ]


# --------------------------------------------------------------------------
# Catalog invariant: every row's declared slot count matches its %s count.
#
# Parses sql/005_nlp_catalog.sql directly rather than querying a live DB, so
# it runs in the default (no-DB, no-network) test path but still checks the
# exact file that ships to both fresh and migrated installs.
# --------------------------------------------------------------------------

_CATALOG_SQL_PATH = Path(__file__).parent.parent / "sql" / "005_nlp_catalog.sql"


def _parse_catalog_rows() -> list[tuple[str, list[dict[str, str]]]]:
    """Return (sql_text, slots) for every row in the 005 catalog INSERT."""
    import json

    text = _CATALOG_SQL_PATH.read_text()
    _, rest = text.split("VALUES\n", 1)
    body, _ = rest.rsplit("\nON CONFLICT", 1)
    rows = []
    for chunk in re.split(r"\n\),\n\(\n", body):
        # sql_text is the *first* single-quoted literal in the tuple (the
        # question is the literal before it).
        first_literal = re.match(
            r"\(?\s*'((?:[^']|'')*)',\s*\n\s*'((?:[^']|'')*(?:\n(?:[^']|'')*)*?)',",
            chunk,
            re.DOTALL,
        )
        assert first_literal, f"could not parse a catalog row:\n{chunk[:200]}"
        sql_text = first_literal.group(2).replace("''", "'")
        slots_match = re.search(r"('\[.*?\]')::jsonb", chunk, re.DOTALL)
        assert slots_match, f"row has no slots literal:\n{chunk[:200]}"
        slots = json.loads(slots_match.group(1)[1:-1])
        rows.append((sql_text, slots))
    return rows


def test_catalog_slot_count_matches_placeholder_count() -> None:
    rows = _parse_catalog_rows()
    assert len(rows) == 28, "unexpected catalog row count -- update this test if intentional"
    for sql_text, slots in rows:
        assert count_placeholders(sql_text) == len(slots), (
            f"placeholder/slot mismatch ({count_placeholders(sql_text)} vs "
            f"{len(slots)} slots) in: {sql_text[:80]!r}"
        )


# --------------------------------------------------------------------------
# CLI `ask`
# --------------------------------------------------------------------------


class _FakeConnection:
    def execute(self, *args: Any, **kwargs: Any) -> None:
        return None


@contextmanager
def _fake_connect(database_url: str, **kwargs: Any):
    yield _FakeConnection()


def _common_fetch_all(conn: Any, sql: str, *a: Any, **k: Any) -> list[dict[str, Any]]:
    if "DISTINCT dir_name" in sql or "DISTINCT model" in sql:
        return []
    return [
        {
            "example_id": 1,
            "question": "how many sessions",
            "sql_text": "SELECT count(*) FROM claude.sessions",
            "description": None,
            "tags": [],
            "slots": [],
        }
    ]


def _patch_common(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_mod, "connect", _fake_connect)
    monkeypatch.setattr(
        cli_mod,
        "get_settings",
        lambda: type("S", (), {"database_url": "postgresql://fake"})(),
    )
    monkeypatch.setattr(cli_mod, "fetch_all", _common_fetch_all)


def test_ask_cli_sql_no_execute_prints_matched_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_common(monkeypatch)
    backend = _FakeBackend(
        {
            "how many sessions": [1.0, 0.0],
            "how many sessions do we have?": [1.0, 0.0],
        }
    )
    monkeypatch.setattr(nlp_mod, "LocalEmbeddingBackend", lambda: backend)

    result = runner.invoke(
        cli_mod.app,
        ["ask", "how many sessions do we have?", "--sql", "--no-execute"],
    )

    assert result.exit_code == 0, result.output
    assert "Matched:" in result.output
    assert "SELECT count(*) FROM claude.sessions" in result.output


def test_ask_cli_refuses_undeclared_time_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression this PR fixes: "last month" must not silently match the
    all-time template and return unfiltered numbers."""

    def _fetch_all(conn: Any, sql: str, *a: Any, **k: Any) -> list[dict[str, Any]]:
        if "DISTINCT dir_name" in sql or "DISTINCT model" in sql:
            return []
        return [
            {
                "example_id": 1,
                "question": "how many tokens were spent per model",
                "sql_text": "SELECT model, sum(output_tokens) FROM claude.api_calls GROUP BY model",
                "description": None,
                "tags": [],
                "slots": [],
            }
        ]

    monkeypatch.setattr(cli_mod, "connect", _fake_connect)
    monkeypatch.setattr(
        cli_mod,
        "get_settings",
        lambda: type("S", (), {"database_url": "postgresql://fake"})(),
    )
    monkeypatch.setattr(cli_mod, "fetch_all", _fetch_all)
    backend = _FakeBackend(
        {
            "how many tokens were spent per model": [1.0, 0.0],
            # "last month" is stripped by extract_slots before the retriever
            # ever sees the question -- this is the cleaned residual text.
            "tokens per model": [1.0, 0.0],
        }
    )
    monkeypatch.setattr(nlp_mod, "LocalEmbeddingBackend", lambda: backend)

    result = runner.invoke(cli_mod.app, ["ask", "tokens per model last month"])

    assert result.exit_code == 1
    assert "Refusing" in result.output
    assert "since" in result.output


def test_ask_cli_low_confidence_prints_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_common(monkeypatch)
    backend = _FakeBackend(
        {
            "how many sessions": [1.0, 0.0],
            "completely unrelated gibberish": [-1.0, 0.0],
        }
    )
    monkeypatch.setattr(nlp_mod, "LocalEmbeddingBackend", lambda: backend)

    result = runner.invoke(cli_mod.app, ["ask", "completely unrelated gibberish"])

    assert result.exit_code == 1
    assert "Not confident enough" in result.output
    assert "how many sessions" in result.output


# --------------------------------------------------------------------------
# CLI `ask` - session-scoped param wiring / DB error handling
# --------------------------------------------------------------------------


_SESSION_ID = "11111111-1111-1111-1111-111111111111"
_SESSION_SQL = "SELECT * FROM claude.api_calls WHERE session_id = %s"


def _patch_session_scoped(monkeypatch: pytest.MonkeyPatch, fetch_all_impl: Any) -> None:
    monkeypatch.setattr(cli_mod, "connect", _fake_connect)
    monkeypatch.setattr(
        cli_mod,
        "get_settings",
        lambda: type("S", (), {"database_url": "postgresql://fake"})(),
    )
    monkeypatch.setattr(cli_mod, "fetch_all", fetch_all_impl)
    backend = _FakeBackend(
        {
            "cache report": [1.0, 0.0],
        }
    )
    monkeypatch.setattr(nlp_mod, "LocalEmbeddingBackend", lambda: backend)


def test_ask_cli_session_scoped_sizes_params_from_placeholders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The (session_id,) * n params tuple handed to fetch_all matches the
    query's placeholder count, for a session-scoped question."""
    calls: list[tuple[str, Any]] = []

    def _fetch_all(conn: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
        if "nlp_examples" in sql:
            return [
                {
                    "example_id": 1,
                    "question": "cache report",
                    "sql_text": _SESSION_SQL,
                    "description": None,
                    "tags": ["session_scoped"],
                    "slots": [{"name": "session_id", "type": "session_id"}],
                }
            ]
        if "DISTINCT dir_name" in sql or "DISTINCT model" in sql:
            return []
        calls.append((sql, params))
        return []

    _patch_session_scoped(monkeypatch, _fetch_all)

    result = runner.invoke(
        cli_mod.app,
        ["ask", f"cache report session_id={_SESSION_ID}"],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    executed_sql, params = calls[0]
    assert executed_sql == _SESSION_SQL
    assert params == (_SESSION_ID,)


def test_ask_cli_db_error_caught_and_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A psycopg.Error raised by fetch_all when running the matched query is
    caught and surfaces as a typer.Exit, not a raw traceback."""

    def _fetch_all(conn: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
        if "nlp_examples" in sql:
            return [
                {
                    "example_id": 1,
                    "question": "cache report",
                    "sql_text": _SESSION_SQL,
                    "description": None,
                    "tags": ["session_scoped"],
                    "slots": [{"name": "session_id", "type": "session_id"}],
                }
            ]
        if "DISTINCT dir_name" in sql or "DISTINCT model" in sql:
            return []
        raise psycopg.errors.InvalidTextRepresentation("invalid input syntax for type uuid")

    _patch_session_scoped(monkeypatch, _fetch_all)

    result = runner.invoke(
        cli_mod.app,
        ["ask", f"cache report session_id={_SESSION_ID}"],
    )

    assert result.exit_code == 1
    assert "Query failed" in result.output
