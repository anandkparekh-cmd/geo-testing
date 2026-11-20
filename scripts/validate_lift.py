"""Do estimate_lift's confidence intervals hold their stated coverage?

For each fresh synthetic world: two random 10-geo designs (outside London, no low-volume
or flagged geos), two test start dates, and two scenarios per design and date:
  - holdout: spend switched off for 28 days; the truth is the injected effect
  - no test: nothing changes; the truth is zero
A 90% interval should contain the truth about 90% of the time, and a no-test result
should be called significant about 10% of the time.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from geo_engine.analysis import estimate_lift  # noqa: E402
from geo_engine.quality import quality_report  # noqa: E402
from geo_engine.geography import AREA_TO_REGION  # noqa: E402
from geo_engine.synthetic import GeoTestSpec, WorldConfig, generate_world  # noqa: E402

STARTS = ["2027-01-11", "2027-02-15"]
L = 28


def area_panel(df: pd.DataFrame) -> pd.DataFrame:
    d = df.assign(geo=df["postcode"].str.extract(r"^([A-Z]+)", expand=False),
                  date=pd.to_datetime(df["date"]))
    out = d.groupby(["date", "geo"], as_index=False)[["kpi", "spend"]].sum()
    out.attrs["unit"] = "postcode_area"
    return out


def main(seeds=range(1, 11), tag: str = "") -> dict:
    rows = []
    for seed in seeds:
        world = generate_world(WorldConfig(seed=seed))
        hist = area_panel(world.history())
        flags = quality_report(hist).geo_flags.set_index("geo")
        keep = [g for g in flags.index if flags.loc[g, "recommendation"] == "keep"
                and AREA_TO_REGION[g] != "London"]
        review = set(flags.index[flags["recommendation"] == "review"])
        end = len(world.dates)
        bau = area_panel(world._frame(world._district_mu(world.spend), world.spend, end))
        rng = np.random.default_rng(seed)
        for d in range(2):
            group = sorted(rng.choice(keep, 10, replace=False).tolist())
            controls = [g for g in flags.index if g not in group and g not in review]
            for start in STARTS:
                observed, truth, _ = world.run_test(GeoTestSpec(group, start, test_days=L, cooldown_days=0))
                for scenario, data, true in (("holdout", area_panel(observed),
                                              truth["test_period"]["lift_pct_expected"] / 100),
                                             ("no test", bau, 0.0)):
                    r = estimate_lift(data, group, start, L, controls=controls)
                    rows.append({"seed": seed, "design": d, "start": start, "scenario": scenario,
                                 "true_lift": true, "estimate": r.test["lift_pct"] / 100,
                                 "rmse": r.null_rmse, "n_windows": r.n_null_windows,
                                 "ci_low": r.test["lift_ci_pct"][0] / 100, "ci_high": r.test["lift_ci_pct"][1] / 100,
                                 "significant": r.test["significant"]})
        print(f"seed {seed} done", flush=True)
    df = pd.DataFrame(rows)
    df["covered"] = (df["ci_low"] <= df["true_lift"]) & (df["true_lift"] <= df["ci_high"])
    out = {
        "estimates": len(df),
        "coverage_of_90pct_interval": round(float(df["covered"].mean()), 3),
        "coverage_holdout": round(float(df.loc[df["scenario"] == "holdout", "covered"].mean()), 3),
        "coverage_no_test": round(float(df.loc[df["scenario"] == "no test", "covered"].mean()), 3),
        "false_positive_rate_no_test": round(float(df.loc[df["scenario"] == "no test", "significant"].mean()), 3),
        "detection_rate_holdout": round(float(df.loc[df["scenario"] == "holdout", "significant"].mean()), 3),
        "actual_rmse_pct": round(100 * float(np.sqrt(np.mean((df["estimate"] - df["true_lift"]) ** 2))), 2),
        "predicted_rmse_pct": round(100 * float(np.sqrt(np.mean(df["rmse"] ** 2))), 2),
        "mean_true_holdout_lift_pct": round(100 * float(df.loc[df["scenario"] == "holdout", "true_lift"].mean()), 2),
    }
    (ROOT / "reports").mkdir(exist_ok=True)
    out["seeds"] = [int(x) for x in seeds]
    df.to_csv(ROOT / "reports" / f"lift_validation{tag}_runs.csv", index=False)
    (ROOT / "reports" / f"lift_validation{tag}.json").write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))
    return out


if __name__ == "__main__":
    # seeds 1-10 were used to set noise_inflation; seeds 11-20 are a fresh check
    if len(sys.argv) > 1 and sys.argv[1] == "fresh":
        main(range(11, 21), tag="_fresh")
    else:
        main()
