"""Sanity-check charts for the synthetic data. Writes reports/synthetic_sanity_check.png."""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
S1, S2, S3 = "#2a78d6", "#eb6834", "#1baf7a"

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "axes.edgecolor": GRID,
    "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2, "text.color": INK,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8, "axes.spines.top": False,
    "axes.spines.right": False, "axes.spines.left": False, "font.size": 10,
    "axes.titlesize": 12, "axes.titleweight": "bold", "axes.titlelocation": "left",
})


def main() -> None:
    hist = pd.read_csv(ROOT / "data/synthetic/history.csv", parse_dates=["date"])
    truth = json.loads((ROOT / "data/_ground_truth/test_demo.json").read_text())
    daily = pd.read_csv(ROOT / "data/_ground_truth/test_demo_daily.csv", parse_dates=["date"])

    fig, axes = plt.subplots(3, 1, figsize=(11, 12.5))
    fig.subplots_adjust(hspace=0.62, left=0.08, right=0.86, top=0.95, bottom=0.05)

    # 1. National daily conversions
    ax = axes[0]
    nat = hist.groupby("date")["kpi"].sum()
    ax.plot(nat.index, nat.values, color="#9ec5f4", lw=1, label="Daily")
    ax.plot(nat.index, nat.rolling(7, center=True).mean(), color=S1, lw=2, label="7-day average")
    ax.set_title("National conversions: weekly cycle, winter peak, Black Friday spike, Christmas dip")
    ax.set_ylabel("Conversions per day")
    ax.set_ylim(0, None)
    ax.legend(loc="lower left", frameon=False, ncol=2)
    for d, txt in [("2025-12-01", "Cyber Monday"), ("2025-12-25", "Christmas Day")]:
        y = nat.loc[d]
        ax.annotate(txt, (pd.Timestamp(d), y), xytext=(8, 0 if txt.startswith("Cyber") else -4),
                    textcoords="offset points", fontsize=9, color=INK2, va="center")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))

    # 2. Example areas, weekly, indexed to own mean
    ax = axes[1]
    hist["area"] = hist["postcode"].str.extract(r"^([A-Z]+)", expand=False)
    weekly = (hist[hist["area"].isin(["B", "LS", "ZE"])]
              .groupby(["area", pd.Grouper(key="date", freq="W-SUN")])["kpi"].sum().unstack(0))
    weekly = weekly.iloc[1:-1]
    labels = {"B": "Birmingham (B), large", "LS": "Leeds (LS), large", "ZE": "Shetland (ZE), tiny"}
    for area, col in zip(["B", "LS", "ZE"], [S1, S2, S3]):
        idx = 100 * weekly[area] / weekly[area].mean()
        ax.plot(idx.index, idx.values, color=col, lw=2 if area != "ZE" else 1.4, label=labels[area])
        ax.annotate(labels[area].split(" (")[0], (idx.index[-1], idx.values[-1]), xytext=(6, 0),
                    textcoords="offset points", fontsize=9, color=INK2, va="center")
    ax.set_title("Areas share national patterns, but small areas are far noisier")
    ax.set_ylabel("Weekly conversions\n(index, area mean = 100)")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.1), frameon=False, ncol=3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))

    # 3. Demo holdout: treated areas vs their counterfactual
    ax = axes[2]
    d = daily[daily["date"] >= "2026-12-07"].set_index("date")
    start, end = map(pd.Timestamp, truth["test_window"])
    cool_end = pd.Timestamp(truth["cooldown_window"][1])
    ax.axvspan(start, end, color=GRID, alpha=0.6, lw=0)
    ax.axvspan(end, cool_end, color=GRID, alpha=0.25, lw=0)
    ax.plot(d.index, d["observed_counterfactual"].rolling(7, center=True).mean(), color=S2, lw=2,
            label="Without the test (true counterfactual)")
    ax.plot(d.index, d["observed_actual"].rolling(7, center=True).mean(), color=S1, lw=2,
            label="With the test (what the data shows)")
    tp = truth["test_period"]
    ymax = d["observed_counterfactual"].rolling(7, center=True).mean().max()
    ax.text(start + (end - start) / 2, ymax * 1.06, "Test: spend off", ha="center", fontsize=9, color=INK2)
    ax.text(end + (cool_end - end) / 2, ymax * 1.06, "Cooldown", ha="center", fontsize=9, color=INK2)
    ax.set_ylim(None, ymax * 1.12)
    ax.set_title(f"Demo holdout in 10 areas: true effect {tp['lift_pct_expected']:.1f}% "
                 f"({tp['incremental_kpi_expected']:,.0f} conversions over 4 weeks)")
    ax.set_ylabel("Conversions per day, treated areas\n(7-day average)")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.1), frameon=False, ncol=2)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b %Y"))

    out = ROOT / "reports" / "synthetic_sanity_check.png"
    out.parent.mkdir(exist_ok=True)
    fig.savefig(out, dpi=150)
    print(out)


if __name__ == "__main__":
    main()
