"""Data quality report for geo-level KPI data, run before any test design.

Checks: history length, dates with no data, national tracking drops, zero runs
(possible tracking gaps), low-volume geos, unusual spikes and dips (per geo
and per region), level shifts, dominant geos and spend coverage.

Spikes and dips are found on each geo's share of the national total, so
national events (Black Friday, Christmas) cancel out and only local ones remain.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .geography import AREA_TO_REGION


@dataclass
class QualitySettings:
    min_history_days: int = 182        # below this: not ready
    good_history_days: int = 365       # below this: warning (seasonality cannot be matched)
    low_volume_per_day: float = 10.0   # mean KPI per day below this: avoid as a test geo
    z_threshold: float = 4.5           # robust z-score for an unusual period in one geo
    region_z_threshold: float = 4.0    # same for a whole region
    min_effect: float = 0.25           # and at least this far from expected (25%) for a geo
    region_min_effect: float = 0.10    # same for a whole region (larger, steadier totals)
    min_expected: float = 5.0          # ignore periods where expected KPI is tiny
    window_days: int = 29              # rolling window for the expected share
    zero_run_days: int = 3             # consecutive zero days that count as a gap
    national_drop: float = 0.4         # national total below 40% of its rolling median
    shift_ratio: float = 1.35          # share in first vs last quarter-year differs by this factor
    dominant_share: float = 0.10       # one geo above 10% of the total
    region_lookup: dict[str, str] | None = None  # geo -> region, defaults to UK area -> region


@dataclass
class QualityReport:
    status: str                         # "ready", "ready with flags", "not ready"
    checks: list[dict]
    geo_flags: pd.DataFrame
    events: pd.DataFrame
    markdown: str
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"status": self.status, "checks": self.checks, "meta": self.meta,
                "geo_flags": self.geo_flags.to_dict(orient="records"),
                "events": self.events.astype({"start": str, "end": str}).to_dict(orient="records")}


def _runs(mask: np.ndarray, max_gap: int = 0) -> list[tuple[int, int]]:
    """Start/end index pairs of True runs, joining runs separated by up to max_gap False values."""
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return []
    runs, start, prev = [], idx[0], idx[0]
    for i in idx[1:]:
        if i - prev > max_gap + 1:
            runs.append((start, prev))
            start = i
        prev = i
    runs.append((start, prev))
    return runs


def _unusual(Y: pd.DataFrame, s: QualitySettings, window: int, scope: str, z_threshold: float,
             min_effect: float, max_gap: int, spans: tuple[int, ...]) -> pd.DataFrame:
    """Spikes and dips in each column's share of the total. Y: dates x units.

    Residuals use the Anscombe transform, 2 * (sqrt(y + 3/8) - sqrt(e + 3/8)), which makes
    count noise roughly symmetric with constant variance. Each residual series is averaged
    over several spans (e.g. 1, 3 and 7 days) so a short sharp drop and a longer, gentler
    promotion are both caught; a robust (median/MAD) z-score per column and span absorbs
    any noise beyond Poisson."""
    N = Y.sum(axis=1)
    share = Y.div(N.replace(0, np.nan), axis=0)
    exp_share = share.rolling(window, center=True, min_periods=window // 2).median()
    expected = exp_share.mul(N, axis=0)
    r = 2 * (np.sqrt(Y.clip(lower=0) + 0.375) - np.sqrt(expected.clip(lower=0) + 0.375))
    spike = pd.DataFrame(False, index=Y.index, columns=Y.columns)
    dip = spike.copy()
    zmax = pd.DataFrame(0.0, index=Y.index, columns=Y.columns)
    for w in spans:
        m = r.rolling(w, center=True, min_periods=w).mean()
        z = (m - m.median()) / ((m - m.median()).abs().median() * 1.4826).replace(0, np.nan)
        pct = (Y.rolling(w, center=True, min_periods=w).sum()
               / expected.rolling(w, center=True, min_periods=w).sum() - 1)
        big = (expected.rolling(w, center=True, min_periods=w).mean() >= s.min_expected)
        up = (z > z_threshold) & (pct > min_effect) & big
        down = (z < -z_threshold) & (pct < -min_effect) & big
        # a flagged window marks every period inside it
        spike |= up.astype(float).rolling(w, center=True, min_periods=1).max().astype(bool)
        dip |= down.astype(float).rolling(w, center=True, min_periods=1).max().astype(bool)
        zmax = np.maximum(zmax, z.abs().fillna(0))
    rows = []
    for col in Y.columns:
        for direction, mask in (("spike", spike[col].to_numpy()), ("dip", dip[col].to_numpy())):
            for a, b in _runs(mask, max_gap=max_gap):
                y, e = Y[col].iloc[a:b + 1].sum(), expected[col].iloc[a:b + 1].sum()
                rows.append({"scope": scope, "where": col, "start": Y.index[a], "end": Y.index[b],
                             "periods": b - a + 1, "direction": direction,
                             "size_pct": round(100 * (y / e - 1), 1),
                             "max_abs_z": round(float(zmax[col].iloc[a:b + 1].max()), 1)})
    return pd.DataFrame(rows, columns=["scope", "where", "start", "end", "periods", "direction",
                                       "size_pct", "max_abs_z"])


def quality_report(geo_data: pd.DataFrame, missing_dates: list[str] | None = None,
                   settings: QualitySettings | None = None) -> QualityReport:
    """Run the quality checks on map_geo output (date, geo, kpi, spend).

    missing_dates: dates the source file had no rows for (IngestResult.missing_dates).
    """
    s = settings or QualitySettings()
    Y = geo_data.pivot(index="date", columns="geo", values="kpi").sort_index().fillna(0)
    has_spend = geo_data["spend"].notna().any()
    dates = Y.index
    step_days = int(pd.Series(dates).diff().dt.days.median())
    weekly = step_days == 7
    per_day = 1 / step_days
    window = max(5, round(s.window_days / step_days) | 1)
    span_days = (dates.max() - dates.min()).days + step_days
    unit = geo_data.attrs.get("unit", "geo")
    region_of = s.region_lookup or (AREA_TO_REGION if unit == "postcode_area" else None)
    checks: list[dict] = []
    flags: dict[str, list[str]] = {g: [] for g in Y.columns}

    def check(name, result, detail):
        checks.append({"check": name, "result": result, "detail": detail})

    # 1. History length
    yrs = span_days / 365.25
    if span_days < s.min_history_days:
        check("History length", "fail", f"{span_days} days; need at least {s.min_history_days}, ideally 2 years")
    elif span_days < s.good_history_days:
        check("History length", "warn", f"{span_days} days; under a year, so seasonality cannot be matched")
    else:
        check("History length", "pass", f"{span_days} days ({yrs:.1f} years)")

    # 2. Dates with no data in the source file
    missing_dates = missing_dates or []
    if missing_dates:
        share = len(missing_dates) / len(dates)
        check("Dates with no data", "fail" if share > 0.02 else "warn",
              f"{len(missing_dates)} dates had no rows in the file (zero-filled), e.g. {missing_dates[:5]}")
    else:
        check("Dates with no data", "pass", "every date present")

    # 3. National drops (tracking outage across all geos)
    N = Y.sum(axis=1)
    roll = N.rolling(window, center=True, min_periods=window // 2).median()
    drops = N[(N < s.national_drop * roll) & ~N.index.strftime("%Y-%m-%d").isin(missing_dates)]
    if len(drops):
        check("National drops", "warn",
              f"{len(drops)} dates below {s.national_drop:.0%} of normal: "
              + ", ".join(f"{d.date()} ({v / roll[d]:.0%})" for d, v in drops.items()))
    else:
        check("National drops", "pass", f"no date below {s.national_drop:.0%} of the normal national level")

    # 4. Zero runs inside a geo
    min_run = 2 if weekly else s.zero_run_days
    zero_geos = []
    for g in Y.columns:
        if Y[g].mean() * per_day < 3:
            continue
        runs = [(a, b) for a, b in _runs((Y[g] == 0).to_numpy()) if b - a + 1 >= min_run]
        if runs:
            zero_geos.append(g)
            a, b = max(runs, key=lambda r: r[1] - r[0])
            flags[g].append(f"zero run of {b - a + 1} periods from {dates[a].date()} (tracking gap?)")
    check("Zero runs", "warn" if zero_geos else "pass",
          f"{len(zero_geos)} geos with {min_run}+ consecutive zero periods: {zero_geos[:10]}" if zero_geos
          else "no unexpected runs of zeros")

    # 5. Low volume
    mean_per_day = Y.mean() * per_day
    low = mean_per_day[mean_per_day < s.low_volume_per_day].sort_values()
    for g, v in low.items():
        flags[g].append(f"low volume ({v:.1f} per day)")
    check("Low-volume geos", "warn" if len(low) else "pass",
          f"{len(low)} geos average under {s.low_volume_per_day:g} per day: {list(low.index)}" if len(low)
          else f"every geo averages {s.low_volume_per_day:g}+ per day")

    # 6. Unusual spikes and dips: regions first, then geos
    spans = (1, 2) if weekly else (1, 3, 7)
    regional = pd.DataFrame(columns=["scope", "where", "start", "end", "direction"])
    if region_of:
        R = Y.T.groupby(Y.columns.map(region_of)).sum().T
        regional = _unusual(R, s, window, "region", s.region_z_threshold, s.region_min_effect,
                            max_gap=1, spans=spans)
    local = _unusual(Y, s, window, "geo", s.z_threshold, s.min_effect, max_gap=1, spans=spans)
    if len(local) and len(regional) and region_of:
        def covered(row):
            reg = regional[(regional["where"] == region_of.get(row["where"]))
                           & (regional["direction"] == row["direction"])]
            return bool(((reg["start"] <= row["end"]) & (reg["end"] >= row["start"])).any())
        local = local[~local.apply(covered, axis=1)]
    events = pd.concat([regional, local], ignore_index=True)
    if len(events):
        events = events.sort_values(["scope", "start"], key=lambda c: c.map({"region": 0, "geo": 1})
                                    if c.name == "scope" else c, ignore_index=True)
    for g, n in local["where"].value_counts().items():
        if n >= 3:
            flags[g].append(f"{n} unusual spikes or dips")
    check("Unusual spikes and dips", "warn" if len(events) else "pass",
          f"{len(regional)} regional and {len(local)} single-geo events; review before choosing test dates"
          if len(events) else "none found")

    # 7. Level shifts: share in the first vs last quarter-year
    q = max(4, round(91 / step_days))
    if len(dates) >= 4 * q:
        first, last = Y.iloc[:q].sum() / N.iloc[:q].sum(), Y.iloc[-q:].sum() / N.iloc[-q:].sum()
        ratio = (last / first.replace(0, np.nan)).dropna()
        shifted = ratio[(ratio > s.shift_ratio) | (ratio < 1 / s.shift_ratio)]
        for g, v in shifted.items():
            flags[g].append(f"share {'up' if v > 1 else 'down'} {abs(v - 1):.0%} first vs last quarter-year")
        check("Level shifts", "warn" if len(shifted) else "pass",
              f"{len(shifted)} geos changed share by more than {s.shift_ratio - 1:.0%}: {list(shifted.index)}"
              if len(shifted) else f"no geo's share moved more than {s.shift_ratio - 1:.0%}")

    # 8. Dominant geos
    share = Y.sum() / Y.values.sum()
    dom = share[share > s.dominant_share]
    for g, v in dom.items():
        flags[g].append(f"dominant ({v:.0%} of total): hard to balance test and control")
    check("Dominant geos", "warn" if len(dom) else "pass",
          f"{list(dom.index)} each hold over {s.dominant_share:.0%}" if len(dom)
          else f"no geo above {s.dominant_share:.0%} of the total")

    # 9. Spend coverage
    if has_spend:
        S = geo_data.pivot(index="date", columns="geo", values="spend").sort_index().fillna(0)
        no_spend = S.columns[S.sum() == 0].tolist()
        dark_days = int((S.sum(axis=1) == 0).sum())
        for g in no_spend:
            flags[g].append("no spend recorded: cannot be a holdout test geo")
        result = "warn" if no_spend or dark_days else "pass"
        check("Spend coverage", result,
              f"{len(no_spend)} geos with no spend {no_spend[:10]}; {dark_days} dates with no spend anywhere"
              if result == "warn" else "spend recorded in every geo")
    else:
        check("Spend coverage", "info", "no spend column: lift can be measured, iROAS and cost per "
                                       "incremental conversion need spend from the platforms")

    # Per-geo flags and recommendation
    gf = pd.DataFrame({"geo": Y.columns, "mean_per_day": mean_per_day.round(1).to_numpy(),
                       "share_pct": (100 * share).round(2).to_numpy()})
    gf["flags"] = gf["geo"].map(lambda g: "; ".join(flags[g]))

    def recommend(g):
        f = " ".join(flags[g])
        if "zero run" in f or "share " in f or "unusual" in f or "no spend" in f:
            return "review"
        if "low volume" in f or "dominant" in f:
            return "avoid as test geo"
        return "keep"
    gf["recommendation"] = gf["geo"].map(recommend)
    gf = gf.sort_values(["recommendation", "share_pct"], ascending=[True, False],
                        key=lambda c: c.map({"review": 0, "avoid as test geo": 1, "keep": 2})
                        if c.name == "recommendation" else c, ignore_index=True)

    results = {c["result"] for c in checks}
    status = "not ready" if "fail" in results else ("ready with flags" if "warn" in results else "ready")
    meta = {"unit": unit, "frequency": "weekly" if weekly else "daily", "geos": Y.shape[1],
            "periods": Y.shape[0], "start": str(dates.min().date()), "end": str(dates.max().date())}
    md = _markdown(status, checks, gf, events, meta)
    return QualityReport(status, checks, gf, events, md, meta)


def _markdown(status, checks, gf, events, meta) -> str:
    icon = {"pass": "Pass", "warn": "Warning", "fail": "Fail", "info": "Note"}
    out = [f"# Data quality report", "",
           f"**Status: {status}.** {meta['geos']} geos ({meta['unit'].replace('_', ' ')}), "
           f"{meta['periods']} {meta['frequency']} periods, {meta['start']} to {meta['end']}.", "",
           "| Check | Result | Detail |", "| --- | --- | --- |"]
    out += [f"| {c['check']} | {icon[c['result']]} | {c['detail']} |" for c in checks]
    flagged = gf[gf["recommendation"] != "keep"]
    out += ["", f"## Geos to review or avoid as test geos ({len(flagged)} of {len(gf)})", ""]
    if len(flagged):
        out += ["| Geo | Per day | Share | Recommendation | Why |", "| --- | --- | --- | --- | --- |"]
        out += [f"| {r.geo} | {r.mean_per_day:g} | {r.share_pct:.2f}% | {r.recommendation} | {r.flags} |"
                for r in flagged.itertuples()]
    else:
        out.append("None.")
    out += ["", f"## Unusual spikes and dips ({len(events)})", ""]
    if len(events):
        out += ["| Scope | Where | Start | Periods | Type | Size vs expected |",
                "| --- | --- | --- | --- | --- | --- |"]
        out += [f"| {e.scope} | {e.where} | {pd.Timestamp(e.start).date()} | {e.periods} | {e.direction} | "
                f"{e.size_pct:+.1f}% |" for e in events.itertuples()]
    else:
        out.append("None.")
    out += ["", "Geos marked *avoid as test geo* can still sit in the control pool. Geos marked "
            "*review* need a decision before market selection: fix the data, exclude the geo, or "
            "keep it and avoid the affected dates."]
    return "\n".join(out)
