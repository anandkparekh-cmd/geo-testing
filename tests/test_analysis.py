import numpy as np
import pandas as pd
import pytest

from geo_engine.analysis import estimate_lift, placebo_test, robustness_suite
from geo_engine.map_geo import map_geo
from geo_engine.ingest import ingest
from geo_engine.report import lift_chart, lift_markdown
from geo_engine.synthetic import GeoTestSpec

GROUP = ["AB", "CF", "CW", "G", "HD", "KW", "LE", "ME", "PL", "S"]
START = "2027-01-11"


@pytest.fixture(scope="module")
def demo(world42):
    observed, truth, _ = world42.run_test(GeoTestSpec(GROUP, START, test_days=28, cooldown_days=14))
    geo = map_geo(ingest("demo.csv", raw=observed.astype(str)).data)
    return geo, truth


@pytest.fixture(scope="module")
def result(demo):
    return estimate_lift(demo[0], GROUP, START, 28, cooldown_days=14)


def test_recovers_the_true_effect(result, demo):
    _, truth = demo
    true = truth["test_period"]["lift_pct_expected"]
    lo, hi = result.test["lift_ci_pct"]
    assert lo <= true <= hi and result.test["significant"]
    assert result.test["lift_pct"] == pytest.approx(true, abs=1.0)
    assert result.test["incremental_kpi"] == pytest.approx(truth["test_period"]["incremental_kpi_expected"], rel=0.1)
    assert result.test_plus_cooldown is not None and result.cooldown_window == ("2027-02-08", "2027-02-21")


def test_cost_per_incremental_conversion(result, demo):
    sp = result.spend
    assert sp["test_geo_spend_gbp"] == 0 and sp["spend_change_gbp"] < 0
    true_cost = demo[1]["test_period"]["cost_per_incremental_kpi_gbp"]
    lo, hi = sp["cost_per_incremental_kpi_ci_gbp"]
    assert lo <= true_cost <= hi
    assert sp["cost_per_incremental_kpi_gbp"] == pytest.approx(true_cost, rel=0.2)


def test_daily_series_adds_up(result):
    d = result.daily
    test = d[d["period"] == "test"]
    assert len(test) == 28 and (d["period"] == "cooldown").sum() == 14
    assert test["incremental"].sum() == pytest.approx(result.test["incremental_kpi"], abs=1)
    assert d.loc[d["period"] == "pre", "cumulative_incremental"].eq(0).all()


def test_no_test_period_is_not_called_significant(areas42):
    r = estimate_lift(areas42, GROUP, "2026-06-01", 28)
    lo, hi = r.test["lift_ci_pct"]
    assert lo < 0 < hi and not r.test["significant"]


def test_placebo_and_robustness(demo, result):
    pl = placebo_test(demo[0], result, n_space=40)
    assert pl.space_groups == 40 and pl.time_false_positive_rate <= 0.2
    assert pl.space_p_value <= 0.1          # the real effect stands out from fake groups
    rb = robustness_suite(demo[0], result)
    assert rb["consistent"] and len(rb["checks"]) == 5
    assert all(c["lift_pct"] < 0 for c in rb["checks"])


def test_readout_and_chart(result, demo, tmp_path):
    md = lift_markdown(result, truth=demo[1])
    assert "Truth inside the interval: yes" in md and "Cost per incremental conversion" in md
    path = lift_chart(result, tmp_path / "lift.png")
    assert path.exists() and path.stat().st_size > 10_000


def test_bad_windows_rejected(areas42):
    with pytest.raises(ValueError, match="not a date in the data"):
        estimate_lift(areas42, GROUP, "2030-01-01", 28)
    with pytest.raises(ValueError, match="before the test ends"):
        estimate_lift(areas42, GROUP, str(areas42["date"].max().date()), 28)
