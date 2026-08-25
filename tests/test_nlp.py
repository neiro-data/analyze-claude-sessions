from __future__ import annotations

from contextlib import contextmanager
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
    UnsafeQueryError,
    count_placeholders,
    ensure_read_only,
    extract_session_id,
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


def test_extract_session_id_handles_spaces_around_equals() -> None:
    session_id, cleaned = extract_session_id("cache report session_id = abc-123")

    assert session_id == "abc-123"
    assert cleaned == "cache report"


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
# CLI `ask`
# --------------------------------------------------------------------------


class _FakeConnection:
    def execute(self, *args: Any, **kwargs: Any) -> None:
        return None


@contextmanager
def _fake_connect(database_url: str, **kwargs: Any):
    yield _FakeConnection()


def _patch_common(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_mod, "connect", _fake_connect)
    monkeypatch.setattr(
        cli_mod,
        "get_settings",
        lambda: type("S", (), {"database_url": "postgresql://fake"})(),
    )
    monkeypatch.setattr(
        cli_mod,
        "fetch_all",
        lambda conn, sql, *a, **k: [
            {
                "example_id": 1,
                "question": "how many sessions",
                "sql_text": "SELECT count(*) FROM claude.sessions",
                "description": None,
                "tags": [],
            }
        ],
    )


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


def _patch_session_scoped(
    monkeypatch: pytest.MonkeyPatch, fetch_all_impl: Any
) -> None:
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
                }
            ]
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
                }
            ]
        raise psycopg.errors.InvalidTextRepresentation("invalid input syntax for type uuid")

    _patch_session_scoped(monkeypatch, _fetch_all)

    result = runner.invoke(
        cli_mod.app,
        ["ask", f"cache report session_id={_SESSION_ID}"],
    )

    assert result.exit_code == 1
    assert "Query failed" in result.output
