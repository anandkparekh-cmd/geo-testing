"""The orchestrator's rules: which step comes next, what each gate needs, and the steps
themselves, each one calling the engine and writing its result into the test plan.

The agents (phase 2, step 2) talk to you and decide how to word things; these rules
decide what is allowed. Nothing here calls an LLM.

Flow and owners:
  draft            Brief agent completes the brief            -> you approve G1
  brief_approved   Data agent runs run_data                   -> data_ready
  data_ready       Design agent runs run_design, you choose   -> you approve G2
  design_approved  Launch agent runs run_launch               -> you approve G3 (locks)
  locked / live    Monitoring agent runs run_monitor weekly; after the test,
                   Analysis agent runs run_analysis           -> analysed
  analysed         you approve G4                             -> results_approved
  results_approved Readout agent writes the readout           -> closed
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from .analysis import estimate_lift, outside_events, placebo_test, robustness_suite
from .ingest import ingest
from .launch import export_targeting
from .map_geo import map_geo
from .market_selection import DesignConstraints, select_markets
from .monitoring import SpendPlan, leakage_check, monitoring_markdown, pacing_report
from .plan import BRIEF_REQUIRED, GATES, PlanError, TestPlan, now
from .power import PowerSettings
from .quality import quality_report
from .report import lift_chart, lift_markdown

TEST_TYPES = ("holdout", "heavy_up")
SUCCESS_METRICS = ("cost_per_incremental", "iroas", "incremental_kpi")


def _rel(plan: TestPlan, p: Path) -> str:
    try:
        return str(Path(p).resolve().relative_to(plan.folder.resolve()))
    except ValueError:
        return str(p)


def _write(plan: TestPlan, name: str, text: str) -> str:
    p = plan.folder / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return _rel(plan, p)


def _need(plan: TestPlan, status: str, doing: str) -> None:
    if not plan.at_least(status):
        raise PlanError(f"cannot {doing} yet: the plan is at '{plan.status}' and needs '{status}'. "
                        f"Next step: {next_step(plan)['action']}")


# ---- brief ---------------------------------------------------------------------------
def set_brief(plan: TestPlan, **fields) -> list[str]:
    """Fill in the brief. Returns the required fields still missing."""
    if plan.at_least("brief_approved"):
        raise PlanError("the brief is approved (G1); a change now needs a new approval: use "
                        "reopen_brief() first")
    fields = {k: v for k, v in fields.items() if v is not None}
    if "test_type" in fields and fields["test_type"] not in TEST_TYPES:
        raise PlanError(f"test_type must be one of {TEST_TYPES}")
    if "success_metric" in fields and fields["success_metric"] not in SUCCESS_METRICS:
        raise PlanError(f"success_metric must be one of {SUCCESS_METRICS}")
    if fields.get("expected_lift") is not None:
        e = float(fields["expected_lift"])
        if not -0.5 < e < 0.5 or e == 0:
            raise PlanError("expected_lift is a fraction, e.g. -0.08 for a holdout expected to lose 8%")
        fields["expected_lift"] = e
    plan.update("brief", fields)
    plan.save()
    return missing_brief(plan)


def missing_brief(plan: TestPlan) -> list[str]:
    return [f for f in BRIEF_REQUIRED if not plan.section("brief").get(f)]


def reopen_brief(plan: TestPlan, by: str, reason: str) -> None:
    if plan.at_least("data_ready"):
        raise PlanError("data and design already build on this brief; start a new test plan instead")
    plan.d["gates"]["G1"] = None
    plan.set_status("draft")
    plan.log(f"brief reopened by {by}: {reason}")
    plan.save()


# ---- gates -----------------------------------------------------------------------------
def approve(plan: TestPlan, gate: str, by: str, note: str = "") -> None:
    """Record your approval at a gate, after checking it is ready to approve."""
    if gate not in GATES:
        raise PlanError(f"gate must be one of {list(GATES)}")
    if plan.d["gates"].get(gate):
        raise PlanError(f"{gate} was already approved by {plan.d['gates'][gate]['approved_by']}")
    if gate == "G1":
        if plan.status != "draft":
            raise PlanError("G1 approves a draft brief")
        miss = missing_brief(plan)
        if miss:
            raise PlanError(f"the brief is missing {miss}")
        plan.record_gate(gate, by, note)
        plan.set_status("brief_approved")
    elif gate == "G2":
        if plan.status != "data_ready" or not plan.section("design").get("treatment_geos"):
            raise PlanError("G2 approves a chosen design: run run_design, then choose_design")
        plan.record_gate(gate, by, note)
        plan.set_status("design_approved")
    elif gate == "G3":
        if plan.status != "design_approved" or not plan.section("launch").get("files"):
            raise PlanError("G3 approves the launch: run run_launch first")
        plan.record_gate(gate, by, note)
        plan.lock(by)
        plan.set_status("locked")
    else:
        if plan.status != "analysed":
            raise PlanError("G4 approves results: run run_analysis after the test first")
        plan.record_gate(gate, by, note)
        plan.set_status("results_approved")
    plan.save()


# ---- data ------------------------------------------------------------------------------
def _load_geo(plan: TestPlan, file: str | None = None, spend_only: bool = False):
    data = plan.section("data")
    file = file or data["source_file"]
    res = ingest(file, column_map=data.get("column_map") or None, spend_only=spend_only)
    geo = map_geo(res.data, unit=data.get("geo_unit", "postcode_area"))
    return res, geo


def run_data(plan: TestPlan, file: str, geo_unit: str = "postcode_area",
             column_map: dict | None = None) -> dict:
    """Data agent: ingest, map to geos, quality report. Moves the plan to data_ready."""
    _need(plan, "brief_approved", "load data")
    if plan.at_least("data_ready") and plan.section("design").get("treatment_geos"):
        raise PlanError("a design already builds on the current data; start a new plan to change data")
    res = ingest(file, column_map=column_map)
    geo = map_geo(res.data, unit=geo_unit)
    q = quality_report(geo, res.missing_dates)
    md = "\n".join(["# Ingest summary", "", "```", res.summary(), "```", "", q.markdown])
    report = _write(plan, "quality_report.md", md)
    _write(plan, "quality_report.json", json.dumps({"ingest_issues": res.issues, **q.to_dict()},
                                                     indent=2, default=str))
    flags = q.geo_flags
    plan.update("data", {
        "source_file": str(Path(file).resolve()), "column_map": res.column_map,
        "data_version": res.data_version, "geo_unit": geo_unit, "frequency": res.frequency,
        "postcode_level": res.postcode_level, "history": list(res.date_range),
        "quality_status": q.status,
        "excluded_geos": sorted(flags.loc[flags["recommendation"] == "review", "geo"]),
        "control_only_geos": sorted(flags.loc[flags["recommendation"] == "avoid as test geo", "geo"]),
        "ingest_warnings": [f"{i['check']}: {i['detail']}" for i in res.issues if i["severity"] == "warning"],
        "quality_report": report})
    if q.status == "not ready":
        plan.log("data loaded but not ready; fix the data and run run_data again")
    elif plan.status == "brief_approved":
        plan.set_status("data_ready")
    plan.save()
    return {"status": q.status, "report": report, "checks": q.checks}


# ---- design ----------------------------------------------------------------------------
def run_design(plan: TestPlan, test_days=(21, 28, 42), n_treatment=(5, 10, 15, 20),
               exclude_regions_from_test=(), exclude=(), candidates: int = 40, seed: int = 0,
               max_kpi_share: float = 0.30) -> dict:
    """Design agent: search test/control splits and rank them. You then choose one."""
    _need(plan, "data_ready", "design the test")
    if plan.at_least("design_approved"):
        raise PlanError("the design is approved (G2); it can no longer be re-run")
    data, brief = plan.section("data"), plan.section("brief")
    res, geo = _load_geo(plan)
    if res.data_version != data["data_version"]:
        raise PlanError("the data file changed since run_data; run run_data again")
    q = quality_report(geo, res.missing_dates)
    c = DesignConstraints(test_days=tuple(test_days), n_treatment=tuple(n_treatment),
                          candidates_per_size=candidates, exclude=tuple(exclude),
                          exclude_regions_from_test=tuple(exclude_regions_from_test),
                          max_kpi_share=max_kpi_share, expected_lift=brief.get("expected_lift"), seed=seed)
    s = PowerSettings()
    sl = select_markets(geo, q.geo_flags, c, s)
    report = _write(plan, "design_shortlist.md", sl.markdown)
    _write(plan, "design_shortlist.json", json.dumps(sl.to_dict(), indent=2, default=str))
    sl.all_candidates.to_csv(plan.folder / "design_candidates.csv", index=False)
    keep = ["test_days", "n_treatment", "mde_pct", "kpi_share_pct", "spend_in_test_geos_gbp",
            "power_at_expected_pct", "power_cautious_pct", "bias_pct", "treatment"]
    options = [{k: (v.item() if hasattr(v, "item") else v) for k, v in row.items() if k in keep}
               for row in sl.options.to_dict(orient="records")]
    rec = sl.recommended
    plan.update("design", {
        "options": options,
        "recommended": ({"test_days": rec["test_days"], "n_treatment": rec["n_treatment"], "why": rec["why"]}
                        if rec else None),
        "control_pool": [g for g in geo["geo"].unique() if g not in set(sl.eligibility["left_out"])],
        "constraints": {"exclude_regions_from_test": list(exclude_regions_from_test),
                        "exclude": list(exclude), "max_kpi_share": max_kpi_share},
        "shortlist_report": report})
    plan.save()
    return {"options": len(options), "recommended": plan.section("design")["recommended"], "report": report}


def choose_design(plan: TestPlan, test_days: int | None = None, n_treatment: int | None = None,
                  cooldown_days: int = 14, by: str = "you") -> dict:
    """Pick one option from the shortlist (default: the recommended one)."""
    _need(plan, "data_ready", "choose a design")
    if plan.at_least("design_approved"):
        raise PlanError("the design is approved (G2) and cannot be changed")
    d = plan.section("design")
    if not d.get("options"):
        raise PlanError("no shortlist yet: run run_design first")
    if test_days is None and n_treatment is None:
        if not d.get("recommended"):
            raise PlanError("no recommended option: give test_days and n_treatment")
        test_days, n_treatment = d["recommended"]["test_days"], d["recommended"]["n_treatment"]
    pick = [o for o in d["options"] if o["test_days"] == test_days and o["n_treatment"] == n_treatment]
    if not pick:
        raise PlanError(f"no option with {test_days} days and {n_treatment} geos")
    o = pick[0]
    treat = o["treatment"].split(", ")
    test_type = plan.section("brief")["test_type"]
    plan.update("design", {
        "method": "synthetic_control", "treatment_geos": treat,
        "control_geos": [g for g in d["control_pool"] if g not in set(treat)],
        "test_days": test_days, "cooldown_days": cooldown_days,
        "spend_multiplier": 0.0 if test_type == "holdout" else plan.section("brief").get("spend_multiplier", 1.5),
        "mde_pct": o["mde_pct"], "power_at_expected_pct": o.get("power_at_expected_pct"),
        "power_cautious_pct": o.get("power_cautious_pct"),
        "chosen_by": by, "chosen_at": now()})
    plan.log(f"design chosen by {by}: {n_treatment} geos for {test_days} days")
    plan.save()
    return {"treatment_geos": treat, "mde_pct": o["mde_pct"]}


# ---- launch ----------------------------------------------------------------------------
def run_launch(plan: TestPlan, start: str, name: str | None = None) -> dict:
    """Launch agent: location lists and checklist for the chosen design."""
    _need(plan, "design_approved", "prepare the launch")
    if plan.at_least("locked"):
        raise PlanError("the launch is approved (G3) and locked")
    d = plan.section("design")
    hist_end = pd.Timestamp(plan.section("data")["history"][1])
    if pd.Timestamp(start) <= hist_end:
        raise PlanError(f"the test must start after the data used for design ends ({hist_end.date()})")
    res, _ = _load_geo(plan)
    x = export_targeting(res.data, d["treatment_geos"], d["control_geos"], start, d["test_days"],
                         plan.folder / "targeting", name or plan.d["test_id"],
                         plan.section("brief")["test_type"], d["cooldown_days"])
    s = pd.Timestamp(start)
    end = s + pd.Timedelta(days=d["test_days"] - 1)
    plan.update("launch", {
        "start": str(s.date()), "end": str(end.date()),
        "cooldown_end": str((end + pd.Timedelta(days=d["cooldown_days"])).date()),
        "action": x.action, "districts": len(x.test_districts),
        "files": {k: [_rel(plan, Path(p)) for p in v] for k, v in x.files.items()},
        "warnings": x.warnings})
    plan.save()
    return plan.section("launch")


# ---- during the test -----------------------------------------------------------------------
def run_monitor(plan: TestPlan, spend_file: str, column_map: dict | None = None) -> dict:
    """Monitoring agent: pacing and leakage from a spend report. Never reads results."""
    _need(plan, "locked", "monitor")
    d, launch = plan.section("design"), plan.section("launch")
    res = ingest(spend_file, column_map=column_map, spend_only=True)
    geo = map_geo(res.data, unit=plan.section("data").get("geo_unit", "postcode_area"))
    sp = SpendPlan(d["treatment_geos"], d["control_geos"], launch["start"], d["test_days"],
                   plan.section("brief")["test_type"], d["spend_multiplier"])
    pace, leak = pacing_report(geo, sp), leakage_check(geo, sp)
    md = monitoring_markdown(pace, leak, plan.d["test_id"])
    report = _write(plan, f"monitoring/{pace['as_of']}.md", md)
    entry = {"as_of": pace["as_of"], "day": pace["days_elapsed"], "pacing": pace["status"],
             "leakage": leak["status"],
             "flags": [f for f in pace["flags"] + leak["flags"]
                       if f not in ("spend is running to plan", "no leakage found")],
             "report": report}
    plan.d["monitoring"].append(entry)
    if plan.status == "locked":
        plan.set_status("live")
    plan.save()
    return entry


# ---- after the test ------------------------------------------------------------------------
def run_analysis(plan: TestPlan, file: str, column_map: dict | None = None) -> dict:
    """Analysis agent: lift, interval, placebo and robustness for the test as run."""
    _need(plan, "locked", "analyse")
    if plan.at_least("results_approved"):
        raise PlanError("results are approved (G4); a re-analysis needs a new plan")
    d, launch = plan.section("design"), plan.section("launch")
    res = ingest(file, column_map=column_map or plan.section("data").get("column_map") or None)
    geo = map_geo(res.data, unit=plan.section("data").get("geo_unit", "postcode_area"))
    if geo["date"].max() < pd.Timestamp(launch["end"]):
        raise PlanError(f"the data ends {geo['date'].max().date()}, before the test ends ({launch['end']})")
    cooldown = min(d["cooldown_days"], (geo["date"].max() - pd.Timestamp(launch["end"])).days)
    s = PowerSettings()
    lift = estimate_lift(geo, d["treatment_geos"], launch["start"], d["test_days"], cooldown,
                         d["control_geos"], s)
    placebo = placebo_test(geo, lift, settings=s)
    robust = robustness_suite(geo, lift, s)
    events = outside_events(geo, lift)
    md = lift_markdown(lift, placebo, robust, title=f"Test readout: {plan.d['test_id']}", events=events)
    chart = lift_chart(lift, plan.folder / "results_chart.png")
    report = _write(plan, "results.md", md + f"\n\n![Result]({chart.name})\n")
    _write(plan, "results.json", json.dumps({**lift.to_dict(), "placebo": placebo.to_dict(),
                                              "robustness": robust, "outside_events": events},
                                             indent=2, default=str))
    lift.daily.to_csv(plan.folder / "results_daily.csv", index=False)
    t = lift.test
    execution = sorted({f for m in plan.d["monitoring"] for f in m["flags"]})
    results = {
        "analysed_at": now(), "data_file": str(Path(file).resolve()), "data_version": res.data_version,
        "lift_pct": t["lift_pct"], "lift_ci_pct": t["lift_ci_pct"],
        "incremental_kpi": t["incremental_kpi"], "incremental_kpi_ci": t["incremental_kpi_ci"],
        "p_value": t["p_value"], "significant": t["significant"],
        "cooldown_days_used": cooldown,
        "test_plus_cooldown": ({k: lift.test_plus_cooldown[k] for k in ("lift_pct", "lift_ci_pct", "incremental_kpi")}
                               if lift.test_plus_cooldown else None),
        "spend": lift.spend, "placebo_passed": placebo.passed, "placebo_notes": placebo.notes,
        "robust": robust["consistent"], "robust_range_pct": robust["range_pct"],
        "outside_events": events,
        "execution_flags": execution, "report": report}
    plan.update("results", results)
    plan.set_status("analysed")
    plan.save()
    return results


def set_readout(plan: TestPlan, recommendation: str, by: str, retest_date: str | None = None,
                mmm_calibration: dict | None = None) -> None:
    """Readout agent (phase 2, step 3): record the recommendation and close the test."""
    if plan.status != "results_approved":
        raise PlanError("the readout follows approved results (G4)")
    plan.update("readout", {"recommendation": recommendation, "by": by, "at": now(),
                            "retest_date": retest_date, "mmm_calibration": mmm_calibration})
    plan.set_status("closed")
    plan.save()


# ---- what next -------------------------------------------------------------------------------
def next_step(plan: TestPlan) -> dict:
    """What should happen next, who does it, and anything blocking."""
    st, d = plan.status, plan.section("design")
    if st == "draft":
        miss = missing_brief(plan)
        if miss:
            return {"who": "agent", "agent": "brief", "step": "set_brief",
                    "action": f"complete the brief: {', '.join(miss)}"}
        return {"who": "you", "step": "approve G1", "action": "review and approve the brief (G1)"}
    if st == "brief_approved":
        if plan.section("data").get("quality_status") == "not ready":
            return {"who": "you", "step": "run_data",
                    "action": "the data is not ready (see quality_report.md): fix it and load it again"}
        return {"who": "agent", "agent": "data", "step": "run_data",
                "action": "load the KPI-by-postcode file and run the quality checks"}
    if st == "data_ready":
        if not d.get("options"):
            return {"who": "agent", "agent": "design", "step": "run_design",
                    "action": "search test/control splits and rank them"}
        if not d.get("treatment_geos"):
            return {"who": "you", "step": "choose_design",
                    "action": "choose an option from design_shortlist.md (or accept the recommended one)"}
        return {"who": "you", "step": "approve G2", "action": "review and approve the design (G2)"}
    if st == "design_approved":
        if not plan.section("launch").get("files"):
            return {"who": "agent", "agent": "launch", "step": "run_launch",
                    "action": "pick a start date and write the location lists and checklist"}
        return {"who": "you", "step": "approve G3",
                "action": "check the lists and checklist, then approve the launch (G3); this locks the plan"}
    if st in ("locked", "live"):
        end = pd.Timestamp(plan.section("launch")["cooldown_end"])
        return {"who": "agent", "agent": "monitoring/analysis", "step": "run_monitor or run_analysis",
                "action": f"monitor spend weekly; analyse once the data runs to {end.date()} "
                          "(the test end at the earliest)"}
    if st == "analysed":
        return {"who": "you", "step": "approve G4", "action": "review results.md and approve the results (G4)"}
    if st == "results_approved":
        return {"who": "agent", "agent": "readout", "step": "set_readout",
                "action": "write the recommendation and close the test"}
    return {"who": "nobody", "step": "done", "action": "the test is closed"}


def summary(plan: TestPlan) -> str:
    n = next_step(plan)
    gates = ", ".join(f"{g} {'done' if v else 'open'}" for g, v in plan.d["gates"].items())
    lines = [f"{plan.d['test_id']}: {plan.status} ({gates})",
             f"Next ({n['who']}{' / ' + n['agent'] + ' agent' if n.get('agent') else ''}): {n['action']}"]
    if plan.d["deviations"]:
        lines.append(f"Deviations logged: {len(plan.d['deviations'])}")
    return "\n".join(lines)
