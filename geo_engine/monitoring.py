"""Monitoring a live test: pacing_report and leakage_check.

Both read SPEND ONLY. Any KPI column is dropped on the way in, so nothing here can show
how the test is going. That is deliberate: peeking at results mid-test tempts people
to stop early when the numbers look good, which inflates false positives.

- pacing_report: is spend running to plan? Test areas against the planned change, and
  control areas against their normal level. Spend rising in the controls during a
  holdout usually means the platform moved the paused budget there, which inflates
  the controls and biases the result towards the channel looking more valuable.
- leakage_check: is spend reaching places it should not? In a holdout, any spend
  left in test areas dilutes the test. In a heavy-up, extra spend landing outside the
  test areas does the same.

The baseline is each area's average daily spend over the 28 days before the test.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class SpendPlan:
    treatment: list[str]
    controls: list[str]
    start: str
    test_days: int
    kind: str = "holdout"              # or "heavy_up"
    spend_multiplier: float | None = None  # holdout 0.0; heavy-up e.g. 1.5
    baseline_days: int = 28
    tolerance: float = 0.15            # spend within +/-15% of plan counts as on track
    step_tolerance: float = 0.05       # control spend jumping more than 5% the week the test starts
    leak_tolerance: float = 0.02       # a holdout may keep up to 2% of normal spend in test areas

    def __post_init__(self):
        if self.spend_multiplier is None:
            self.spend_multiplier = 0.0 if self.kind == "holdout" else 1.5


def _spend(geo_data: pd.DataFrame, plan: SpendPlan):
    if "spend" not in geo_data or geo_data["spend"].isna().all():
        raise ValueError("monitoring needs spend by geo (a platform location report or your spend data)")
    d = geo_data[["date", "geo", "spend"]]                      # KPI is never read
    S = d.pivot(index="date", columns="geo", values="spend").sort_index().fillna(0)
    i0 = int(np.searchsorted(S.index, pd.Timestamp(plan.start)))
    if i0 >= len(S) or S.index[i0] != pd.Timestamp(plan.start):
        raise ValueError("the test start date is not in the spend data yet")
    b = plan.baseline_days
    if i0 < b:
        raise ValueError(f"need {b} days of spend before the test as a baseline")
    last = min(len(S), i0 + plan.test_days)
    missing = sorted(set(plan.treatment + plan.controls) - set(S.columns))
    if missing:
        raise ValueError(f"no spend data for {missing}")
    return S, i0, last


def pacing_report(geo_data: pd.DataFrame, plan: SpendPlan) -> dict:
    """Spend so far against plan, for the test and control groups. Reads spend only."""
    S, i0, last = _spend(geo_data, plan)
    b = plan.baseline_days
    days = last - i0
    test, ctrl = S[plan.treatment].sum(axis=1), S[plan.controls].sum(axis=1)
    base_t, base_c = test.iloc[i0 - b:i0].mean(), ctrl.iloc[i0 - b:i0].mean()
    planned_t = base_t * plan.spend_multiplier * days
    actual_t, actual_c = float(test.iloc[i0:last].sum()), float(ctrl.iloc[i0:last].sum())
    ctrl_change = actual_c / (base_c * days) - 1
    # a step at launch is the tell-tale of reallocated budget; comparing with the week just
    # before the test keeps seasonal drift in the 28-day baseline from hiding it
    pre_week = ctrl.iloc[i0 - 7:i0].mean()
    ctrl_step = (actual_c / days) / pre_week - 1

    weeks = []
    for w, a in enumerate(range(i0, last, 7), 1):
        z = min(a + 7, last)
        n = z - a
        weeks.append({"week": w, "from": str(S.index[a].date()), "days": n,
                      "test_spend_gbp": round(float(test.iloc[a:z].sum()), 0),
                      "test_planned_gbp": round(base_t * plan.spend_multiplier * n, 0),
                      "control_spend_gbp": round(float(ctrl.iloc[a:z].sum()), 0),
                      "control_baseline_gbp": round(base_c * n, 0)})

    flags = []
    if plan.kind == "holdout":
        kept = actual_t / (base_t * days)
        if kept > plan.leak_tolerance:
            flags.append(f"test areas are still spending {kept:.1%} of normal (plan: 0%); run leakage_check")
    else:
        off = actual_t / planned_t - 1
        if abs(off) > plan.tolerance:
            flags.append(f"test areas are {'over' if off > 0 else 'under'}-delivering: {off:+.0%} vs plan")
    if ctrl_step > plan.step_tolerance or ctrl_change > plan.tolerance:
        jump = ctrl_step if ctrl_step > plan.step_tolerance else ctrl_change
        since = "the week before the test" if ctrl_step > plan.step_tolerance else "its 28-day baseline"
        flags.append(f"control spend is up {jump:.0%} on {since}"
                     + (": the paused budget looks to have moved into the controls. Cut campaign budgets "
                        "by the test areas' share (or check for a budget change), or the result will "
                        "overstate the channel" if plan.kind == "holdout"
                        else ": extra budget may be landing outside the test areas"))
    elif ctrl_change < -plan.tolerance:
        flags.append(f"control spend is down {abs(ctrl_change):.0%} on its baseline: controls are "
                     "under-delivering, check budgets and bids")
    return {"as_of": str(S.index[last - 1].date()), "days_elapsed": days, "test_days": plan.test_days,
            "test_spend_gbp": round(actual_t, 0), "test_planned_gbp": round(planned_t, 0),
            "control_spend_gbp": round(actual_c, 0), "control_baseline_gbp": round(base_c * days, 0),
            "control_change_pct": round(100 * ctrl_change, 1),
            "control_step_at_launch_pct": round(100 * ctrl_step, 1), "weeks": weeks,
            "status": "check" if flags else "on track", "flags": flags or ["spend is running to plan"]}


def leakage_check(geo_data: pd.DataFrame, plan: SpendPlan) -> dict:
    """Spend reaching places it should not, by area. Reads spend only."""
    S, i0, last = _spend(geo_data, plan)
    b, days = plan.baseline_days, last - i0
    base = S.iloc[i0 - b:i0].mean()
    during = S.iloc[i0:last].sum()
    rows, flags = [], []
    if plan.kind == "holdout":
        for g in plan.treatment:
            normal = base[g] * days
            share = during[g] / normal if normal else 0.0
            rows.append({"area": g, "spend_gbp": round(float(during[g]), 0),
                         "normal_gbp": round(float(normal), 0), "share_of_normal_pct": round(100 * share, 1)})
        leaked = float(during[plan.treatment].sum())
        normal_total = float(base[plan.treatment].sum() * days)
        rate = leaked / normal_total if normal_total else 0.0
        leaking = [r["area"] for r in rows if r["share_of_normal_pct"] > 100 * plan.leak_tolerance]
        if rate > plan.leak_tolerance or leaking:
            flags.append(f"£{leaked:,.0f} ({rate:.1%} of normal) still spent in test areas"
                         + (f", mostly in {', '.join(sorted(leaking, key=lambda g: -during[g])[:5])}" if leaking else "")
                         + ". Check the exclusions and the Presence setting; the readout will measure a "
                           "diluted test")
        summary = {"leaked_spend_gbp": round(leaked, 0), "leak_rate_pct": round(100 * rate, 1)}
    else:
        extra_t = float(during[plan.treatment].sum() - base[plan.treatment].sum() * days)
        extra_c = float(during[plan.controls].sum() - base[plan.controls].sum() * days)
        outside = max(extra_c, 0) / (max(extra_t, 0) + max(extra_c, 0)) if (extra_t > 0 or extra_c > 0) else 0.0
        if outside > 0.10:
            flags.append(f"{outside:.0%} of the extra spend is landing outside the test areas; check the "
                         "heavy-up campaign's location targets")
        summary = {"extra_spend_test_gbp": round(extra_t, 0), "extra_spend_controls_gbp": round(extra_c, 0),
                   "share_outside_pct": round(100 * outside, 1)}
        rows = [{"area": g, "spend_gbp": round(float(during[g]), 0),
                 "normal_gbp": round(float(base[g] * days), 0),
                 "share_of_normal_pct": round(100 * float(during[g] / (base[g] * days)), 1) if base[g] else None}
                for g in plan.treatment]
    rows.sort(key=lambda r: -(r["share_of_normal_pct"] or 0))
    return {"as_of": str(S.index[last - 1].date()), "days_elapsed": days, **summary, "by_area": rows,
            "status": "check" if flags else "clean", "flags": flags or ["no leakage found"]}


def monitoring_markdown(pacing: dict, leakage: dict, name: str = "test") -> str:
    p, l = pacing, leakage
    lines = [f"# Monitoring: {name}", "",
             f"As of {p['as_of']}: day {p['days_elapsed']} of {p['test_days']}. "
             f"**Pacing: {p['status']}. Leakage: {l['status']}.** These checks read spend only, never results.", ""]
    lines += [f"- {f}" for f in p["flags"] + l["flags"] if f not in ("spend is running to plan", "no leakage found")]
    if p["status"] == "on track" and l["status"] == "clean":
        lines.append("- Spend is running to plan and none is reaching places it should not.")
    lines += ["", "## Spend by week", "", "| Week | From | Test spend | Test plan | Control spend | Control baseline |",
              "| --- | --- | --- | --- | --- | --- |"]
    lines += [f"| {w['week']} | {w['from']} | £{w['test_spend_gbp']:,.0f} | £{w['test_planned_gbp']:,.0f} | "
              f"£{w['control_spend_gbp']:,.0f} | £{w['control_baseline_gbp']:,.0f} |" for w in p["weeks"]]
    lines += ["", f"Control spend so far: {p['control_change_pct']:+.1f}% against the 28-day pre-test average, "
              f"{p['control_step_at_launch_pct']:+.1f}% against the week just before the test.",
              "", "## Test areas", "", "| Area | Spend so far | Normal | Share of normal |", "| --- | --- | --- | --- |"]
    lines += [f"| {r['area']} | £{r['spend_gbp']:,.0f} | £{r['normal_gbp']:,.0f} | {r['share_of_normal_pct']}% |"
              for r in l["by_area"]]
    return "\n".join(lines) + "\n"
