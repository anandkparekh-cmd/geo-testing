import numpy as np
import pandas as pd
import pytest

from geo_engine.estimators import fit, lift
from geo_engine.power import Panel, PowerSettings, simulate_many, simulate_power
from geo_engine.synthetic import GeoTestSpec

GROUP = ["BD", "CA", "DA", "FY", "HP", "LE", "RG", "SP", "WR", "WS"]


@pytest.fixture(scope="module")
def panel(areas42):
    return Panel(areas42)


@pytest.fixture(scope="module")
def results(panel):
    controls = [g for g in panel.geos if g not in GROUP]
    return simulate_many(panel, GROUP, controls, [14, 28, 42])


def test_null_error_is_unbiased_and_shrinks_with_length(results):
    by_len = {r.test_days: r for r in results}
    for r in results:
        assert abs(r.bias) < 0.5 * r.rmse          # no systematic error with no test running
        assert r.n_windows >= 20 and r.pre_period_days == 365
    assert by_len[42].rmse < by_len[14].rmse        # longer tests average out more noise


def test_mde_and_power_are_consistent(results):
    from scipy.stats import t
    r = results[1]
    assert r.noise == pytest.approx(r.rmse * 1.15)
    assert 5 <= r.df <= r.n_windows - 1                       # overlapping windows count for less
    assert r.mde == pytest.approx((t.ppf(0.95, r.df) + t.ppf(0.80, r.df)) * r.noise)
    assert 0.80 <= r.power_at(-r.mde) <= 0.85
    assert r.power_at(0.5 * r.mde) < r.power_at(r.mde) < r.power_at(2 * r.mde)
    assert r.power_at(0) <= r.settings.alpha                   # false positive rate at most alpha


def test_no_simulated_window_touches_peak_trading(results):
    for start in results[2].window_starts:
        end = start + pd.Timedelta(days=41)
        for y in (start.year - 1, start.year):
            assert not (start <= pd.Timestamp(f"{y + 1}-01-05") and end >= pd.Timestamp(f"{y}-11-20"))


def test_more_geos_usually_means_lower_mde(panel):
    rng = np.random.default_rng(3)
    pool = [g for g in panel.geos if g not in {"ZE", "LD", "WC", "EC", "KW", "TD", "DG", "HG", "IV"}]
    def avg(k):
        return np.mean([simulate_power(panel, list(rng.choice(pool, k, replace=False)), 28).rmse
                        for _ in range(6)])
    assert avg(20) < avg(5)


def test_estimator_recovers_a_real_injected_holdout(world42, panel, results):
    observed, truth, _ = world42.run_test(GeoTestSpec(GROUP, "2027-02-15", test_days=28, cooldown_days=0))
    d = observed.assign(geo=observed["postcode"].str.extract(r"^([A-Z]+)", expand=False),
                        date=pd.to_datetime(observed["date"]))
    p = Panel(d.groupby(["date", "geo"], as_index=False)[["kpi", "spend"]].sum())
    i = int(np.searchsorted(p.dates, pd.Timestamp("2027-02-15")))
    ti, ci = p.index(GROUP), ~p.index(GROUP)
    y, X = p.Y[:, ti].sum(axis=1), p.Y[:, ci]
    f = fit("synthetic_control", y[i - 365:i], X[i - 365:i])
    est = lift(y[i:i + 28], f.predict(X[i:i + 28]))
    true = truth["test_period"]["lift_pct_expected"] / 100
    assert true < -0.05
    assert abs(est - true) < 2.5 * results[1].rmse   # within the error the simulation predicted


def test_tbr_also_works(panel):
    r = simulate_power(panel, GROUP, 28, settings=PowerSettings(method="tbr"))
    assert 0 < r.mde < 0.2


def test_bad_designs_rejected(panel):
    with pytest.raises(ValueError, match="both test and control"):
        simulate_many(panel, ["B"], ["B", "M"], [28])
    with pytest.raises(ValueError, match="not in the data"):
        simulate_power(panel, ["XX"], 28)
    short = Panel(pd.DataFrame({"date": pd.date_range("2025-01-01", periods=200).repeat(2),
                                "geo": ["A", "B"] * 200, "kpi": 1.0, "spend": np.nan}))
    with pytest.raises(ValueError, match="not enough history"):
        simulate_power(short, ["A"], 28)
