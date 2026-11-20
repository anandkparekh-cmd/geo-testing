"""Command line for a test plan: what the agents (and you) call at each step.

  python -m geo_engine.plan_cli new jan_holdout
  python -m geo_engine.plan_cli brief geotests/jan_holdout --question "..." --channel meta_prospecting \\
      --test-type holdout --kpi new_customers --kpi-source "orders database" \\
      --success-metric cost_per_incremental --decision-rule "..." --expected-lift -0.08
  python -m geo_engine.plan_cli approve geotests/jan_holdout G1 --by Anand
  python -m geo_engine.plan_cli data geotests/jan_holdout data/synthetic/history.csv
  python -m geo_engine.plan_cli design geotests/jan_holdout --exclude-region London
  python -m geo_engine.plan_cli choose geotests/jan_holdout            # the recommended option
  python -m geo_engine.plan_cli approve geotests/jan_holdout G2 --by Anand
  python -m geo_engine.plan_cli launch geotests/jan_holdout --start 2027-01-11
  python -m geo_engine.plan_cli approve geotests/jan_holdout G3 --by Anand   # locks the plan
  python -m geo_engine.plan_cli monitor geotests/jan_holdout spend_report.csv
  python -m geo_engine.plan_cli analyse geotests/jan_holdout full_data.csv
  python -m geo_engine.plan_cli approve geotests/jan_holdout G4 --by Anand
  python -m geo_engine.plan_cli readout geotests/jan_holdout --decision keep \
      --recommendation "..." --by Anand
  python -m geo_engine.plan_cli status geotests/jan_holdout
  python -m geo_engine.plan_cli list
"""

from __future__ import annotations

import argparse
import json
import sys

import yaml

from . import workflow as wf
from .plan import PlanError, TestPlan

