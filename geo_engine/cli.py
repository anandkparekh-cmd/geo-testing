"""Command line for the engine.

  python -m geo_engine.cli quality data/synthetic/history.csv
  python -m geo_engine.cli quality my_export.csv --col kpi="New Customers" --col postcode="Billing Postcode"
  python -m geo_engine.cli design data/synthetic/history.csv --expected-lift -0.08 --exclude-region London
  python -m geo_engine.cli analyse data/synthetic/test_demo.csv --test-geos AB,CF,CW,G,HD,KW,LE,ME,PL,S \
      --start 2027-01-11 --days 28 --cooldown 14
  python -m geo_engine.cli launch data/synthetic/history.csv --name jan_holdout \
      --test-geos BD,CA,DA,FY,HP,LE,RG,SP,WR,WS --start 2027-01-11 --days 21 --cooldown 14
  python -m geo_engine.cli monitor platform_location_report.csv --name jan_holdout \
      --test-geos BD,CA,DA,FY,HP,LE,RG,SP,WR,WS --start 2027-01-11 --days 21

Writes reports/<name>_quality.md and reports/<name>_quality.json, plus the
cleaned, area-level data as data/clean/<name>_<unit>.csv.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .analysis import estimate_lift, placebo_test, robustness_suite
from .ingest import ingest
from .launch import export_targeting
from .monitoring import SpendPlan, leakage_check, monitoring_markdown, pacing_report
from .map_geo import UNITS, map_geo
from .market_selection import DesignConstraints, select_markets
from .power import PowerSettings
from .quality import quality_report
from .report import lift_chart, lift_markdown

ROOT = Path(__file__).resolve().parents[1]


def cmd_quality(args) -> None:
    cmap = dict(c.split("=", 1) for c in (args.col or []))
    result = ingest(args.file, column_map=cmap or None)
    geo = map_geo(result.data, unit=args.unit, lookup=args.lookup)
    report = quality_report(geo, result.missing_dates)

    name = Path(args.file).name.split(".")[0]
    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "data" / "clean").mkdir(parents=True, exist_ok=True)
    md = "\n".join(["# Ingest summary", "", "```", result.summary(), "```", "", report.markdown])
    (ROOT / "reports" / f"{name}_quality.md").write_text(md)
    payload = {"source": result.source, "data_version": result.data_version,
               "column_map": result.column_map, "ingest_issues": result.issues, **report.to_dict()}
    (ROOT / "reports" / f"{name}_quality.json").write_text(json.dumps(payload, indent=2, default=str))
    geo.to_csv(ROOT / "data" / "clean" / f"{name}_{args.unit}.csv", index=False)
    print(result.summary())
    print(f"\nQuality status: {report.status}")
    for c in report.checks:
        print(f"  {c['result']:>4}  {c['check']}: {c['detail']}")
    print(f"\nReport: reports/{name}_quality.md")


def cmd_design(args) -> None:
    cmap = dict(c.split("=", 1) for c in (args.col or []))
    result = ingest(args.file, column_map=cmap or None)
    geo = map_geo(result.data, unit=args.unit, lookup=args.lookup)
    report = quality_report(geo, result.missing_dates)
    c = DesignConstraints(test_days=tuple(args.days), n_treatment=tuple(args.geos),
                          candidates_per_size=args.candidates, exclude=tuple(args.exclude or ()),
                          exclude_from_test=tuple(args.exclude_from_test or ()),
                          exclude_regions_from_test=tuple(args.exclude_region or ()),
                          max_kpi_share=args.max_share, expected_lift=args.expected_lift, seed=args.seed)
    s = PowerSettings(alpha=args.alpha, power=args.power, method=args.method)
    sl = select_markets(geo, report.geo_flags, c, s)
    name = Path(args.file).name.split(".")[0]
    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "reports" / f"{name}_design.md").write_text(sl.markdown)
    payload = {"source": result.source, "data_version": result.data_version,
               "quality_status": report.status, **sl.to_dict()}
    (ROOT / "reports" / f"{name}_design.json").write_text(json.dumps(payload, indent=2, default=str))
    sl.all_candidates.to_csv(ROOT / "reports" / f"{name}_design_candidates.csv", index=False)
    print(sl.markdown)
    print(f"\nReport: reports/{name}_design.md")


def cmd_analyse(args) -> None:
    cmap = dict(c.split("=", 1) for c in (args.col or []))
    result = ingest(args.file, column_map=cmap or None)
    geo = map_geo(result.data, unit=args.unit, lookup=args.lookup)
    test = [g.strip() for g in args.test_geos.split(",")]
    if args.controls:
        controls = [g.strip() for g in args.controls.split(",")]
    else:  # same rule as design: leave out geos the quality report wants reviewed
        flags = quality_report(geo, result.missing_dates).geo_flags
        review = set(flags.loc[flags["recommendation"] == "review", "geo"]) - set(test)
        controls = [g for g in geo["geo"].unique() if g not in set(test) | review]
    s = PowerSettings(alpha=args.alpha, method=args.method)
    lift = estimate_lift(geo, test, args.start, args.days, args.cooldown, controls, s)
    placebo = placebo_test(geo, lift, settings=s)
    robust = robustness_suite(geo, lift, s)
    truth = json.loads(Path(args.truth).read_text()) if args.truth else None
    name = Path(args.file).name.split(".")[0]
    (ROOT / "reports").mkdir(exist_ok=True)
    md = lift_markdown(lift, placebo, robust, truth, title=f"Test readout: {name}")
    chart = lift_chart(lift, ROOT / "reports" / f"{name}_lift.png")
    (ROOT / "reports" / f"{name}_lift.md").write_text(md + f"\n\n![Result]({chart.name})\n")
    payload = {"source": result.source, "data_version": result.data_version, **lift.to_dict(),
               "placebo": placebo.to_dict(), "robustness": robust}
    (ROOT / "reports" / f"{name}_lift.json").write_text(json.dumps(payload, indent=2, default=str))
    lift.daily.to_csv(ROOT / "reports" / f"{name}_lift_daily.csv", index=False)
    print(md)
    print(f"\nReport: reports/{name}_lift.md (chart {chart.name})")


def _controls(geo, test, missing_dates):
    flags = quality_report(geo, missing_dates).geo_flags
    review = set(flags.loc[flags["recommendation"] == "review", "geo"]) - set(test)
    return [g for g in sorted(geo["geo"].unique()) if g not in set(test) | review]


def cmd_launch(args) -> None:
    result = ingest(args.file)
    geo = map_geo(result.data)
    test = [g.strip() for g in args.test_geos.split(",")]
    controls = ([g.strip() for g in args.controls.split(",")] if args.controls
                else _controls(geo, test, result.missing_dates))
    x = export_targeting(result.data, test, controls, args.start, args.days, ROOT / "targeting",
                         args.name, args.kind, args.cooldown)
    print(f"{len(x.test_districts)} districts to {x.action} across {len(test)} test areas.")
    for kind, paths in x.files.items():
        for f in paths:
            print(f"  {kind}: {Path(f).relative_to(ROOT)}")
    for w in x.warnings:
        print(f"Note: {w}")


def cmd_monitor(args) -> None:
    result = ingest(args.file, spend_only=True)       # results are never read while a test runs
    geo = map_geo(result.data)
    test = [g.strip() for g in args.test_geos.split(",")]
    controls = ([g.strip() for g in args.controls.split(",")] if args.controls
                else [g for g in sorted(geo["geo"].unique()) if g not in set(test)])
    plan = SpendPlan(test, controls, args.start, args.days, args.kind, args.multiplier)
    pace, leak = pacing_report(geo, plan), leakage_check(geo, plan)
    md = monitoring_markdown(pace, leak, args.name)
    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "reports" / f"{args.name}_monitor.md").write_text(md)
    (ROOT / "reports" / f"{args.name}_monitor.json").write_text(
        json.dumps({"pacing": pace, "leakage": leak}, indent=2, default=str))
    print(md)
    print(f"Report: reports/{args.name}_monitor.md")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="Geo incrementality engine")
    sub = p.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("quality", help="ingest a file, map to geos and write the quality report")
    q.add_argument("file")
    q.add_argument("--unit", choices=UNITS, default="postcode_area")
    q.add_argument("--lookup", help="CSV of postcode district or area -> geo, for --unit custom")
    q.add_argument("--col", action="append", metavar="STANDARD=YOURS",
                   help="map a column, e.g. --col kpi='New Customers' (repeatable)")
    q.set_defaults(func=cmd_quality)

    d = sub.add_parser("design", help="quality checks, then search and rank test/control splits")
    d.add_argument("file")
    d.add_argument("--unit", choices=UNITS, default="postcode_area")
    d.add_argument("--lookup")
    d.add_argument("--col", action="append", metavar="STANDARD=YOURS")
    d.add_argument("--days", type=int, nargs="+", default=[21, 28, 42], help="test lengths to try")
    d.add_argument("--geos", type=int, nargs="+", default=[5, 10, 15, 20], help="test group sizes to try")
    d.add_argument("--candidates", type=int, default=40, help="candidate groups per size")
    d.add_argument("--expected-lift", type=float, help="e.g. -0.08 for a holdout expected to lose 8%%")
    d.add_argument("--exclude", action="append", help="geo left out of test and control")
    d.add_argument("--exclude-from-test", action="append", help="geo allowed only as a control")
    d.add_argument("--exclude-region", action="append", help="region kept out of the test group")
    d.add_argument("--max-share", type=float, default=0.30, help="max test-group share of national KPI")
    d.add_argument("--alpha", type=float, default=0.10)
    d.add_argument("--power", type=float, default=0.80)
    d.add_argument("--method", choices=["synthetic_control", "tbr"], default="synthetic_control")
    d.add_argument("--seed", type=int, default=0)
    d.set_defaults(func=cmd_design)

    a = sub.add_parser("analyse", help="estimate a test's lift, with placebo and robustness checks")
    a.add_argument("file")
    a.add_argument("--test-geos", required=True, help="comma-separated, e.g. AB,CF,LS")
    a.add_argument("--start", required=True, help="first day of the test, YYYY-MM-DD")
    a.add_argument("--days", type=int, required=True, help="test length in days")
    a.add_argument("--cooldown", type=int, default=0, help="days after the test counted for carryover")
    a.add_argument("--controls", help="comma-separated control geos (default: all others not flagged)")
    a.add_argument("--unit", choices=UNITS, default="postcode_area")
    a.add_argument("--lookup")
    a.add_argument("--col", action="append", metavar="STANDARD=YOURS")
    a.add_argument("--alpha", type=float, default=0.10)
    a.add_argument("--method", choices=["synthetic_control", "tbr"], default="synthetic_control")
    a.add_argument("--truth", help="synthetic ground-truth JSON to compare against (testing only)")
    a.set_defaults(func=cmd_analyse)

    la = sub.add_parser("launch", help="write Google Ads and Meta location lists and a launch checklist")
    la.add_argument("file", help="your KPI-by-postcode history (district level)")
    la.add_argument("--name", required=True)
    la.add_argument("--test-geos", required=True)
    la.add_argument("--controls")
    la.add_argument("--start", required=True)
    la.add_argument("--days", type=int, required=True)
    la.add_argument("--cooldown", type=int, default=0)
    la.add_argument("--kind", choices=["holdout", "heavy_up"], default="holdout")
    la.set_defaults(func=cmd_launch)

    mo = sub.add_parser("monitor", help="pacing and leakage checks during a test (reads spend only)")
    mo.add_argument("file", help="spend by postcode and date, e.g. a platform location report")
    mo.add_argument("--name", required=True)
    mo.add_argument("--test-geos", required=True)
    mo.add_argument("--controls")
    mo.add_argument("--start", required=True)
    mo.add_argument("--days", type=int, required=True)
    mo.add_argument("--kind", choices=["holdout", "heavy_up"], default="holdout")
    mo.add_argument("--multiplier", type=float, help="planned spend multiplier (holdout 0, heavy-up e.g. 1.5)")
    mo.set_defaults(func=cmd_monitor)
    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
