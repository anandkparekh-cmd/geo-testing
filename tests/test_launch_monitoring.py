import json

import numpy as np
import pandas as pd
import pytest

from geo_engine.ingest import ingest
from geo_engine.launch import export_targeting
from geo_engine.map_geo import map_geo
from geo_engine.monitoring import SpendPlan, leakage_check, pacing_report
from geo_engine.synthetic import GeoTestSpec

GROUP = ["BD", "CA", "DA", "FY", "HP", "LE", "RG", "SP", "WR", "WS"]
START = "2027-01-11"


# ---- export_targeting ----------------------------------------------------------------
@pytest.fixture(scope="module")
def export(ingested42, tmp_path_factory):
    d = ingested42.data
    controls = [a for a in d["area"].unique() if a not in GROUP]
    out = tmp_path_factory.mktemp("targeting")
    return export_targeting(d, GROUP, controls, START, 21, out, "t", cooldown_days=14), d


def test_lists_hold_exactly_the_test_districts(export):
    x, d = export
    expected = set(d.loc[d["area"].isin(GROUP), "postcode"])
    google = open(x.files["google_ads"][0]).read().split()
    meta = open(x.files["meta"][0]).read().split()
    assert set(google) == set(meta) == expected and x.action == "exclude"
    assert google[:3] == ["BD1", "BD2", "BD3"]          # natural order, not BD1, BD10, BD2


def test_assignment_and_checklist(export):
    x, _ = export
    a = pd.read_csv(x.files["assignment"][0])
    assert set(a.loc[a["group"] == "test", "area"]) == set(GROUP) and len(a) == 121
    md = open(x.files["checklist"][0]).read()
    assert "Mon 11 Jan 2027 to Sun 31 Jan 2027" in md and "cooldown to Sun 14 Feb 2027" in md
    assert "Cut each campaign's budget" in md and "Presence" in md


def test_heavy_up_targets_instead_of_excluding(ingested42, tmp_path):
    d = ingested42.data
    x = export_targeting(d, ["LS"], ["M"], START, 21, tmp_path, "hu", kind="heavy_up")
    assert x.action == "include" and "include" in x.files["google_ads"][0]
    assert "Launch the heavy-up campaign" in open(x.files["checklist"][0]).read()


def test_google_lists_split_at_1000(tmp_path):
    pcs = [f"B{i}" for i in range(1, 1501)]
    d = pd.DataFrame({"date": pd.Timestamp("2025-01-01"), "postcode": pcs + ["M1"], "area": ["B"] * 1500 + ["M"],
                      "kpi": 1.0, "spend": 1.0})
    x = export_targeting(d, ["B"], ["M"], START, 21, tmp_path, "big")
    assert len(x.files["google_ads"]) == 2 and len(open(x.files["google_ads"][1]).read().split()) == 500


def test_area_level_data_and_overlap_rejected(tmp_path):
    d = pd.DataFrame({"date": pd.Timestamp("2025-01-01"), "postcode": ["B", "M"], "area": ["B", "M"],
                      "kpi": 1.0, "spend": 1.0})
    with pytest.raises(ValueError, match="districts"):
        export_targeting(d, ["B"], ["M"], START, 21, tmp_path)
    with pytest.raises(ValueError, match="both test and control"):
        export_targeting(d, ["B"], ["B"], START, 21, tmp_path)


# ---- pacing_report and leakage_check ---------------------------------------------------
def _spend_report(world, days_in=14, **kw):
    observed, truth, _ = world.run_test(GeoTestSpec(GROUP, START, test_days=21, cooldown_days=0, **kw))
    cut = str((pd.Timestamp(START) + pd.Timedelta(days=days_in - 1)).date())
    observed = observed[observed["date"] <= cut]
    return map_geo(ingest("x.csv", raw=observed.astype(str)).data), truth


@pytest.fixture(scope="module")
def plan(areas42):
    return SpendPlan(GROUP, [g for g in areas42["geo"].unique() if g not in GROUP], START, 21)


def test_clean_holdout_is_on_track(world42, plan):
    g, _ = _spend_report(world42)
    p, lk = pacing_report(g, plan), leakage_check(g, plan)
    assert p["status"] == "on track" and lk["status"] == "clean"
    assert p["days_elapsed"] == 14 and len(p["weeks"]) == 2 and lk["leaked_spend_gbp"] == 0


def test_leakage_is_caught_and_named(world42, plan):
    g, truth = _spend_report(world42, leakage=0.08)
    lk = leakage_check(g, plan)
    assert lk["status"] == "check" and 6 < lk["leak_rate_pct"] < 10
    assert pacing_report(g, plan)["status"] == "check"
    assert truth["test_period"]["lift_pct_expected"] > -8.0    # leakage shrinks the true effect


def test_budget_moving_into_controls_is_caught(world42, plan):
    g, _ = _spend_report(world42, reallocation=0.6)
    p = pacing_report(g, plan)
    assert p["status"] == "check" and p["control_step_at_launch_pct"] > 5
    assert any("moved into the controls" in f for f in p["flags"])
    assert leakage_check(g, plan)["status"] == "clean"


def test_heavy_up_under_delivery(world42, areas42):
    observed, _, _ = world42.run_test(GeoTestSpec(GROUP, START, test_days=21, cooldown_days=0,
                                                  kind="heavy_up", spend_multiplier=1.5, leakage=0.6))
    g = map_geo(ingest("x.csv", raw=observed.astype(str)).data)
    plan = SpendPlan(GROUP, [x for x in areas42["geo"].unique() if x not in GROUP], START, 21,
                     kind="heavy_up", spend_multiplier=1.5)
    p = pacing_report(g, plan)
    assert p["status"] == "check" and "under-delivering" in p["flags"][0]


def test_monitoring_never_reads_results(world42, plan):
    g, _ = _spend_report(world42, leakage=0.08)
    scrambled = g.assign(kpi=np.random.default_rng(0).permutation(g["kpi"].to_numpy()) * 7)
    assert pacing_report(g, plan) == pacing_report(scrambled, plan)
    assert leakage_check(g, plan) == leakage_check(scrambled, plan)
    assert "kpi" not in json.dumps([pacing_report(g, plan), leakage_check(g, plan)])


def test_spend_only_report_without_kpi_column():
    rows = [{"Day": str(d.date()), "Postcode": pc, "Cost": "£12.50"}
            for d in pd.date_range("2025-01-01", periods=10) for pc in ("M1 1AE", "LS1 4DY")]
    r = ingest("platform.csv", raw=pd.DataFrame(rows), spend_only=True,
               column_map={"date": "Day", "postcode": "Postcode"})
    assert r.data["kpi"].isna().all() and r.data["spend"].sum() == pytest.approx(250)
