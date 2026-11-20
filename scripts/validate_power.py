"""Is the predicted MDE honest? Check it against real injected tests in fresh synthetic worlds.

For each seed: build a world, run the quality report and select_markets on its history,
then inject a real 28-day holdout into the chosen test group (and into a random group for
comparison) after the history ends, estimate the lift with the same estimator, and
compare the estimation error with the error the power simulation predicted.

If the simulation is calibrated, actual RMSE / predicted RMSE should be close to 1.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from geo_engine.estimators import fit, lift  # noqa: E402
from geo_engine.market_selection import DesignConstraints, select_markets  # noqa: E402
from geo_engine.power import Panel, PowerSettings, simulate_power  # noqa: E402
from geo_engine.quality import quality_report  # noqa: E402
from geo_engine.synthetic import GeoTestSpec, WorldConfig, generate_world  # noqa: E402

STARTS = ["2027-01-11", "2027-02-15"]
L = 28


def area_panel(df: pd.DataFrame) -> pd.DataFrame:
    d = df.assign(geo=df["postcode"].str.extract(r"^([A-Z]+)", expand=False),
                  date=pd.to_datetime(df["date"]))
    out = d.groupby(["date", "geo"], as_index=False)[["kpi", "spend"]].sum()
    out.attrs["unit"] = "postcode_area"
    return out


def estimate(observed: pd.DataFrame, treatment: list[str], controls: list[str], start: str,
             s: PowerSettings) -> float:
    p = Panel(area_panel(observed))
    i = int(np.searchsorted(p.dates, pd.Timestamp(start)))
    pre = p.periods(s.pre_period_days)
    ti, ci = p.index(treatment), p.index(controls)
    y, X = p.Y[:, ti].sum(axis=1), p.Y[:, ci]
    f = fit(s.method, y[i - pre:i], X[i - pre:i], ridge=s.ridge)
    return lift(y[i:i + L], f.predict(X[i:i + L]))


def main(seeds=range(1, 11)) -> dict:
    s = PowerSettings()
    rows = []
    for seed in seeds:
        world = generate_world(WorldConfig(seed=seed))
        hist = area_panel(world.history())
        q = quality_report(hist)
        sl = select_markets(hist, q.geo_flags, DesignConstraints(
            test_days=(L,), n_treatment=(10,), candidates_per_size=30,
            exclude_regions_from_test=("London",), seed=seed), s)
        chosen = sl.options.iloc[0]["treatment"].split(", ")
        pool = [g for g in hist["geo"].unique() if g not in chosen
                and q.geo_flags.set_index("geo").loc[g, "recommendation"] == "keep"]
        rng = np.random.default_rng(seed)
        random_group = sorted(rng.choice([g for g in pool if g not in set(chosen)], 10, replace=False))
        review = set(q.geo_flags.loc[q.geo_flags["recommendation"] == "review", "geo"])
        for label, group in (("selected", chosen), ("random", random_group)):
            controls = [g for g in hist["geo"].unique() if g not in group and g not in review]
            full = simulate_power(hist, group, L, controls, s)
            first, second = full.split()
            for start in STARTS:
                observed, truth, _ = world.run_test(GeoTestSpec(group, start, test_days=L, cooldown_days=0))
                est = estimate(observed, group, controls, start, s)
                true = truth["test_period"]["lift_pct_expected"] / 100
                rows.append({"seed": seed, "design": label, "start": start, "true_lift": true,
                             "estimate": est, "error": est - true,
                             "pred_rmse_full": full.rmse, "pred_rmse_select_half": first.rmse,
                             "pred_rmse_report_half": second.rmse})
        print(f"seed {seed} done", flush=True)
    df = pd.DataFrame(rows)
    out = {}
    for label, g in df.groupby("design"):
        actual = float(np.sqrt(np.mean(g["error"] ** 2)))
        out[label] = {"tests": len(g), "actual_rmse_pct": round(100 * actual, 2),
                      **{k.replace("pred_", "predicted_") + "_pct": round(100 * float(np.sqrt(np.mean(g[k] ** 2))), 2)
                         for k in ("pred_rmse_full", "pred_rmse_select_half", "pred_rmse_report_half")},
                      "mean_error_pct": round(100 * float(g["error"].mean()), 2)}
    (ROOT / "reports").mkdir(exist_ok=True)
    df.to_csv(ROOT / "reports" / "power_validation_runs.csv", index=False)
    (ROOT / "reports" / "power_validation.json").write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))
    return out


if __name__ == "__main__":
    main()
