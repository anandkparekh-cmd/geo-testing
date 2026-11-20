"""select_markets: search for test/control splits and rank them by what they can detect.

1. Decide who is eligible. Geos the quality report marked "review" are left out
   entirely; "avoid as test geo" geos (low volume, dominant) can only be controls. You
   can exclude more geos or whole regions from the test, or from both groups.
2. Generate candidate test groups of several sizes. Each candidate is drawn one geo
   per size band, so every group mixes large and small geos, and kept only if its share
   of the national KPI is within the allowed range.
3. Run simulate_power on every candidate for every test length.
4. Rank: for each test length and group size, the candidate with the lowest MDE on the
   earlier half of the historical windows, so a group that merely got lucky in one
   period is less likely to win. The MDE reported uses all windows. Checked against 40
   real injected tests in 10 fresh synthetic worlds (scripts/validate_power.py): raw
   error came out within about 10% of this prediction (the MDE adds a 15% noise margin
   on top, see power.py), while MDEs from the selection half alone were far too
   optimistic. If
   you give an expected lift, recommend the option that detects it with the target
   power at the lowest cost; otherwise show the trade-off and let you choose.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from .geography import AREA_TO_REGION
from .power import Panel, PowerSettings, simulate_many


@dataclass
class DesignConstraints:
    test_days: tuple[int, ...] = (21, 28, 42)
    n_treatment: tuple[int, ...] = (5, 10, 15, 20)
    candidates_per_size: int = 40
    exclude: tuple[str, ...] = ()                    # in neither test nor control
    exclude_from_test: tuple[str, ...] = ()          # may still be controls
    exclude_regions_from_test: tuple[str, ...] = ()  # e.g. ("London",)
    min_kpi_share: float = 0.02                      # test group's share of national KPI
    max_kpi_share: float = 0.30
    expected_lift: float | None = None               # e.g. -0.08 for a holdout expected to lose 8%
    safety_margin: float = 1.0                       # extra margin on top of PowerSettings.noise_inflation
    seed: int = 0


@dataclass
class DesignShortlist:
    options: pd.DataFrame          # best option per test length and group size
    all_candidates: pd.DataFrame   # every candidate evaluated
    recommended: dict | None
    eligibility: dict
    constraints: DesignConstraints
    settings: PowerSettings
    markdown: str = ""

    def to_dict(self) -> dict:
        return {"recommended": self.recommended, "eligibility": self.eligibility,
                "constraints": asdict(self.constraints), "settings": asdict(self.settings),
                "options": self.options.to_dict(orient="records")}


def _eligibility(panel: Panel, quality_flags: pd.DataFrame | None, c: DesignConstraints,
                 region_of: dict | None) -> tuple[list[str], list[str], dict]:
    geos = list(panel.geos)
    review, avoid = set(), set()
    if quality_flags is not None:
        review = set(quality_flags.loc[quality_flags["recommendation"] == "review", "geo"])
        avoid = set(quality_flags.loc[quality_flags["recommendation"] == "avoid as test geo", "geo"])
    out = set(c.exclude) | review
    not_test = out | avoid | set(c.exclude_from_test)
    if c.exclude_regions_from_test:
        if not region_of:
            raise ValueError("exclude_regions_from_test needs postcode-area geos (or a region lookup)")
        not_test |= {g for g in geos if region_of.get(g) in c.exclude_regions_from_test}
    test_pool = [g for g in geos if g not in not_test]
    control_pool = [g for g in geos if g not in out]
    info = {"geos": len(geos), "test_pool": len(test_pool), "control_pool": len(control_pool),
            "left_out": sorted(out), "control_only": sorted(not_test - out)}
    return test_pool, control_pool, info


def _candidates(test_pool: list[str], share: pd.Series, c: DesignConstraints) -> list[tuple[str, ...]]:
    rng = np.random.default_rng(c.seed)
    ordered = share[test_pool].sort_values(ascending=False).index.to_numpy()
    seen, out = set(), []
    for k in c.n_treatment:
        if k >= len(ordered):
            continue
        bands = np.array_split(ordered, k)
        tries = 0
        made = 0
        while made < c.candidates_per_size and tries < c.candidates_per_size * 20:
            tries += 1
            pick = tuple(sorted(rng.choice(b) for b in bands))
            s = share[list(pick)].sum()
            if pick in seen or not (c.min_kpi_share <= s <= c.max_kpi_share):
                continue
            seen.add(pick)
            out.append(pick)
            made += 1
    return out


def select_markets(geo_data: pd.DataFrame | Panel, quality_flags: pd.DataFrame | None = None,
                   constraints: DesignConstraints | None = None,
                   settings: PowerSettings | None = None) -> DesignShortlist:
    """Rank candidate test groups. quality_flags: QualityReport.geo_flags (optional)."""
    c = constraints or DesignConstraints()
    s = settings or PowerSettings()
    panel = geo_data if isinstance(geo_data, Panel) else Panel(geo_data)
    region_of = AREA_TO_REGION if set(panel.geos) <= set(AREA_TO_REGION) else None
    test_pool, control_pool, info = _eligibility(panel, quality_flags, c, region_of)
    if len(test_pool) < min(c.n_treatment) + 1:
        raise ValueError(f"only {len(test_pool)} geos are eligible for the test group")

    kpi = pd.Series(panel.Y.sum(axis=0), index=panel.geos)
    share = kpi / kpi.sum()
    recent = slice(-max(1, panel.periods(91)), None)
    daily_kpi = pd.Series(panel.Y[recent].mean(axis=0), index=panel.geos) / panel.step_days
    daily_spend = (pd.Series(panel.spend[recent].mean(axis=0), index=panel.geos) / panel.step_days
                   if panel.spend is not None else None)

    rows = []
    for cand in _candidates(test_pool, share, c):
        controls = [g for g in control_pool if g not in cand]
        for r in simulate_many(panel, list(cand), controls, list(c.test_days), s):
            choose_on, later = r.split()
            row = r.summary()
            row["mde_selection_pct"] = round(100 * choose_on.mde, 2)
            row["mde_later_half_pct"] = round(100 * later.mde, 2)
            row["kpi_share_pct"] = round(100 * share[list(cand)].sum(), 2)
            row["test_kpi_per_day"] = round(float(daily_kpi[list(cand)].sum()), 1)
            if daily_spend is not None:
                row["spend_in_test_geos_gbp"] = round(float(daily_spend[list(cand)].sum()) * r.test_days, 0)
            if region_of:
                row["regions"] = len({region_of[g] for g in cand})
            if c.expected_lift is not None:
                row["power_at_expected_pct"] = round(100 * r.power_at(c.expected_lift), 1)
                # cautious check: the noisier of (all windows + safety margin) and (later half alone)
                cautious = r if r.noise * c.safety_margin >= later.noise else later
                scale = c.safety_margin if cautious is r else 1.0
                row["power_cautious_pct"] = round(100 * cautious.power_at(c.expected_lift / scale), 1)
            rows.append(row)
    if not rows:
        raise ValueError("no candidate test group met the KPI share limits; widen min/max_kpi_share")
    allc = pd.DataFrame(rows)
    allc["treatment"] = allc["treatment"].map(lambda t: ", ".join(t))
    best = (allc.sort_values("mde_selection_pct").groupby(["test_days", "n_treatment"], as_index=False).first()
                .sort_values(["test_days", "n_treatment"], ignore_index=True))

    recommended = None
    if c.expected_lift is not None:
        ok = best[best["power_cautious_pct"] >= 100 * s.power]
        if len(ok):
            cost = "spend_in_test_geos_gbp" if "spend_in_test_geos_gbp" in ok else "kpi_share_pct"
            # among options within 10% of the cheapest, take the most robust, then the shortest
            near = ok[ok[cost] <= ok[cost].min() * 1.10]
            pick = near.sort_values(["power_cautious_pct", "test_days", "mde_pct"],
                                    ascending=[False, True, True]).iloc[0]
            recommended = {k: (v.item() if hasattr(v, "item") else v) for k, v in pick.items()}
            recommended["why"] = (f"cheapest option (or within 10% of it, and more robust) that keeps at "
                                  f"least {s.power:.0%} power to detect a {abs(c.expected_lift):.1%} effect "
                                  "on all of history and on its most recent half alone")
    shortlist = DesignShortlist(best, allc, recommended, info, c, s)
    shortlist.markdown = _markdown(shortlist, region_of)
    return shortlist


def _markdown(sl: DesignShortlist, region_of) -> str:
    c, s, b = sl.constraints, sl.settings, sl.options
    has_spend = "spend_in_test_geos_gbp" in b
    lines = ["# Test design shortlist", "",
             f"{len(sl.all_candidates) // len(c.test_days)} candidate test groups simulated over about "
             f"{int(b['n_windows'].min())} historical windows each ({s.method.replace('_', ' ')}, "
             f"{int(b['pre_period_days'].iloc[0])}-day fitting period). MDE is the smallest true lift "
             f"detected {s.power:.0%} of the time at {1 - s.alpha:.0%} confidence (two-sided). Groups are "
             f"ranked on the earlier half of the windows so a lucky group is less likely to win; the MDE "
             f"shown uses all windows.", ""]
    e = sl.eligibility
    lines.append(f"Eligible: {e['test_pool']} of {e['geos']} geos can be in the test group; "
                 f"{e['control_pool']} can be controls."
                 + (f" Left out entirely: {', '.join(e['left_out'])}." if e["left_out"] else "")
                 + (f" Control only: {len(e['control_only'])} geos." if e["control_only"] else ""))
    lines.append("")
    if sl.recommended:
        r = sl.recommended
        lines += [f"**Recommended:** {r['n_treatment']} geos for {r['test_days']} days "
                  f"(MDE {r['mde_pct']:.1f}%, power {r['power_at_expected_pct']:.0f}% at the expected "
                  f"{abs(c.expected_lift):.1%}): {r['treatment']}. {r['why'][0].upper() + r['why'][1:]}.", ""]
    elif c.expected_lift is not None:
        lines += [f"**No option reaches {s.power:.0%} power for a {abs(c.expected_lift):.1%} effect.** "
                  "Use more geos, a longer test, or accept lower power.", ""]
    else:
        lines += ["No expected lift given: pick the cheapest row whose MDE is below the smallest "
                  "effect worth acting on.", ""]
    head = ["Days", "Geos", "MDE", "KPI share"] + (["Spend in test geos"] if has_spend else []) \
        + (["Power at expected"] if c.expected_lift is not None else []) + ["Bias", "Test geos"]
    lines += ["## Best group for each test length and size", "",
              "| " + " | ".join(head) + " |", "|" + " --- |" * len(head)]
    for r in b.itertuples():
        cells = [str(r.test_days), str(r.n_treatment), f"{r.mde_pct:.1f}%", f"{r.kpi_share_pct:.1f}%"]
        if has_spend:
            cells.append(f"£{r.spend_in_test_geos_gbp:,.0f}")
        if c.expected_lift is not None:
            cells.append(f"{r.power_at_expected_pct:.0f}%")
        cells += [f"{r.bias_pct:+.1f}%", r.treatment]
        lines.append("| " + " | ".join(cells) + " |")
    lines += ["", "*Spend in test geos* is what the test geos would normally spend over the test: "
              "paused in a holdout. *Bias* is the average error with no test running; it should be "
              "small next to the MDE."]
    return "\n".join(lines)
