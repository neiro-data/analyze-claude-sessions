"""`acp ask --llm`: prompt building, SQL guards and CLI gating. No network."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from analyze_claude_sessions import cli as cli_mod
from analyze_claude_sessions import llm as llm_mod
from analyze_claude_sessions import nlp as nlp_mod
from analyze_claude_sessions.nlp import NlpExample, UnsafeQueryError

runner = CliRunner()


class _FakeClient:
    def __init__(self, sql: str = "SELECT 1 AS n", summary: str = "one") -> None:
        self.calls: list[dict[str, Any]] = []
        self._sql = sql
        self._summary = summary
        self.messages = self

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        text = (
            json.dumps({"sql": self._sql, "explanation": "x"})
            if "output_config" in kwargs and "format" in kwargs["output_config"]
            else self._summary
        )
        return SimpleNamespace(
            stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)]
        )


def _schema_rows() -> list[dict[str, Any]]:
    def row(obj: str, col: str) -> dict[str, Any]:
        return {
            "object_name": obj,
            "object_type": "view",
            "column_name": col,
            "data_type": "text",
            "column_comment": None,
        }

    return [row("v_cost", "model"), row("v_cost", "usd"), row("unrelated", "blob")]


def _ranked() -> list[tuple[NlpExample, float]]:
    ex = NlpExample(1, "cost per model", "SELECT model, sum(usd) FROM claude.v_cost GROUP BY 1")
    return [(ex, 0.9)]


def test_context_has_examples_and_only_relevant_capped_schema() -> None:
    ctx = llm_mod.build_context("cost per model", _ranked(), _schema_rows())
    assert "cost per model" in ctx.examples_block
    assert "claude.v_cost" in ctx.schema_block
    assert "unrelated" not in ctx.schema_block
    capped = llm_mod.build_context("cost per model", _ranked(), _schema_rows(), max_schema_rows=1)
    assert capped.schema_rows <= 1


def test_generate_sql_wraps_missing_limit() -> None:
    ctx = llm_mod.build_context("q", _ranked(), _schema_rows())
    sql = llm_mod.generate_sql(_FakeClient("SELECT 1 AS n;"), "m", "q", ctx)
    assert sql == "SELECT * FROM (SELECT 1 AS n) AS q LIMIT 200"


def test_generate_sql_keeps_existing_limit() -> None:
    ctx = llm_mod.build_context("q", _ranked(), _schema_rows())
    assert (
        llm_mod.generate_sql(_FakeClient("SELECT 1 LIMIT 5"), "m", "q", ctx) == "SELECT 1 LIMIT 5"
    )


@pytest.mark.parametrize(
    "bad", ["DROP TABLE claude.sessions", "SELECT 1; SELECT 2", "DELETE FROM claude.sessions"]
)
def test_generate_sql_refuses_unsafe(bad: str) -> None:
    ctx = llm_mod.build_context("q", _ranked(), _schema_rows())
    with pytest.raises(UnsafeQueryError):
        llm_mod.generate_sql(_FakeClient(bad), "m", "q", ctx)


# Every statement the CLI sends, in order: `execute` calls and `fetch_all` queries.
_STATEMENTS: list[str] = []


_EXEC_ARGS: list[tuple[Any, ...]] = []


class _Cursor:
    def fetchall(self) -> list[dict[str, Any]]:
        return [{"n": 1}]


class _Conn:
    def execute(self, sql: str, *a: Any, **k: Any) -> _Cursor:
        _STATEMENTS.append(sql)
        _EXEC_ARGS.append(a)
        return _Cursor()


@contextmanager
def _connect(url: str, **kw: Any) -> Iterator[_Conn]:
    _STATEMENTS.append("<connect>")
    yield _Conn()


def _settings(**over: Any) -> Any:
    base = {
        "database_url": "postgresql://owner",
        "ask_database_url": "postgresql://ask",
        "ask_database_url_explicit": True,
        "ask_llm_model": "claude-opus-5-5",
        "anthropic_api_key_set": True,
    }
    return SimpleNamespace(**(base | over))


def _patch(
    monkeypatch: pytest.MonkeyPatch, client: _FakeClient, role: str = "ask_reader", **over: Any
) -> None:
    _STATEMENTS.clear()
    monkeypatch.setattr(cli_mod, "get_settings", lambda: _settings(**over))
    monkeypatch.setattr(cli_mod, "connect", _connect)
    monkeypatch.setattr(llm_mod, "make_client", lambda: client)

    def fetch_all(conn: Any, sql: str, *a: Any, **k: Any) -> list[dict[str, Any]]:
        _STATEMENTS.append(sql)
        if "pg_roles" in sql:
            return [{"rolname": role, "rolsuper": False, "owns_claude": role == "owner"}]
        if "DISTINCT" in sql:
            return []
        if "v_schema_catalog" in sql:
            return _schema_rows()
        if "nlp_examples" in sql:
            return [
                {
                    "example_id": 1,
                    "question": "cost per model",
                    "sql_text": "SELECT model FROM claude.v_cost",
                    "description": None,
                    "tags": [],
                    "slots": [],
                    "paraphrases": [],
                }
            ]
        return [{"n": 1}]

    monkeypatch.setattr(cli_mod, "fetch_all", fetch_all)

    class _Backend:
        model_name = "fake"

        def encode(self, texts: list[str]) -> Any:
            import numpy as np

            return np.ones((len(texts), 2), dtype="float32")

    monkeypatch.setattr(nlp_mod, "LocalEmbeddingBackend", _Backend)


def test_llm_runs_prints_sql_and_summarizes(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    _patch(monkeypatch, client)
    result = runner.invoke(cli_mod.app, ["ask", "--llm", "--summarize", "cost per model"])
    assert result.exit_code == 0, result.output
    assert "LIMIT 200" in result.output
    assert "one" in result.output
    assert len(client.calls) == 2
    assert "cost per model" in client.calls[0]["messages"][0]["content"]


def test_llm_refuses_without_ask_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    _patch(
        monkeypatch,
        client,
        ask_database_url="postgresql://owner",
        ask_database_url_explicit=False,
    )
    result = runner.invoke(cli_mod.app, ["ask", "--llm", "cost per model"])
    assert result.exit_code == 1
    assert "ASK_DATABASE_URL" in result.output
    assert client.calls == []


def test_llm_refuses_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    _patch(monkeypatch, client, anthropic_api_key_set=False)
    result = runner.invoke(cli_mod.app, ["ask", "--llm", "cost per model"])
    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY" in result.output
    assert client.calls == []


def test_llm_no_execute_stops_after_printing_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, _FakeClient())
    result = runner.invoke(cli_mod.app, ["ask", "--llm", "--no-execute", "cost per model"])
    assert result.exit_code == 0, result.output
    assert "LIMIT 200" in result.output
    assert not any(s.startswith("SELECT * FROM (") for s in _STATEMENTS)


def test_default_ask_makes_no_client_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> Any:
        raise AssertionError("default ask must not build an LLM client")

    _patch(monkeypatch, _FakeClient())
    monkeypatch.setattr(llm_mod, "make_client", boom)
    result = runner.invoke(cli_mod.app, ["ask", "cost per model"])
    assert result.exception is None, result.output
    assert "Matched:" in result.output


def test_llm_refuses_when_url_differs_but_not_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    _patch(
        monkeypatch, client, ask_database_url="postgresql://other", ask_database_url_explicit=False
    )
    result = runner.invoke(cli_mod.app, ["ask", "--llm", "cost per model"])
    assert result.exit_code == 1
    assert "ASK_DATABASE_URL" in result.output
    assert client.calls == []


def test_llm_refuses_owner_role(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    _patch(monkeypatch, client, role="owner")
    result = runner.invoke(cli_mod.app, ["ask", "--llm", "cost per model"])
    assert result.exit_code == 1
    assert "refuses" in result.output
    assert client.calls == []


def test_llm_sets_read_only_first_in_every_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, _FakeClient())
    result = runner.invoke(cli_mod.app, ["ask", "--llm", "cost per model"])
    assert result.exit_code == 0, result.output
    connects = [i for i, s in enumerate(_STATEMENTS) if s == "<connect>"]
    assert len(connects) == 2  # context fetch, then execution; none open during the API call
    for i in connects:
        assert _STATEMENTS[i + 1] == "SET TRANSACTION READ ONLY"
        assert _STATEMENTS[i + 2].startswith("SET LOCAL statement_timeout")
    assert any("paraphrases" in s for s in _STATEMENTS)


def test_no_execute_opens_only_the_context_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, _FakeClient())
    result = runner.invoke(cli_mod.app, ["ask", "--llm", "--no-execute", "cost per model"])
    assert result.exit_code == 0, result.output
    assert _STATEMENTS.count("<connect>") == 1


@pytest.mark.parametrize(
    "bad",
    [
        "SELECT * FROM claude.transcripts",
        "SELECT * FROM claude.interactions_upload",
        "SELECT * FROM pg_catalog.pg_authid",
        "SELECT * FROM v_cost",
        "SELECT * FROM public.secrets",
    ],
)
def test_generate_sql_refuses_unlisted_relations(bad: str) -> None:
    ctx = llm_mod.build_context("cost per model", _ranked(), _schema_rows())
    with pytest.raises(UnsafeQueryError):
        llm_mod.generate_sql(_FakeClient(bad), "m", "q", ctx)


def test_generate_sql_allows_listed_relation_and_own_cte() -> None:
    ctx = llm_mod.build_context("cost per model", _ranked(), _schema_rows())
    sql = "WITH c AS (SELECT model FROM claude.v_cost) SELECT * FROM c"
    assert llm_mod.generate_sql(_FakeClient(sql), "m", "q", ctx).endswith("LIMIT 200")


@pytest.mark.parametrize(
    "fn",
    [
        "pg_advisory_lock(1)",
        "query_to_xml('select 1', true, false, '')",
        "lo_get(1)",
        "pg_ls_logdir()",
    ],
)
def test_prefix_blocklist(fn: str) -> None:
    with pytest.raises(UnsafeQueryError):
        nlp_mod.ensure_read_only(f"SELECT {fn}")


def test_llm_api_error_exits_1_without_template_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Down(_FakeClient):
        def create(self, **kwargs: Any) -> Any:
            raise ConnectionError("network down")

    _patch(monkeypatch, _Down())

    def no_template(*a: Any, **k: Any) -> Any:
        raise AssertionError("template path must not run after an LLM failure")

    monkeypatch.setattr(nlp_mod, "resolve_ask", no_template)
    result = runner.invoke(cli_mod.app, ["ask", "--llm", "cost per model"])
    assert result.exit_code == 1
    assert "network down" in result.output


@pytest.mark.parametrize("limit", ["ALL", "100000000"])
def test_cap_rows_wraps_unbounded_limits(limit: str) -> None:
    assert llm_mod.cap_rows(f"SELECT 1 LIMIT {limit}").endswith("LIMIT 200")


def test_generated_sql_with_percent_runs_without_params(monkeypatch: pytest.MonkeyPatch) -> None:
    _EXEC_ARGS.clear()
    _patch(monkeypatch, _FakeClient("SELECT model FROM claude.v_cost WHERE model LIKE '%opus%'"))
    result = runner.invoke(cli_mod.app, ["ask", "--llm", "cost per model"])
    assert result.exit_code == 0, result.output
    assert _EXEC_ARGS[-1] == ()  # executed with no params, so `%` stays literal


def test_generated_sql_is_printed_without_markup_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, _FakeClient('SELECT (ARRAY[1])[1] AS "[/x]"'))
    result = runner.invoke(cli_mod.app, ["ask", "--llm", "--no-execute", "cost per model"])
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_call_rejects_refusal_and_truncation(stop: str) -> None:
    class _Stop(_FakeClient):
        def create(self, **kwargs: Any) -> Any:
            return SimpleNamespace(stop_reason=stop, content=[])

    ctx = llm_mod.build_context("q", _ranked(), _schema_rows())
    with pytest.raises(llm_mod.LlmError):
        llm_mod.generate_sql(_Stop(), "m", "q", ctx)


def test_generate_sql_refuses_pg_sleep() -> None:
    ctx = llm_mod.build_context("q", _ranked(), _schema_rows())
    with pytest.raises(UnsafeQueryError):
        llm_mod.generate_sql(_FakeClient("SELECT pg_sleep(10)"), "m", "q", ctx)


@pytest.mark.slow
@pytest.mark.skipif(not os.getenv("ANTHROPIC_API_KEY"), reason="needs ANTHROPIC_API_KEY")
def test_live_generate_sql_smoke() -> None:
    ctx = llm_mod.build_context("how many sessions are there", _ranked(), _schema_rows())
    sql = llm_mod.generate_sql(llm_mod.make_client(), llm_mod.DEFAULT_MODEL, "count sessions", ctx)
    assert sql.lower().startswith("select")
