"""Fast tests for `scripts/rag_stress.py` using `FakeHashBackend` only (no real model)."""

from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from typer.testing import CliRunner

from analyze_claude_sessions.nlp import DEFAULT_MODEL_NAME, Backend
from analyze_claude_sessions.nlp_stress import (
    COMPARISON_SWEEP,
    CONFIDENCE_SWEEP,
    FakeHashBackend,
    SqlCase,
)

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "rag_stress.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("rag_stress_script", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rag = _load_script()


def _factory(name: str) -> Backend:
    return FakeHashBackend(dim=32 if name == "a" else 64)


@pytest.fixture(scope="module")
def two_model_report() -> Any:
    return rag.run_battery(seed=1, quick=True, models=["a", "b"], backend_factory=_factory)


def test_run_battery_two_models(two_model_report: Any) -> None:
    report = two_model_report
    assert [m.model_name for m in report.models] == ["a", "b"]
    assert [m.embed_dim for m in report.models] == [32, 64]
    assert [t for t, _, _ in report.models[0].threshold_sweep] == list(COMPARISON_SWEEP)
    assert report.latency_by_size  # shared sections computed once
    assert report.models[0].real_latency is None  # quick mode


def test_single_model_uses_default_sweep() -> None:
    report = rag.run_battery(seed=1, quick=True, models=["a"], backend_factory=_factory)
    assert [t for t, _, _ in report.models[0].threshold_sweep] == list(CONFIDENCE_SWEEP)


def test_write_csv_has_model_column(two_model_report: Any, tmp_path: Path) -> None:
    path = tmp_path / "r.csv"
    rag.write_csv(two_model_report, path)
    with path.open() as f:
        rows = list(csv.DictReader(f))
    assert list(rows[0]) == ["model", "section", "key", "metric", "value"]
    models = {r["model"] for r in rows}
    assert models == {"a", "b", "-"}
    shared = {r["section"] for r in rows if r["model"] == "-"}
    assert {"guard_regression", "slot_fuzz", "latency"} <= shared
    assert all(r["section"] != "baseline" for r in rows if r["model"] == "-")


def test_render_markdown_comparison_table(two_model_report: Any) -> None:
    md = rag.render_markdown(two_model_report)
    assert "## Model comparison" in md
    rows = [ln for ln in md.splitlines() if ln.startswith("| `")]
    assert len(rows) == 2
    assert md.count("## Guard regressions") == 1


def test_render_markdown_single_model_has_no_comparison() -> None:
    report = rag.run_battery(seed=1, quick=True, models=["a"], backend_factory=_factory)
    md = rag.render_markdown(report)
    assert "## Model comparison" not in md
    assert "## Baseline recall" in md
    assert "Near-miss questions" in md
    assert "separation AUC" in md


def test_hf_cache_size_missing_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    assert rag._hf_cache_size_mb("all-MiniLM-L6-v2") is None
    blobs = tmp_path / "hub" / "models--sentence-transformers--all-MiniLM-L6-v2" / "blobs"
    blobs.mkdir(parents=True)
    (blobs / "x").write_bytes(b"0" * 1024 * 1024)
    assert rag._hf_cache_size_mb("all-MiniLM-L6-v2") == pytest.approx(1.0)


@pytest.fixture
def captured_models(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    seen: list[list[str]] = []
    real = rag.run_battery

    def spy(**kwargs: Any) -> Any:
        seen.append(list(kwargs["models"]))
        return real(**kwargs)

    monkeypatch.setattr(rag, "run_battery", spy)
    monkeypatch.setattr(rag, "BACKEND_FACTORY", _factory)
    return seen


def _invoke(tmp_path: Path, *args: str) -> None:
    result = CliRunner().invoke(rag.app, ["--quick", "--out", str(tmp_path), *args])
    assert result.exit_code == 0, result.output


def test_cli_default_model(tmp_path: Path, captured_models: list[list[str]]) -> None:
    _invoke(tmp_path)
    assert captured_models == [[DEFAULT_MODEL_NAME]]


def test_cli_compare(tmp_path: Path, captured_models: list[list[str]]) -> None:
    _invoke(tmp_path, "--compare")
    assert captured_models == [list(rag.COMPARISON_MODELS)]


def test_cli_repeated_model_dedupes_in_order(
    tmp_path: Path, captured_models: list[list[str]]
) -> None:
    _invoke(tmp_path, "-m", "b", "--model", "a", "-m", "b")
    assert captured_models == [["b", "a"]]


def test_preflight_exits_2_on_uncached(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    result = CliRunner().invoke(rag.app, ["--quick", "--out", str(tmp_path), "--compare"])
    assert result.exit_code == 2
    assert "rag-stress-prefetch" in result.output


def test_preflight_still_runs_when_offline_flag_is_falsy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    result = CliRunner().invoke(rag.app, ["--quick", "--out", str(tmp_path), "--compare"])
    assert result.exit_code == 2


def test_known_bypass_flag_is_reported_only_when_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = (
        SqlCase("held_open", "SELECT 1", True, known_bypass=True),
        SqlCase("blocked", "DROP TABLE t", False, known_bypass=True),
    )
    monkeypatch.setattr(rag, "SQL_ADVERSARIAL_CORPUS", corpus)
    report = rag.run_battery(seed=1, quick=True, models=["a"], backend_factory=_factory)
    assert [b[0] for b in report.known_bypasses] == ["held_open"]
    assert report.guard_bypasses == []
