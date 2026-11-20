"""Command line for the synthetic data generator.

  python -m geo_engine.synthetic.cli history
  python -m geo_engine.synthetic.cli test --name demo --random 10 --exclude-region London --start 2027-01-11

Observed data goes to data/synthetic/. Ground truth goes to data/_ground_truth/,
which agents are never given access to.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from .generator import GeoTestSpec, WorldConfig, generate_world

ROOT = Path(__file__).resolve().parents[2]
DATA, TRUTH = ROOT / "data" / "synthetic", ROOT / "data" / "_ground_truth"


def _world(args):
    return generate_world(WorldConfig(seed=args.seed))


def cmd_history(args) -> None:
    world = _world(args)
    DATA.mkdir(parents=True, exist_ok=True)
    TRUTH.mkdir(parents=True, exist_ok=True)
    hist = world.history()
    hist.to_csv(DATA / "history.csv", index=False)
    world.area_reference().to_csv(DATA / "postcode_areas.csv", index=False)
    world.events.to_csv(TRUTH / "world_events.csv", index=False)
    print(f"history.csv: {len(hist):,} rows, {hist['postcode'].nunique()} districts, "
          f"{hist['date'].min()} to {hist['date'].max()}")


def cmd_test(args) -> None:
    world = _world(args)
    if args.areas:
        areas = [a.strip().upper() for a in args.areas.split(",")]
    else:
        pool = world.areas
        if args.exclude_region:
            pool = pool[~pool["region"].isin(args.exclude_region)]
        rng = np.random.default_rng(args.seed + 1000)
        areas = sorted(rng.choice(pool["area"].to_numpy(), size=args.random, replace=False).tolist())
    spec = GeoTestSpec(treatment_areas=areas, start=args.start, test_days=args.days,
                       cooldown_days=args.cooldown, kind=args.kind, spend_multiplier=args.multiplier,
                       leakage=args.leakage, reallocation=args.reallocation)
    observed, truth, daily = world.run_test(spec)
    DATA.mkdir(parents=True, exist_ok=True)
    TRUTH.mkdir(parents=True, exist_ok=True)
    observed.to_csv(DATA / f"test_{args.name}.csv", index=False)
    (TRUTH / f"test_{args.name}.json").write_text(json.dumps(truth, indent=2, default=str))
    daily.to_csv(TRUTH / f"test_{args.name}_daily.csv", index=False)
    tp = truth["test_period"]
    print(f"test_{args.name}.csv: {len(observed):,} rows; treatment areas: {', '.join(areas)}")
    print(f"true lift in test period: {tp['lift_pct_expected']}% "
          f"({tp['incremental_kpi_expected']:,.0f} conversions, spend change GBP {tp['spend_change_gbp']:,.0f})")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="Synthetic UK geo data generator")
    p.add_argument("--seed", type=int, default=42)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("history", help="write business-as-usual history").set_defaults(func=cmd_history)

    t = sub.add_parser("test", help="inject a geo test and write data plus ground truth")
    t.add_argument("--name", required=True)
    g = t.add_mutually_exclusive_group(required=True)
    g.add_argument("--areas", help="comma-separated postcode areas, e.g. B,LS,NG")
    g.add_argument("--random", type=int, help="pick this many treatment areas at random")
    t.add_argument("--exclude-region", action="append", help="region to leave out of a random pick")
    t.add_argument("--start", required=True, help="YYYY-MM-DD, after the history period")
    t.add_argument("--days", type=int, default=28)
    t.add_argument("--cooldown", type=int, default=14)
    t.add_argument("--kind", choices=["holdout", "heavy_up"], default="holdout")
    t.add_argument("--multiplier", type=float, default=None,
                   help="spend multiplier in treatment areas (holdout default 0, heavy-up default 1.5)")
    t.add_argument("--leakage", type=float, default=0.0,
                   help="execution error: share of the planned spend change that never happens, e.g. 0.08")
    t.add_argument("--reallocation", type=float, default=0.0,
                   help="execution error: share of paused spend moved into control areas, e.g. 0.6")
    t.set_defaults(func=cmd_test)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
