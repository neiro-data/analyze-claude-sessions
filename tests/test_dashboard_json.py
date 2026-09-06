"""Structural checks for the provisioned Grafana dashboard JSON files.

These don't touch Grafana or Postgres -- they just guard against JSON
authoring mistakes (bad gridPos math, a forgotten filter on a new panel,
duplicate ids) that are easy to make when hand-editing the dashboards.
"""

import json
from pathlib import Path

import pytest

DASHBOARDS_DIR = Path(__file__).parent.parent / "docker" / "grafana" / "dashboards"
DASHBOARD_PATHS = sorted(DASHBOARDS_DIR.glob("*.json"))

OVERVIEW_PATH = DASHBOARDS_DIR / "overview.json"
REQUIRED_OVERVIEW_PREDICATES = ("model", "agent", "small_sessions")


@pytest.mark.parametrize("path", DASHBOARD_PATHS, ids=lambda p: p.name)
def test_dashboard_json_parses(path: Path) -> None:
    json.loads(path.read_text())


@pytest.mark.parametrize("path", DASHBOARD_PATHS, ids=lambda p: p.name)
def test_panel_ids_unique(path: Path) -> None:
    dashboard = json.loads(path.read_text())
    ids = [panel["id"] for panel in dashboard["panels"]]
    assert len(ids) == len(set(ids)), f"duplicate panel id in {path.name}: {ids}"


def _rects_overlap(a: dict[str, int], b: dict[str, int]) -> bool:
    return bool(
        a["x"] < b["x"] + b["w"]
        and b["x"] < a["x"] + a["w"]
        and a["y"] < b["y"] + b["h"]
        and b["y"] < a["y"] + a["h"]
    )


@pytest.mark.parametrize("path", DASHBOARD_PATHS, ids=lambda p: p.name)
def test_panel_gridpos_does_not_overlap(path: Path) -> None:
    dashboard = json.loads(path.read_text())
    panels = dashboard["panels"]
    for i, p in enumerate(panels):
        for q in panels[i + 1 :]:
            assert not _rects_overlap(p["gridPos"], q["gridPos"]), (
                f"{path.name}: panels {p['id']} and {q['id']} overlap"
            )


def test_overview_has_agent_and_small_sessions_template_vars() -> None:
    dashboard = json.loads(OVERVIEW_PATH.read_text())
    names = {v["name"] for v in dashboard["templating"]["list"]}
    assert {"model", "agent", "small_sessions"} <= names


def test_overview_every_target_has_all_three_predicates() -> None:
    dashboard = json.loads(OVERVIEW_PATH.read_text())
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            sql = target["rawSql"]
            for var in REQUIRED_OVERVIEW_PREDICATES:
                assert f"${{{var}" in sql or f"${var}" in sql, (
                    f"panel {panel['id']} ({panel['title']!r}) target {target['refId']} "
                    f"is missing the '{var}' predicate"
                )
