"""Synthetic UK geo data with a known, injectable incremental effect.

The world is built once from a seed, for a horizon of history plus future days.
`history()` returns business-as-usual data for the history period. `run_test()`
changes spend in chosen postcode areas for a test window and returns the
observed data plus the ground truth: what the KPI would have been without the
change (the counterfactual).

Both outputs use common random numbers, so every cell the test does not touch
is identical with and without the test. Output is long-format CSV in the same
shape `ingest` expects for real data: date, postcode, kpi, spend.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd
from scipy.stats import poisson

from .postcodes import TIER_DISTRICTS, TIER_WEIGHT, area_table

WEEKDAY_KPI = np.array([0.08, 0.05, 0.03, 0.0, -0.05, -0.15, 0.0])  # Mon..Sun, log scale
WEEKDAY_SPEND = np.array([0.03, 0.03, 0.03, 0.02, 0.0, -0.08, -0.06])


@dataclass
class WorldConfig:
    seed: int = 42
    start: str = "2024-11-18"          # a Monday
    history_days: int = 728            # 104 weeks of history
    future_days: int = 140             # room to run tests after the history
    national_daily_kpi: float = 4000.0  # conversions per day across the UK
    national_daily_spend: float = 25000.0  # GBP per day on the test channel
    media_share: float = 0.10          # share of KPI driven by the test channel at normal spend
    adstock_decay: float = 0.5         # share of yesterday's ad effect carried into today
    annual_growth: float = 0.06


@dataclass
class GeoTestSpec:
    treatment_areas: list[str]
    start: str
    test_days: int = 28
    cooldown_days: int = 14
    kind: str = "holdout"              # "holdout" or "heavy_up"
    spend_multiplier: float | None = None  # holdout: 0.0; heavy_up: e.g. 1.5 for +50%
    leakage: float = 0.0               # execution error: share of the planned spend change that never
                                       # happens in test geos (0.05 = a holdout still spends 5% of normal)
    reallocation: float = 0.0          # execution error: share of paused spend the platform moves into
                                       # the control geos (holdout only), contaminating the controls

    def __post_init__(self) -> None:
        if not 0 <= self.leakage < 1 or not 0 <= self.reallocation <= 1:
            raise ValueError("leakage must be in [0, 1) and reallocation in [0, 1]")
        if self.kind not in ("holdout", "heavy_up"):
            raise ValueError("kind must be 'holdout' or 'heavy_up'")
        if self.spend_multiplier is None:
            self.spend_multiplier = 0.0 if self.kind == "holdout" else 1.5
        if self.kind == "holdout" and self.spend_multiplier >= 1:
            raise ValueError("a holdout needs spend_multiplier < 1 (0 = go dark)")
        if self.kind == "heavy_up" and self.spend_multiplier <= 1:
            raise ValueError("a heavy-up needs spend_multiplier > 1")
        if not self.treatment_areas:
            raise ValueError("treatment_areas is empty")


def _ar1(rng: np.random.Generator, n_series: int, n_steps: int, phi: float, sd) -> np.ndarray:
    """Stationary AR(1) series, one row per series. sd may be a scalar or per-series array."""
    sd = np.broadcast_to(np.asarray(sd, dtype=float), (n_series,))
    out = np.empty((n_series, n_steps))
    out[:, 0] = rng.normal(0, sd / np.sqrt(1 - phi**2))
    shocks = rng.normal(0, 1, (n_series, n_steps)) * sd[:, None]
    for t in range(1, n_steps):
        out[:, t] = phi * out[:, t - 1] + shocks[:, t]
    return out


def _calendar_effects(dates: pd.DatetimeIndex) -> np.ndarray:
    """Log-scale annual seasonality plus UK retail calendar events."""
    doy = dates.dayofyear.to_numpy()
    eff = 0.06 * np.cos(2 * np.pi * (doy - 10) / 365.25)  # winter high, summer low
    for year in sorted(set(dates.year)):
        nov = pd.date_range(f"{year}-11-01", f"{year}-11-30")
        black_friday = nov[nov.dayofweek == 3][3] + pd.Timedelta(days=1)
        bumps = {black_friday + pd.Timedelta(days=d): v for d, v in
                 [(-3, 0.10), (-2, 0.10), (-1, 0.12), (0, 0.25), (1, 0.15), (2, 0.12), (3, 0.22)]}
        bumps.update({pd.Timestamp(f"{year}-12-24"): -0.30, pd.Timestamp(f"{year}-12-25"): -0.90,
                      pd.Timestamp(f"{year}-12-26"): -0.20, pd.Timestamp(f"{year}-01-01"): -0.30})
        for d in range(27, 32):
            bumps[pd.Timestamp(f"{year}-12-{d}")] = 0.08
        for day, v in bumps.items():
            eff[dates == day] += v
    return eff


class SyntheticWorld:
    def __init__(self, config: WorldConfig | None = None):
        self.config = cfg = config or WorldConfig()
        rng = np.random.default_rng(cfg.seed)
        T = cfg.history_days + cfg.future_days
        self.dates = pd.date_range(cfg.start, periods=T, freq="D")
        self.history_end = self.dates[cfg.history_days - 1]

        # Areas and regions
        areas = pd.DataFrame(area_table())
        areas["weight"] = areas["tier"].map(TIER_WEIGHT) * rng.lognormal(0, 0.25, len(areas))
        areas["share"] = areas["weight"] / areas["weight"].sum()
        self.areas = areas
        G = len(areas)
        regions = sorted(areas["region"].unique())
        region_idx = areas["region"].map({r: i for i, r in enumerate(regions)}).to_numpy()

        # Districts within each area (codes are illustrative)
        rows = []
        for gi, a in areas.iterrows():
            n = max(1, TIER_DISTRICTS[a["tier"]] + int(rng.integers(-1, 2)))
            kw = rng.dirichlet(np.full(n, 3.0))
            sw = kw * rng.lognormal(0, 0.1, n)
            sw /= sw.sum()
            for i in range(n):
                rows.append({"postcode": f"{a['area']}{i + 1}", "area": a["area"], "area_idx": gi,
                             "kpi_weight": kw[i], "spend_weight": sw[i]})
        self.districts = pd.DataFrame(rows)
        D = len(self.districts)
        self._d_area = self.districts["area_idx"].to_numpy()

        # Organic demand (log scale components)
        t_years = np.arange(T) / 365.25
        dow = self.dates.dayofweek.to_numpy()
        national = cfg.annual_growth * t_years + _ar1(rng, 1, T, 0.98, 0.008)[0] + _calendar_effects(self.dates)
        geo_growth = rng.normal(0, 0.03, G)[:, None] * t_years[None, :]
        weekday = (WEEKDAY_KPI[None, :] + rng.normal(0, 0.015, (G, 7)))[:, dow]
        reg_sd = np.where(np.array(regions) == "London", 0.014, 0.010)
        regional = _ar1(rng, len(regions), T, 0.97, reg_sd)[region_idx]
        idio_sd = areas["tier"].map({1: 0.06, 2: 0.045, 3: 0.035, 4: 0.03}).to_numpy()
        idio = _ar1(rng, G, T, 0.85, idio_sd)

        shock, events = np.zeros((G, T)), []
        for ri, region in enumerate(regions):  # regional promotions, about 2 a year
            for _ in range(rng.poisson(2 * T / 365.25)):
                s, length, size = int(rng.integers(0, T - 10)), int(rng.integers(3, 11)), rng.uniform(0.12, 0.30)
                shock[region_idx == ri, s:s + length] += size
                events.append({"type": "regional_promo", "where": region, "start": self.dates[s].date(),
                               "days": length, "kpi_effect_pct": round(100 * (np.exp(size) - 1), 1)})
        for gi, area in enumerate(areas["area"]):  # local outages, about 1 every 2.5 years per area
            for _ in range(rng.poisson(0.4 * T / 365.25)):
                s, length, size = int(rng.integers(0, T - 2)), int(rng.integers(1, 3)), np.log(rng.uniform(0.3, 0.6))
                shock[gi, s:s + length] += size
                events.append({"type": "outage", "where": area, "start": self.dates[s].date(),
                               "days": length, "kpi_effect_pct": round(100 * (np.exp(size) - 1), 1)})
        self.events = pd.DataFrame(events).sort_values("start").reset_index(drop=True)

        organic = np.exp(national[None, :] + geo_growth + weekday + regional + idio + shock)
        h = cfg.history_days
        base = cfg.national_daily_kpi * (1 - cfg.media_share) * areas["share"].to_numpy()
        self.organic = organic * (base / organic[:, :h].mean(axis=1))[:, None]

        # Business-as-usual spend on the test channel
        steps = np.zeros(T)
        for s in rng.choice(np.arange(30, T - 30), size=3, replace=False):
            steps[s:] += rng.uniform(-0.15, 0.15)
        doy = self.dates.dayofyear.to_numpy()
        seasonal = 0.10 * np.exp(-((doy - 335) / 25.0) ** 2)  # pushes into Nov-Dec
        national_spend = cfg.national_daily_spend * np.exp(steps + seasonal + WEEKDAY_SPEND[dow])
        spend_share = areas["share"].to_numpy() ** 0.95 * rng.lognormal(0, 0.15, G)
        spend_share /= spend_share.sum()
        spend = national_spend[None, :] * spend_share[:, None] * rng.lognormal(0, 0.08, (G, T))
        self.spend = spend * cfg.national_daily_spend / spend[:, :cfg.history_days].sum(axis=0).mean()

        # Media response: adstock, then diminishing returns, calibrated to media_share
        A = self._adstock(self.spend)
        self._half_sat = A[:, :h].mean(axis=1)
        r = (A / (A + self._half_sat[:, None]))[:, :h].mean(axis=1)
        mean_org = self.organic[:, :h].mean(axis=1)
        self._beta = cfg.media_share / (1 - cfg.media_share) * mean_org / r

        # District-level noise and common random numbers
        self._d_noise = rng.lognormal(0, 0.06, (D, T))
        self._u = rng.uniform(1e-12, 1 - 1e-12, (D, T))

    # ---- mechanics -------------------------------------------------------
    def _adstock(self, spend: np.ndarray) -> np.ndarray:
        A = np.empty_like(spend)
        A[:, 0] = spend[:, 0] / (1 - self.config.adstock_decay)  # start at steady state
        for t in range(1, spend.shape[1]):
            A[:, t] = spend[:, t] + self.config.adstock_decay * A[:, t - 1]
        return A

    def _area_mu(self, spend: np.ndarray) -> np.ndarray:
        A = self._adstock(spend)
        media = self._beta[:, None] * A / (A + self._half_sat[:, None])
        return self.organic + media

    def _district_mu(self, spend: np.ndarray) -> np.ndarray:
        w = self.districts["kpi_weight"].to_numpy()[:, None]
        return self._area_mu(spend)[self._d_area] * w * self._d_noise

    def _district_spend(self, spend: np.ndarray) -> np.ndarray:
        return spend[self._d_area] * self.districts["spend_weight"].to_numpy()[:, None]

    def _frame(self, mu: np.ndarray, spend: np.ndarray, end: int) -> pd.DataFrame:
        counts = poisson.ppf(self._u[:, :end], mu[:, :end]).astype(np.int64)
        d_spend = self._district_spend(spend)[:, :end]
        D = len(self.districts)
        return pd.DataFrame({
            "date": np.tile(self.dates[:end].strftime("%Y-%m-%d"), D),
            "postcode": np.repeat(self.districts["postcode"].to_numpy(), end),
            "kpi": counts.ravel(),
            "spend": np.round(d_spend.ravel(), 2),
        }).sort_values(["date", "postcode"], kind="stable").reset_index(drop=True)

    # ---- public API ------------------------------------------------------
    def history(self) -> pd.DataFrame:
        """Business-as-usual data for the history period: date, postcode, kpi, spend."""
        return self._frame(self._district_mu(self.spend), self.spend, self.config.history_days)

    def run_test(self, spec: GeoTestSpec) -> tuple[pd.DataFrame, dict, pd.DataFrame]:
        """Inject a test. Returns (observed data, ground truth summary, daily truth for treated areas)."""
        unknown = set(spec.treatment_areas) - set(self.areas["area"])
        if unknown:
            raise ValueError(f"unknown postcode areas: {sorted(unknown)}")
        start = int(np.searchsorted(self.dates, pd.Timestamp(spec.start)))
        if self.dates[start] != pd.Timestamp(spec.start):
            raise ValueError("test start is outside the generated horizon")
        if start < self.config.history_days:
            raise ValueError(f"test must start after the history period ends ({self.history_end.date()})")
        test_end = start + spec.test_days
        end = test_end + spec.cooldown_days
        if end > len(self.dates):
            raise ValueError("test plus cooldown runs past the generated horizon; raise future_days")

        g_idx = self.areas.index[self.areas["area"].isin(spec.treatment_areas)].to_numpy()
        spend_t = self.spend.copy()
        m = spec.spend_multiplier + spec.leakage * (1 - spec.spend_multiplier)  # leakage dilutes the change
        spend_t[g_idx, start:test_end] *= m
        if spec.reallocation and spec.kind == "holdout":
            paused = (self.spend[g_idx, start:test_end] - spend_t[g_idx, start:test_end]).sum(axis=0)
            others = np.setdiff1d(np.arange(len(self.areas)), g_idx)
            share = self.spend[others, start:test_end] / self.spend[others, start:test_end].sum(axis=0)
            spend_t[others, start:test_end] += spec.reallocation * paused * share

        mu_t, mu_c = self._district_mu(spend_t), self._district_mu(self.spend)
        observed = self._frame(mu_t, spend_t, end)

        d_mask = np.isin(self._d_area, g_idx)
        obs_t = poisson.ppf(self._u[d_mask], mu_t[d_mask]).sum(axis=0)
        obs_c = poisson.ppf(self._u[d_mask], mu_c[d_mask]).sum(axis=0)
        exp_t, exp_c = mu_t[d_mask].sum(axis=0), mu_c[d_mask].sum(axis=0)
        sp_t, sp_c = spend_t[g_idx].sum(axis=0), self.spend[g_idx].sum(axis=0)

        def window(a: int, b: int) -> dict:
            inc_exp = float(exp_t[a:b].sum() - exp_c[a:b].sum())
            spend_change = float(sp_t[a:b].sum() - sp_c[a:b].sum())
            return {
                "counterfactual_kpi_expected": round(float(exp_c[a:b].sum()), 1),
                "actual_kpi_expected": round(float(exp_t[a:b].sum()), 1),
                "incremental_kpi_expected": round(inc_exp, 1),
                "lift_pct_expected": round(100 * inc_exp / float(exp_c[a:b].sum()), 2),
                "incremental_kpi_realised": int(obs_t[a:b].sum() - obs_c[a:b].sum()),
                "spend_change_gbp": round(spend_change, 2),
                "cost_per_incremental_kpi_gbp": round(spend_change / inc_exp, 2) if inc_exp else None,
            }

        truth = {
            "spec": asdict(spec),
            "test_window": [str(self.dates[start].date()), str(self.dates[test_end - 1].date())],
            "cooldown_window": [str(self.dates[test_end].date()), str(self.dates[end - 1].date())],
            "test_period": window(start, test_end),
            "test_plus_cooldown": window(start, end),
            "world_config": asdict(self.config),
        }
        daily = pd.DataFrame({
            "date": self.dates[:end].strftime("%Y-%m-%d"),
            "expected_actual": exp_t[:end].round(2), "expected_counterfactual": exp_c[:end].round(2),
            "observed_actual": obs_t[:end].astype(int), "observed_counterfactual": obs_c[:end].astype(int),
            "spend_actual": sp_t[:end].round(2), "spend_counterfactual": sp_c[:end].round(2),
        })
        return observed, truth, daily

    def area_reference(self) -> pd.DataFrame:
        """Postcode area to region lookup (what map_geo will use)."""
        return self.areas[["area", "region"]].copy()


def generate_world(config: WorldConfig | None = None) -> SyntheticWorld:
    return SyntheticWorld(config)
