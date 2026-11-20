import numpy as np
import pandas as pd
import pytest

from geo_engine.synthetic import GeoTestSpec, WorldConfig, generate_world
from geo_engine.synthetic.postcodes import AREAS

AREAS_IN_TEST = ["B", "LS", "NG", "CF", "EX"]
START = "2027-01-11"


@pytest.fixture(scope="module")
def world():
    return generate_world(WorldConfig(seed=7))


@pytest.fixture(scope="module")
def history(world):
    return world.history()


@pytest.fixture(scope="module")
def holdout(world):
    return world.run_test(GeoTestSpec(treatment_areas=AREAS_IN_TEST, start=START))


def _area(postcodes: pd.Series) -> pd.Series:
    return postcodes.str.extract(r"^([A-Z]+)", expand=False)


def test_same_seed_same_data(history):
    again = generate_world(WorldConfig(seed=7)).history()
    pd.testing.assert_frame_equal(history, again)


def test_different_seed_different_data(history):
    other = generate_world(WorldConfig(seed=8)).history()
    assert not history["kpi"].equals(other["kpi"])


def test_schema_matches_ingest_format(history, world):
    assert list(history.columns) == ["date", "postcode", "kpi", "spend"]
    assert (history["kpi"] >= 0).all() and (history["spend"] >= 0).all()
    n_dates, n_districts = history["date"].nunique(), history["postcode"].nunique()
    assert n_dates == world.config.history_days
    assert len(history) == n_dates * n_districts  # every district on every day
    assert set(_area(history["postcode"])) == set(AREAS)


def test_national_level_close_to_config(history, world):
    daily = history.groupby("date")[["kpi", "spend"]].sum().mean()
    assert daily["kpi"] == pytest.approx(world.config.national_daily_kpi, rel=0.03)
    assert daily["spend"] == pytest.approx(world.config.national_daily_spend, rel=0.01)


def test_history_unchanged_by_test(history, holdout, world):
    observed, _, _ = holdout
    head = observed[observed["date"] <= str(world.history_end.date())].reset_index(drop=True)
    pd.testing.assert_frame_equal(head, history)


def test_untreated_areas_identical_with_and_without_test(world, holdout):
    observed, _, _ = holdout
    end = observed["date"].nunique()
    bau = world._frame(world._district_mu(world.spend), world.spend, end)
    untreated = ~_area(observed["postcode"]).isin(AREAS_IN_TEST)
    pd.testing.assert_frame_equal(observed[untreated].reset_index(drop=True),
                                  bau[untreated].reset_index(drop=True))
    treated_kpi_differs = (observed.loc[~untreated, "kpi"].to_numpy() != bau.loc[~untreated, "kpi"].to_numpy()).any()
    assert treated_kpi_differs


def test_holdout_turns_spend_off_only_in_window(holdout):
    observed, truth, _ = holdout
    treated = _area(observed["postcode"]).isin(AREAS_IN_TEST)
    a, b = truth["test_window"]
    in_window = observed["date"].between(a, b)
    assert (observed.loc[treated & in_window, "spend"] == 0).all()
    assert (observed.loc[treated & ~in_window, "spend"] > 0).all()
    assert (observed.loc[~treated, "spend"] > 0).all()


def test_holdout_truth_is_a_plausible_loss(holdout, world):
    _, truth, daily = holdout
    tp = truth["test_period"]
    assert tp["incremental_kpi_expected"] < 0
    assert -20 < tp["lift_pct_expected"] < -2
    # realised effect (with Poisson noise) stays close to the expected effect
    assert tp["incremental_kpi_realised"] == pytest.approx(tp["incremental_kpi_expected"], rel=0.25)
    # the daily truth file adds up to the summary
    window = daily["date"].between(*truth["test_window"])
    inc = (daily.loc[window, "expected_actual"] - daily.loc[window, "expected_counterfactual"]).sum()
    assert inc == pytest.approx(tp["incremental_kpi_expected"], abs=1)
    # carryover: some effect remains in the cooldown, then it fades
    assert truth["test_plus_cooldown"]["incremental_kpi_expected"] < tp["incremental_kpi_expected"]


def test_heavy_up_has_positive_but_diminishing_effect(world):
    _, up, _ = world.run_test(GeoTestSpec(AREAS_IN_TEST, START, kind="heavy_up", spend_multiplier=1.5))
    _, up2, _ = world.run_test(GeoTestSpec(AREAS_IN_TEST, START, kind="heavy_up", spend_multiplier=2.0))
    assert 0 < up["test_period"]["lift_pct_expected"] < up2["test_period"]["lift_pct_expected"]
    # diminishing returns: the second 50% of extra spend buys less than the first
    first = up["test_period"]["incremental_kpi_expected"]
    second = up2["test_period"]["incremental_kpi_expected"] - first
    assert second < first


@pytest.mark.parametrize("kwargs, message", [
    ({"treatment_areas": ["XX"], "start": START}, "unknown postcode areas"),
    ({"treatment_areas": ["B"], "start": "2026-06-01"}, "after the history period"),
    ({"treatment_areas": ["B"], "start": "2027-03-20"}, "past the generated horizon"),
])
def test_bad_specs_rejected(world, kwargs, message):
    with pytest.raises(ValueError, match=message):
        world.run_test(GeoTestSpec(**kwargs))


def test_spec_validation():
    with pytest.raises(ValueError):
        GeoTestSpec(["B"], START, kind="holdout", spend_multiplier=1.2)
    with pytest.raises(ValueError):
        GeoTestSpec(["B"], START, kind="heavy_up", spend_multiplier=0.8)
    assert GeoTestSpec(["B"], START, kind="heavy_up").spend_multiplier == 1.5
