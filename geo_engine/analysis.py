"""Read a geo test: estimate_lift, placebo_test and robustness_suite.

estimate_lift fits the counterfactual on the pre-period, compares it with what the
test geos actually did, and puts a confidence interval around the result. The interval
comes from replaying the same design over windows before the test, where nothing
changed: the spread of those "lifts" is the noise this design carries (the same method
simulate_power used to set the MDE, so design and readout agree).

placebo_test checks that interval two ways:
- in time: for each pre-test window, would an interval built from the other windows
  have wrongly called a lift? That rate should be close to alpha.
- in space: during the real test window, groups of untouched control geos are treated
  as fake test groups. Their "lifts" should be small, and the real lift should stand out.

robustness_suite re-estimates the lift under reasonable alternative choices (other
method, shorter fitting period, each test geo left out, top control left out) and
checks the answer keeps its direction.

outside_events lists unusual regional movements among the control areas during the test
(a regional promotion, an outage, weather), as context for the readout. They are common:
across 16 synthetic tests, 12 windows had two or more, and those tests were no less
accurate than the rest, because the history behind the interval holds such events too.
So they are listed, not used to discount the result.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.stats import t as student_t

from .estimators import fit
from .geography import AREA_TO_REGION
from .power import Panel, PowerSettings, simulate_many


def _panel(geo_data) -> Panel:
    return geo_data if isinstance(geo_data, Panel) else Panel(geo_data)


def _truncate(panel: Panel, end: int) -> Panel:
    """A copy of the panel holding only periods before index `end`."""
    p = object.__new__(Panel)
    p.dates, p.geos, p.Y, p.step_days = panel.dates[:end], panel.geos, panel.Y[:end], panel.step_days
    p.spend = panel.spend[:end] if panel.spend is not None else None
    return p


def _locate(panel: Panel, start: str, test_days: int, cooldown_days: int) -> tuple[int, int, int]:
    ts = pd.Timestamp(start)
    i0 = int(np.searchsorted(panel.dates, ts))
    if i0 >= len(panel.dates) or panel.dates[i0] != ts:
        raise ValueError(f"test start {ts.date()} is not a date in the data")
    L, C = panel.periods(test_days), panel.periods(cooldown_days) if cooldown_days else 0
    if i0 + L > len(panel.dates):
        raise ValueError(f"data ends {panel.dates[-1].date()}, before the test ends; wait for the full test")
    C = min(C, len(panel.dates) - i0 - L)  # cooldown may still be running
    return i0, L, C


@dataclass
class LiftResult:
    treatment: list[str]
    controls: list[str]
    method: str
    test_window: tuple[str, str]
    cooldown_window: tuple[str, str] | None
    pre_period_days: int
    n_null_windows: int
    alpha: float
    test: dict
    test_plus_cooldown: dict | None
    spend: dict | None
    daily: pd.DataFrame = field(repr=False)
    top_controls: dict = field(default_factory=dict)
    null_rmse: float = 0.0          # raw RMSE of the no-test windows
    null_noise: float = 0.0         # the standard error used: RMSE x noise_inflation
    critical: float = 0.0           # t critical value used for the interval
    null_estimates: np.ndarray = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {"treatment": self.treatment, "n_controls": len(self.controls), "method": self.method,
                "test_window": self.test_window, "cooldown_window": self.cooldown_window,
                "pre_period_days": self.pre_period_days, "n_null_windows": self.n_null_windows,
                "confidence": 1 - self.alpha, "test": self.test,
                "test_plus_cooldown": self.test_plus_cooldown, "spend": self.spend,
                "top_controls": self.top_controls}


def _window_stats(actual: float, cf: float, null) -> dict:
    """null: the PowerResult for this window length (same noise and critical value as the design)."""
    est = actual / cf - 1
    se, crit = null.noise, null.critical
    lo, hi = est - crit * se, est + crit * se
    p_norm = float(2 * (1 - student_t.cdf(abs(est) / se, null.df)))
    p_emp = float((1 + np.sum(np.abs(null.null_estimates) >= abs(est))) / (1 + len(null.null_estimates)))
    return {"actual_kpi": round(actual, 1), "counterfactual_kpi": round(cf, 1),
            "incremental_kpi": round(actual - cf, 1),
            "incremental_kpi_ci": [round(lo * cf, 1), round(hi * cf, 1)],
            "lift_pct": round(100 * est, 2), "lift_ci_pct": [round(100 * lo, 2), round(100 * hi, 2)],
            "p_value": round(p_norm, 4), "p_value_empirical": round(p_emp, 4),
            "significant": bool(lo > 0 or hi < 0)}


def estimate_lift(geo_data: pd.DataFrame | Panel, treatment: list[str], start: str, test_days: int,
                  cooldown_days: int = 0, controls: list[str] | None = None,
                  settings: PowerSettings | None = None, baseline_days: int = 28) -> LiftResult:
    """Estimate the test's effect. geo_data: map_geo output covering history and the test.

    start: first day of the test. test_days: its length. cooldown_days: days after the test
    still counted for carryover. controls default to every geo not in the test.
    """
    s = settings or PowerSettings()
    panel = _panel(geo_data)
    i0, L, C = _locate(panel, start, test_days, cooldown_days)
    controls = controls if controls is not None else [g for g in panel.geos if g not in set(treatment)]

    # Null distribution from before the test, for the test window and test + cooldown
    lengths = [test_days] + ([test_days + C * panel.step_days] if C else [])
    nulls = simulate_many(_truncate(panel, i0), treatment, controls, lengths, s)
    pre = panel.periods(nulls[0].pre_period_days)

    ti, ci = panel.index(treatment), panel.index(controls)
    y, X = panel.Y[:, ti].sum(axis=1), panel.Y[:, ci]
    f = fit(s.method, y[i0 - pre:i0], X[i0 - pre:i0], ridge=s.ridge)
    span = slice(i0 - pre, i0 + L + C)
    cf = f.predict(X[span])
    actual = y[span]
    test_sl = slice(pre, pre + L)

    test = _window_stats(float(actual[test_sl].sum()), float(cf[test_sl].sum()), nulls[0])
    tpc = None
    if C:
        both = slice(pre, pre + L + C)
        tpc = _window_stats(float(actual[both].sum()), float(cf[both].sum()), nulls[1])

    spend = None
    if panel.spend is not None:
        sp = panel.spend[:, ti].sum(axis=1)
        b = panel.periods(baseline_days)
        base_rate = float(sp[i0 - b:i0].mean())
        actual_spend = float(sp[i0:i0 + L].sum())
        change = actual_spend - base_rate * L
        inc, (ilo, ihi) = test["incremental_kpi"], test["incremental_kpi_ci"]
        spend = {"test_geo_spend_gbp": round(actual_spend, 2),
                 "baseline_spend_gbp": round(base_rate * L, 2),
                 "spend_change_gbp": round(change, 2),
                 "baseline": f"average of the {baseline_days} days before the test"}
        if inc != 0 and np.sign(inc) == np.sign(change):
            spend["cost_per_incremental_kpi_gbp"] = round(change / inc, 2)
            if test["significant"] and np.sign(ilo) == np.sign(ihi):
                bounds = sorted([change / ilo, change / ihi])
                spend["cost_per_incremental_kpi_ci_gbp"] = [round(bounds[0], 2), round(bounds[1], 2)]

    dates = panel.dates[span]
    period = np.array(["pre"] * pre + ["test"] * L + ["cooldown"] * C)
    inc = actual - cf
    daily = pd.DataFrame({"date": dates, "period": period, "actual": actual.round(2),
                          "counterfactual": cf.round(2), "incremental": inc.round(2)})
    daily["cumulative_incremental"] = np.where(period == "pre", 0.0, np.cumsum(np.where(period == "pre", 0, inc)))
    weights = pd.Series(f.weights, index=np.array(controls)[np.isin(np.array(controls), panel.geos[ci])])
    top = (weights[weights > 0].sort_values(ascending=False).head(8).round(3).to_dict()
           if s.method == "synthetic_control" else {})

    d = panel.dates
    return LiftResult(
        treatment=list(treatment), controls=list(controls), method=s.method,
        test_window=(str(d[i0].date()), str(d[i0 + L - 1].date())),
        cooldown_window=(str(d[i0 + L].date()), str(d[i0 + L + C - 1].date())) if C else None,
        pre_period_days=pre * panel.step_days, n_null_windows=nulls[0].n_windows, alpha=s.alpha,
        test=test, test_plus_cooldown=tpc, spend=spend, daily=daily, top_controls=top,
        null_rmse=nulls[0].rmse, null_noise=nulls[0].noise, critical=nulls[0].critical,
        null_estimates=nulls[0].null_estimates)


# ---------------------------------------------------------------------------------------
@dataclass
class PlaceboResult:
    time_false_positive_rate: float
    time_windows: int
    space_groups: int
    space_false_positive_rate: float
    space_rmse_pct: float
    space_p_value: float            # share of fake groups whose lift stands out at least as much as the real one
    alpha: float
    passed: bool
    notes: list[str]
    space_lifts: np.ndarray = field(repr=False, default=None)

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "space_lifts"}
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in d.items()}


def placebo_test(geo_data: pd.DataFrame | Panel, result: LiftResult, n_space: int = 100,
                 settings: PowerSettings | None = None, seed: int = 0) -> PlaceboResult:
    """Check the confidence interval behind `result` in time and in space."""
    s = settings or PowerSettings(method=result.method, alpha=result.alpha)
    panel = _panel(geo_data)
    infl, crit = s.noise_inflation, result.critical

    # In time: leave-one-out interval for each pre-test window
    e = result.null_estimates
    loo = np.array([np.sqrt(np.mean(np.delete(e, k) ** 2)) for k in range(len(e))]) * infl
    time_fpr = float(np.mean(np.abs(e) > crit * loo))

    # In space: fake test groups drawn from controls, read during the real test window
    i0 = int(np.searchsorted(panel.dates, pd.Timestamp(result.test_window[0])))
    L = panel.periods((pd.Timestamp(result.test_window[1]) - pd.Timestamp(result.test_window[0])).days
                      + panel.step_days)
    pre = panel.periods(result.pre_period_days)
    tot = pd.Series(panel.Y[:i0].sum(axis=0), index=panel.geos)
    share = tot / tot.sum()
    target = share[result.treatment].sum()
    pool = share[result.controls].sort_values(ascending=False)
    k = min(len(result.treatment), len(pool) // 3)
    rng = np.random.default_rng(seed)
    before = _truncate(panel, i0)
    days = L * panel.step_days
    lifts, zs, tries = [], [], 0
    while len(lifts) < n_space and tries < n_space * 30:
        tries += 1
        bands = np.array_split(pool.index.to_numpy(), k)
        fake = [rng.choice(b) for b in bands]
        if not (0.5 * target <= share[fake].sum() <= 2 * target):
            continue
        ctrl = [g for g in result.controls if g not in fake]
        fi, ci = panel.index(fake), panel.index(ctrl)
        y, X = panel.Y[:, fi].sum(axis=1), panel.Y[:, ci]
        f = fit(s.method, y[i0 - pre:i0], X[i0 - pre:i0], ridge=s.ridge)
        lift = y[i0:i0 + L].sum() / f.predict(X[i0:i0 + L]).sum() - 1
        # each fake group is judged against its own noise, as the real group was
        own = simulate_many(before, fake, ctrl, [days], s)[0]
        lifts.append(lift)
        zs.append(abs(lift) / own.noise / own.critical)
    lifts, zs = np.array(lifts), np.array(zs)
    if len(lifts) < 20:
        raise ValueError("could not draw enough fake test groups from the controls")
    real = result.test["lift_pct"] / 100
    real_z = abs(real) / result.null_noise / crit
    space_fpr = float(np.mean(zs > 1))
    space_p = float((1 + np.sum(zs >= real_z)) / (1 + len(zs)))
    space_rmse = float(np.sqrt(np.mean(lifts ** 2)))

    notes, ok = [], True
    if time_fpr > 2 * result.alpha:
        ok = False
        notes.append(f"in time, {time_fpr:.0%} of no-test windows looked significant (expected about "
                     f"{result.alpha:.0%}): the interval is too narrow for this design")
    if space_fpr > 2 * result.alpha:
        ok = False
        notes.append(f"in space, {space_fpr:.0%} of fake test groups looked significant during the test "
                     f"window (expected about {result.alpha:.0%}): something else moved in this period")
    if result.test["significant"] and space_p > result.alpha:
        ok = False
        notes.append(f"{space_p:.0%} of fake groups stood out as much as the real one")
    if ok:
        notes.append("interval behaves as expected in time and in space")
    return PlaceboResult(time_fpr, len(e), len(lifts), space_fpr, round(100 * space_rmse, 2), space_p,
                         result.alpha, ok, notes, lifts)


# ---------------------------------------------------------------------------------------
def robustness_suite(geo_data: pd.DataFrame | Panel, result: LiftResult,
                     settings: PowerSettings | None = None) -> dict:
    """Re-estimate the test-period lift under alternative choices; report whether the direction holds."""
    s = settings or PowerSettings(method=result.method, alpha=result.alpha)
    panel = _panel(geo_data)
    i0 = int(np.searchsorted(panel.dates, pd.Timestamp(result.test_window[0])))
    L = panel.periods((pd.Timestamp(result.test_window[1]) - pd.Timestamp(result.test_window[0])).days
                      + panel.step_days)
    pre = panel.periods(result.pre_period_days)

    def lift_for(treat, ctrl, method=s.method, pre_p=pre):
        ti, ci = panel.index(treat), panel.index(ctrl)
        y, X = panel.Y[:, ti].sum(axis=1), panel.Y[:, ci]
        f = fit(method, y[i0 - pre_p:i0], X[i0 - pre_p:i0], ridge=s.ridge)
        return float(y[i0:i0 + L].sum() / f.predict(X[i0:i0 + L]).sum() - 1)

    T, Cn = result.treatment, result.controls
    checks = []
    other = "tbr" if s.method == "synthetic_control" else "synthetic_control"
    checks.append({"check": f"method: {other.replace('_', ' ')}", "lift_pct": 100 * lift_for(T, Cn, other)})
    short = min(pre, panel.periods(182))
    if short < pre:
        checks.append({"check": "fitting period: 182 days", "lift_pct": 100 * lift_for(T, Cn, pre_p=short)})
    if result.top_controls:
        top = next(iter(result.top_controls))
        checks.append({"check": f"without top control ({top})",
                       "lift_pct": 100 * lift_for(T, [g for g in Cn if g != top])})
    if len(T) > 1:
        loo = [(g, 100 * lift_for([t for t in T if t != g], Cn)) for g in T]
        lo, hi = min(loo, key=lambda x: x[1]), max(loo, key=lambda x: x[1])
        checks.append({"check": f"each test geo left out (lowest without {lo[0]})", "lift_pct": lo[1]})
        checks.append({"check": f"each test geo left out (highest without {hi[0]})", "lift_pct": hi[1]})
    main = result.test["lift_pct"]
    for c in checks:
        c["lift_pct"] = round(c["lift_pct"], 2)
        c["same_direction"] = bool(np.sign(c["lift_pct"]) == np.sign(main))
    consistent = all(c["same_direction"] for c in checks)
    spread = [c["lift_pct"] for c in checks] + [main]
    return {"main_lift_pct": main, "checks": checks, "consistent": consistent,
            "range_pct": [round(min(spread), 2), round(max(spread), 2)],
            "verdict": ("every alternative gives the same direction" if consistent else
                        "direction changes under some alternatives: treat the result with caution")}


# ---------------------------------------------------------------------------------------
def outside_events(geo_data: pd.DataFrame | Panel, result: LiftResult,
                   region_of: dict | None = None) -> list[dict]:
    """Unusual regional movements among the control areas during the test and cooldown.

    Uses the quality report's regional spike-and-dip detection on the controls only, so
    the test itself cannot trigger it. Needs postcode-area geos (or a region lookup).
    """
    from .quality import QualitySettings, _unusual
    panel = _panel(geo_data)
    region_of = region_of or (AREA_TO_REGION if set(panel.geos) <= set(AREA_TO_REGION) else None)
    if not region_of:
        return []
    s = QualitySettings()
    ci = panel.index(result.controls)
    Y = pd.DataFrame(panel.Y[:, ci], index=panel.dates, columns=panel.geos[ci])
    R = Y.T.groupby(Y.columns.map(region_of)).sum().T
    window = max(5, round(s.window_days / panel.step_days) | 1)
    spans = (1, 2) if panel.step_days == 7 else (1, 3, 7)
    ev = _unusual(R, s, window, "region", s.region_z_threshold, s.region_min_effect, 1, spans)
    a = pd.Timestamp(result.test_window[0])
    b = pd.Timestamp(result.cooldown_window[1] if result.cooldown_window else result.test_window[1])
    ev = ev[(ev["end"] >= a) & (ev["start"] <= b)]
    out = []
    for e in ev.itertuples():
        out.append({"region": e.where, "start": str(e.start.date()), "end": str(e.end.date()),
                    "direction": e.direction, "size_pct": float(e.size_pct),
                    "test_geos_in_region": sorted(g for g in result.treatment if region_of.get(g) == e.where)})
    return out