FOLDER = "geotests"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run a geo test step by step from its test plan")
    sub = p.add_subparsers(dest="cmd", required=True)

    n = sub.add_parser("new", help="start a test plan")
    n.add_argument("test_id")
    n.add_argument("--folder", default=FOLDER)

    s = sub.add_parser("status", help="where the test is and what comes next")
    s.add_argument("plan")
    s.add_argument("--json", action="store_true")

    b = sub.add_parser("brief", help="fill in the brief")
    b.add_argument("plan")
    for f in ("question", "channel", "kpi", "kpi_source", "decision_rule", "notes"):
        b.add_argument(f"--{f.replace('_', '-')}")
    b.add_argument("--test-type", choices=wf.TEST_TYPES)
    b.add_argument("--success-metric", choices=wf.SUCCESS_METRICS)
    b.add_argument("--expected-lift", type=float)

    a = sub.add_parser("approve", help="approve a gate")
    a.add_argument("plan")
    a.add_argument("gate", choices=["G1", "G2", "G3", "G4"])
    a.add_argument("--by", required=True)
    a.add_argument("--note", default="")

    d = sub.add_parser("data", help="load data and run the quality checks")
    d.add_argument("plan")
    d.add_argument("file")
    d.add_argument("--unit", default="postcode_area")
    d.add_argument("--col", action="append", metavar="STANDARD=YOURS")

    de = sub.add_parser("design", help="search and rank test/control splits")
    de.add_argument("plan")
    de.add_argument("--days", type=int, nargs="+", default=[21, 28, 42])
    de.add_argument("--geos", type=int, nargs="+", default=[5, 10, 15, 20])
    de.add_argument("--exclude-region", action="append", default=[])
    de.add_argument("--exclude", action="append", default=[])
    de.add_argument("--candidates", type=int, default=40)

    c = sub.add_parser("choose", help="choose a design option (default: recommended)")
    c.add_argument("plan")
    c.add_argument("--days", type=int)
    c.add_argument("--geos", type=int)
    c.add_argument("--cooldown", type=int, default=14)
    c.add_argument("--by", default="you")

    la = sub.add_parser("launch", help="write location lists and the launch checklist")
    la.add_argument("plan")
    la.add_argument("--start", required=True)

    m = sub.add_parser("monitor", help="pacing and leakage checks from a spend report")
    m.add_argument("plan")
    m.add_argument("file")

    an = sub.add_parser("analyse", help="estimate the lift after the test")
    an.add_argument("plan")
    an.add_argument("file")

    ro = sub.add_parser("readout", help="record the decision and recommendation, close the test")
    ro.add_argument("plan")
    ro.add_argument("--decision", required=True, choices=wf.DECISIONS)
    ro.add_argument("--recommendation", required=True)
    ro.add_argument("--by", required=True)
    ro.add_argument("--retest-date")

    ls = sub.add_parser("list", help="every test and what comes next")
    ls.add_argument("--folder", default=FOLDER)

    dv = sub.add_parser("deviation", help="change a locked field, with a reason")
    dv.add_argument("plan")
    dv.add_argument("--section", required=True, choices=["brief", "data", "design"])
    dv.add_argument("--field", required=True)
    dv.add_argument("--value", required=True, help="YAML value, e.g. 21 or [BD, CA]")
    dv.add_argument("--reason", required=True)
    dv.add_argument("--by", required=True)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "new":
            plan = TestPlan.new(args.test_id, args.folder)
            print(f"Created {plan.path}")
        elif args.cmd == "list":
            tests = wf.list_tests(args.folder)
            if not tests:
                print(f"No tests in {args.folder}/ yet. Start one with: new <test_id>")
            for t in tests:
                print(f"{t['test_id']}: {t['status']}. Next ({t['who']}): {t['next']}")
            return 0
        else:
            plan = TestPlan.load(args.plan)
            if args.cmd == "status":
                if args.json:
                    print(json.dumps({"status": plan.status, "next": wf.next_step(plan)}, indent=2))
                else:
                    print(wf.summary(plan))
                return 0
            if args.cmd == "brief":
                fields = {k: getattr(args, k) for k in ("question", "channel", "test_type", "kpi",
                                                        "kpi_source", "success_metric", "decision_rule",
                                                        "expected_lift", "notes")}
                missing = wf.set_brief(plan, **fields)
                print("Brief complete." if not missing else f"Still missing: {', '.join(missing)}")
            elif args.cmd == "approve":
                wf.approve(plan, args.gate, args.by, args.note)
                print(f"{args.gate} approved by {args.by}.")
            elif args.cmd == "data":
                cmap = dict(x.split("=", 1) for x in (args.col or [])) or None
                out = wf.run_data(plan, args.file, args.unit, cmap)
                print(f"Data quality: {out['status']}. Report: {out['report']}")
            elif args.cmd == "design":
                out = wf.run_design(plan, args.days, args.geos, args.exclude_region, args.exclude,
                                    args.candidates)
                print(f"{out['options']} options. Recommended: {out['recommended']}. Report: {out['report']}")
            elif args.cmd == "choose":
                out = wf.choose_design(plan, args.days, args.geos, args.cooldown, args.by)
                print(f"Chosen: {', '.join(out['treatment_geos'])} (MDE {out['mde_pct']}%)")
            elif args.cmd == "launch":
                out = wf.run_launch(plan, args.start)
                print(f"Test {out['start']} to {out['end']}, {out['districts']} districts to {out['action']}.")
                for w in out["warnings"]:
                    print(f"Note: {w}")
            elif args.cmd == "monitor":
                out = wf.run_monitor(plan, args.file)
                print(f"Day {out['day']}: pacing {out['pacing']}, leakage {out['leakage']}.")
                for f in out["flags"]:
                    print(f"- {f}")
            elif args.cmd == "analyse":
                out = wf.run_analysis(plan, args.file)
                print(f"Lift {out['lift_pct']}% (90% interval {out['lift_ci_pct']}). Report: {out['report']}")
            elif args.cmd == "readout":
                report = wf.write_readout(plan, args.decision, args.recommendation, args.by, args.retest_date)
                print(f"Readout written: {report}")
            elif args.cmd == "deviation":
                plan.record_deviation(args.section, args.field, yaml.safe_load(args.value), args.reason, args.by)
                plan.save()
                print("Deviation logged.")
            print(wf.summary(TestPlan.load(plan.path)))
    except (PlanError, ValueError, FileNotFoundError) as e:   # IngestError is a ValueError
        print(f"Not done: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
