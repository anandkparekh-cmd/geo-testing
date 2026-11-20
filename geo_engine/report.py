"""Readable output for a test result: a markdown readout and a chart.

The engine's job here is to state what the numbers say, plainly, with their intervals.
The recommendation (scale, cut, retest) belongs to the Readout agent and to you.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd

from .analysis import LiftResult, PlaceboResult

SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
S1, S2 = "#2a78d6", "#eb6834"


def _n(x: float) -> str:
    return f"{x:,.0f}"


def lift_markdown(r: LiftResult, placebo: PlaceboResult | None = None, robustness: dict | None = None,
                  truth: dict | None = None, title: str = "Test readout") -> str:
    t, conf = r.test, f"{1 - r.alpha:.0%}"
    days = (pd.Timestamp(r.test_window[1]) - pd.Timestamp(r.test_window[0])).days + 1
    lo, hi = t["lift_ci_pct"]
    verdict = ("The change is statistically clear" if t["significant"]
               else "The result is not distinguishable from no effect")
    out = [f"# {title}", "",
           f"**{len(r.treatment)} test geos, {r.test_window[0]} to {r.test_window[1]} ({days} days): "
           f"KPI {'fell' if t['lift_pct'] < 0 else 'rose'} {abs(t['lift_pct']):.1f}% against the counterfactual "
           f"({conf} interval {lo:+.1f}% to {hi:+.1f}%), about {_n(abs(t['incremental_kpi']))} "
           f"{'fewer' if t['incremental_kpi'] < 0 else 'more'} conversions.** {verdict} "
           f"(p = {t['p_value']:.3f}).", "",
           f"| Measure | Estimate | {conf} interval |", "| --- | --- | --- |",
           f"| Lift, test period | {t['lift_pct']:+.1f}% | {lo:+.1f}% to {hi:+.1f}% |",
           f"| Incremental KPI, test period | {t['incremental_kpi']:+,.0f} | "
           f"{t['incremental_kpi_ci'][0]:+,.0f} to {t['incremental_kpi_ci'][1]:+,.0f} |",
           f"| Actual vs counterfactual KPI | {_n(t['actual_kpi'])} vs {_n(t['counterfactual_kpi'])} | |"]
    if r.test_plus_cooldown:
        c = r.test_plus_cooldown
        out.append(f"| Lift, test + cooldown to {r.cooldown_window[1]} | {c['lift_pct']:+.1f}% | "
                   f"{c['lift_ci_pct'][0]:+.1f}% to {c['lift_ci_pct'][1]:+.1f}% |")
        out.append(f"| Incremental KPI, test + cooldown | {c['incremental_kpi']:+,.0f} | "
                   f"{c['incremental_kpi_ci'][0]:+,.0f} to {c['incremental_kpi_ci'][1]:+,.0f} |")
    if r.spend:
        sp = r.spend
        sign = "-" if sp["spend_change_gbp"] < 0 else "+"
        out.append(f"| Spend change in test geos | {sign}£{abs(sp['spend_change_gbp']):,.0f} | vs {sp['baseline']} |")
        if "cost_per_incremental_kpi_gbp" in sp:
            ci = sp.get("cost_per_incremental_kpi_ci_gbp")
            out.append(f"| Cost per incremental conversion | £{sp['cost_per_incremental_kpi_gbp']:,.2f} | "
                       + (f"£{ci[0]:,.2f} to £{ci[1]:,.2f}" if ci else "not significant") + " |")
    out += ["", "## Checks", ""]
    if placebo:
        out += [f"- **Placebo in time:** {placebo.time_false_positive_rate:.0%} of {placebo.time_windows} "
                f"no-test windows before the test would have been called significant (expected about "
                f"{placebo.alpha:.0%} or less).",
                f"- **Placebo in space:** {placebo.space_groups} fake test groups drawn from the controls, read over "
                f"the same dates: {placebo.space_false_positive_rate:.0%} looked significant; "
                f"{placebo.space_p_value:.0%} stood out as much as the real result.",
                f"- **Verdict:** {'; '.join(placebo.notes)}."]
    if robustness:
        out += ["", f"**Robustness: {robustness['verdict']}** (range {robustness['range_pct'][0]:+.1f}% to "
                f"{robustness['range_pct'][1]:+.1f}%).", "", "| Alternative | Lift | Same direction |",
                "| --- | --- | --- |"]
        out += [f"| {c['check']} | {c['lift_pct']:+.1f}% | {'yes' if c['same_direction'] else 'no'} |"
                for c in robustness["checks"]]
    if truth:
        tp = truth["test_period"]
        out += ["", "## Ground truth (synthetic data only)", "",
                f"True lift {tp['lift_pct_expected']:+.2f}% ({tp['incremental_kpi_expected']:+,.0f} conversions); "
                f"estimate {t['lift_pct']:+.2f}% ({t['incremental_kpi']:+,.0f}). Truth inside the interval: "
                f"{'yes' if lo <= tp['lift_pct_expected'] <= hi else 'no'}."]
        if "cost_per_incremental_kpi_gbp" in tp and r.spend and "cost_per_incremental_kpi_gbp" in r.spend:
            out.append(f"True cost per incremental conversion £{tp['cost_per_incremental_kpi_gbp']:,.2f}; "
                       f"estimate £{r.spend['cost_per_incremental_kpi_gbp']:,.2f}.")
    out += ["", "## Method", "",
            f"{r.method.replace('_', ' ').capitalize()} fitted on the {r.pre_period_days} days before the test "
            f"against {len(r.controls)} control geos. The interval comes from replaying the same design over "
            f"{r.n_null_windows} earlier windows with no test (error {100 * r.null_rmse:.2f}%, widened 15% and "
            f"read with a t critical value of {r.critical:.2f}); across 160 injected synthetic tests this gave "
            f"90% intervals that held the truth 95% of the time."]
    if r.top_controls:
        out.append("Largest control weights: " + ", ".join(f"{g} {w:.2f}" for g, w in r.top_controls.items()) + ".")
    return "\n".join(out)


def lift_chart(r: LiftResult, path: str | Path, weeks_before: int = 8) -> Path:
    plt.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "axes.edgecolor": GRID,
        "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2, "text.color": INK,
        "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8, "axes.spines.top": False,
        "axes.spines.right": False, "axes.spines.left": False, "font.size": 10,
        "axes.titlesize": 12, "axes.titleweight": "bold", "axes.titlelocation": "left"})
    d = r.daily.set_index("date")
    t0, t1 = pd.Timestamp(r.test_window[0]), pd.Timestamp(r.test_window[1])
    end = pd.Timestamp(r.cooldown_window[1]) if r.cooldown_window else t1
    d = d[d.index >= t0 - pd.Timedelta(weeks=weeks_before)]
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(11, 8), gridspec_kw={"height_ratios": [3, 2]})
    fig.subplots_adjust(hspace=0.5, left=0.09, right=0.97, top=0.92, bottom=0.1)
    for ax in (a1, a2):
        ax.axvspan(t0, t1 + pd.Timedelta(days=1), color=GRID, alpha=0.6, lw=0)
        if end > t1:
            ax.axvspan(t1 + pd.Timedelta(days=1), end + pd.Timedelta(days=1), color=GRID, alpha=0.25, lw=0)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    a1.plot(d.index, d["counterfactual"].rolling(7, center=True, min_periods=4).mean(), color=S2, lw=2,
            ls="--", label="Counterfactual (no test)")
    a1.plot(d.index, d["actual"].rolling(7, center=True, min_periods=4).mean(), color=S1, lw=2,
            label="Actual")
    a1.set_title(f"Test geos: actual vs counterfactual (7-day average). Lift {r.test['lift_pct']:+.1f}%")
    top = a1.get_ylim()[1]
    a1.text(t0 + (t1 - t0) / 2, top, "Test", ha="center", va="bottom", fontsize=9, color=INK2)
    if end > t1:
        a1.text(t1 + (end - t1) / 2, top, "Cooldown", ha="center", va="bottom", fontsize=9, color=INK2)
    a1.set_ylabel("KPI per day")
    a1.legend(loc="upper center", bbox_to_anchor=(0.5, -0.12), frameon=False, ncol=2)
    post = d[d["period"] != "pre"]
    a2.plot(post.index, post["cumulative_incremental"], color=S1, lw=2)
    a2.axhline(0, color=INK2, lw=0.8)
    lo, hi = r.test["incremental_kpi_ci"]
    a2.errorbar([t1], [r.test["incremental_kpi"]], yerr=[[r.test["incremental_kpi"] - lo],
                [hi - r.test["incremental_kpi"]]], color=INK, capsize=5, lw=1.2, fmt="o", ms=5)
    a2.annotate(f"{r.test['incremental_kpi']:+,.0f}\n({lo:+,.0f} to {hi:+,.0f})", (t1, lo),
                xytext=(10, 4), textcoords="offset points", fontsize=9, color=INK2, va="bottom")
    a2.set_title(f"Cumulative incremental KPI, with the {1 - r.alpha:.0%} interval at the end of the test")
    a2.set_ylabel("Cumulative incremental KPI")
    a2.set_xlim(a1.get_xlim())
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path
