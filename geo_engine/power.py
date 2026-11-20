"""simulate_power: how small a lift can a given test design reliably detect?

Method: replay the design over many historical windows where no test happened. In each
window, fit the estimator on the pre-period and estimate the "lift" over the test
period. Since nothing changed, every estimate is pure error; their spread is the noise
the real test will face. The minimum detectable effect (MDE) follows from that error:

    noise = RMSE(null estimates) x noise_inflation
    MDE   = (t[1 - alpha/2] + t[power]) x noise

RMSE rather than standard deviation, so a design that is systematically off in history
(bias) is penalised rather than hidden. t rather than normal quantiles, with degrees of
freedom set by how many non-overlapping windows the history really holds, because ~40
overlapping 28-day windows carry only ~10 windows' worth of independent information.
noise_inflation (1.15) is calibrated: across 80 injected tests in 10 fresh synthetic
worlds (scripts/validate_lift.py), raw RMSE ran about 10% below the real error and
intervals without it covered the truth only 82% of the time instead of 90%.

Windows that overlap peak trading (20 Nov to 5 Jan by default) are skipped, since tests
are rarely run then and including them would overstate the noise.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.stats import norm, t as student_t

from .estimators import fit


@dataclass
class PowerSettings:
    alpha: float = 0.10                 # two-sided significance level
    power: float = 0.80                 # target probability of detecting a true effect of size MDE
    pre_period_days: int = 365          # history used to fit each window (shortened if needed)
    min_windows: int = 20               # fewest historical windows worth trusting
    window_step_days: int = 7
    skip_peak: tuple[str, str] | None = ("11-20", "01-05")  # month-day span to avoid, or None
    method: str = "synthetic_control"
    ridge: float = 0.05
    noise_inflation: float = 1.15       # calibrated on injected synthetic tests; see module docstring


class Panel:
    """Geo-level KPI as a dates x geos matrix, built once and shared across many simulations."""

    def __init__(self, geo_data: pd.DataFrame):
        Y = geo_data.pivot(index="date", columns="geo", values="kpi").sort_index().fillna(0)
        self.dates = Y.index
        self.geos = np.array(Y.columns)
        self.Y = Y.to_numpy(dtype=float)
        self.step_days = int(pd.Series(self.dates).diff().dt.days.median())
        S = geo_data.pivot(index="date", columns="geo", values="spend").reindex(index=Y.index, columns=Y.columns)
        self.spend = S.to_numpy(dtype=float) if S.notna().any().any() else None

    def index(self, geos) -> np.ndarray:
        missing = set(geos) - set(self.geos)
        if missing:
            raise ValueError(f"geos not in the data: {sorted(missing)}")
        return np.isin(self.geos, list(geos))

    def periods(self, days: int) -> int:
        return max(1, round(days / self.step_days))


def _windows(panel: Panel, pre: int, max_len: int, s: PowerSettings) -> list[int]:
    step = panel.periods(s.window_step_days)
    starts = list(range(pre, len(panel.dates) - max_len + 1, step))
    if s.skip_peak:
        a, b = s.skip_peak

        def overlaps_peak(i):
            d0, d1 = panel.dates[i], panel.dates[i + max_len - 1]
            for y in range(d0.year - 1, d1.year + 1):
                p0, p1 = pd.Timestamp(f"{y}-{a}"), pd.Timestamp(f"{y + 1}-{b}")
                if d0 <= p1 and d1 >= p0:
                    return True
            return False
        starts = [i for i in starts if not overlaps_peak(i)]
    return starts


def pre_period(panel: Panel, max_len: int, s: PowerSettings) -> int:
    """Longest pre-period up to the setting that still leaves min_windows windows."""
    want = panel.periods(s.pre_period_days)
    floor = panel.periods(91)
    for pre in range(want, floor - 1, -panel.periods(7)):
        if len(_windows(panel, pre, max_len, s)) >= s.min_windows:
            return pre
    raise ValueError("not enough history to simulate this design: need at least "
                     f"{s.min_windows} test-length windows after a 91-day pre-period")


@dataclass
class PowerResult:
    treatment: list[str]
    controls: list[str]
    test_days: int
    pre_period_days: int
    n_windows: int
    null_estimates: np.ndarray = field(repr=False)
    pre_fit_mape: float
    settings: PowerSettings = field(repr=False)
    window_starts: list = field(default_factory=list, repr=False)

    @property
    def sd(self) -> float:
        return float(self.null_estimates.std(ddof=1))

    @property
    def bias(self) -> float:
        return float(self.null_estimates.mean())

    @property
    def rmse(self) -> float:
        """Raw error of the estimator over the historical windows."""
        return float(np.sqrt(np.mean(self.null_estimates ** 2)))

    @property
    def noise(self) -> float:
        """The standard error used for intervals and power: RMSE x noise_inflation."""
        return self.rmse * self.settings.noise_inflation

    @property
    def df(self) -> float:
        """Effective degrees of freedom: independent test-length windows in the history used."""
        if not self.window_starts or len(self.window_starts) < 2:
            return max(2.0, self.n_windows - 1.0)
        span = (self.window_starts[-1] - self.window_starts[0]).days + self.test_days
        return float(max(2.0, min(self.n_windows - 1, span / self.test_days)))

    @property
    def critical(self) -> float:
        return float(student_t.ppf(1 - self.settings.alpha / 2, self.df))

    @property
    def mde(self) -> float:
        return float((self.critical + student_t.ppf(self.settings.power, self.df)) * self.noise)

    def power_at(self, lift: float) -> float:
        """Probability of a significant result if the true lift is `lift` (e.g. -0.08)."""
        c, se = self.critical, self.noise
        return float(norm.cdf((abs(lift) - c * se) / se) + norm.cdf((-abs(lift) - c * se) / se))

    def split(self) -> tuple["PowerResult", "PowerResult"]:
        """Earlier and later halves of the windows: choose on one, report on the other."""
        h = self.n_windows // 2
        mk = lambda sl: PowerResult(self.treatment, self.controls, self.test_days, self.pre_period_days,
                                    len(self.null_estimates[sl]), self.null_estimates[sl],
                                    self.pre_fit_mape, self.settings, self.window_starts[sl])
        return mk(slice(None, h)), mk(slice(h, None))

    def summary(self) -> dict:
        return {"treatment": self.treatment, "n_treatment": len(self.treatment),
                "n_controls": len(self.controls), "test_days": self.test_days,
                "pre_period_days": self.pre_period_days, "n_windows": self.n_windows,
                "mde_pct": round(100 * self.mde, 2), "rmse_pct": round(100 * self.rmse, 2),
                "noise_pct": round(100 * self.noise, 2), "df": round(self.df, 1),
                "bias_pct": round(100 * self.bias, 2), "pre_fit_mape_pct": round(100 * self.pre_fit_mape, 2)}


def simulate_many(panel: Panel, treatment: list[str], controls: list[str], test_days: list[int],
                  settings: PowerSettings | None = None) -> list[PowerResult]:
    """simulate_power for several test lengths at once (one fit per window serves them all)."""
    s = settings or PowerSettings()
    lens = [panel.periods(d) for d in test_days]
    max_len = max(lens)
    pre = pre_period(panel, max_len, s)
    starts = _windows(panel, pre, max_len, s)
    ti, ci = panel.index(treatment), panel.index(controls)
    if ti.sum() == 0 or ci.sum() == 0:
        raise ValueError("need at least one test geo and one control geo")
    if (ti & ci).any():
        raise ValueError("a geo cannot be both test and control")
    y_all, X_all = panel.Y[:, ti].sum(axis=1), panel.Y[:, ci]
    est = np.empty((len(starts), len(lens)))
    mape = np.empty(len(starts))
    for w, i in enumerate(starts):
        y_pre = y_all[i - pre:i]
        f = fit(s.method, y_pre, X_all[i - pre:i], ridge=s.ridge)
        mape[w] = np.mean(np.abs(f.pre_fitted - y_pre) / np.maximum(y_pre, 1))
        y_post, cf = y_all[i:i + max_len], f.predict(X_all[i:i + max_len])
        cy, cc = np.cumsum(y_post), np.cumsum(cf)
        for j, L in enumerate(lens):
            est[w, j] = cy[L - 1] / cc[L - 1] - 1
    starts_d = [panel.dates[i] for i in starts]
    return [PowerResult(list(treatment), list(controls), d, pre * panel.step_days, len(starts),
                        est[:, j], float(mape.mean()), s, starts_d) for j, d in enumerate(test_days)]


def simulate_power(geo_data: pd.DataFrame | Panel, treatment: list[str], test_days: int,
                   controls: list[str] | None = None, settings: PowerSettings | None = None) -> PowerResult:
    """MDE and power for one design. Controls default to every geo not in the test."""
    panel = geo_data if isinstance(geo_data, Panel) else Panel(geo_data)
    controls = controls if controls is not None else [g for g in panel.geos if g not in set(treatment)]
    return simulate_many(panel, treatment, controls, [test_days], settings)[0]
