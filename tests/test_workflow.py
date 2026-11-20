import pandas as pd
import pytest
import yaml

from geo_engine import workflow as wf
from geo_engine.plan import LockedError, PlanError, TestPlan
from geo_engine.synthetic import GeoTestSpec

START = "2027-02-15"   # January 2027 in this world is full of regional events (see test below)
BRIEF = dict(question="Does Meta prospecting drive incremental new customers?", channel="meta_prospecting",
             test_type="holdout", kpi="new_customers", kpi_source="orders database",
             success_metric="cost_per_incremental",
             decision_rule="Keep spend if cost per incremental customer is under £90", expected_lift=-0.08)


@pytest.fixture(scope="module")
def files(tmp_path_factory, history42):
    d = tmp_path_factory.mktemp("data")
    history42.to_csv(d / "history.csv", index=False)
    return d


@pytest.fixture(scope="module")
def run(files, world42, tmp_path_factory):
    """One plan taken all the way through, keeping a snapshot at each stage."""
    folder = tmp_path_factory.mktemp("geotests")
    plan = TestPlan.new("jan_holdout", folder)
    snaps = {}
    wf.set_brief(plan, **BRIEF)
    snaps["brief_done"] = wf.next_step(plan)
    wf.approve(plan, "G1", "Anand")
    wf.run_data(plan, str(files / "history.csv"))
    wf.run_design(plan, test_days=(21,), n_treatment=(10,), candidates=8,
                  exclude_regions_from_test=("London",))
    snaps["designed"] = wf.next_step(plan)
    wf.choose_design(plan, by="Anand")
    wf.approve(plan, "G2", "Anand")
    wf.run_launch(plan, START)
    wf.approve(plan, "G3", "Anand")
    snaps["locked"] = dict(plan.d)

    geos = plan.section("design")["treatment_geos"]
    observed, truth, _ = world42.run_test(GeoTestSpec(geos, START, test_days=21, cooldown_days=14))
    observed.to_csv(files / "full.csv", index=False)
    observed[observed["date"] <= "2027-02-28"][["date", "postcode", "spend"]].to_csv(
        files / "spend.csv", index=False)
    wf.run_monitor(plan, str(files / "spend.csv"))
    wf.run_analysis(plan, str(files / "full.csv"))
    return plan, truth, snaps, files


def test_steps_happen_in_order_and_say_what_is_next(run):
    plan, _, snaps, _ = run
    assert snaps["brief_done"]["step"] == "approve G1" and snaps["brief_done"]["who"] == "you"
    assert snaps["designed"]["step"] == "choose_design"
    assert plan.status == "analysed" and wf.next_step(plan)["step"] == "approve G4"
    assert all(plan.d["gates"][g] for g in ("G1", "G2", "G3")) and plan.d["gates"]["G4"] is None


def test_plan_file_is_the_record(run):
    plan, _, _, _ = run
    on_disk = yaml.safe_load(plan.path.read_text())
    assert on_disk["design"]["treatment_geos"] == plan.section("design")["treatment_geos"]
    assert len(on_disk["design"]["treatment_geos"]) == 10
    assert on_disk["launch"]["start"] == START and on_disk["launch"]["end"] == "2027-03-07"
    assert on_disk["monitoring"][0]["pacing"] == "on track" and on_disk["monitoring"][0]["day"] == 14
    for f in ("quality_report.md", "design_shortlist.md", "results.md", "results_chart.png",
              on_disk["launch"]["files"]["checklist"][0], on_disk["monitoring"][0]["report"]):
        assert (plan.folder / f).exists(), f


def test_result_matches_the_truth(run):
    plan, truth, _, _ = run
    r = plan.section("results")
    lo, hi = r["lift_ci_pct"]
    assert lo <= truth["test_period"]["lift_pct_expected"] <= hi and r["significant"]
    assert r["execution_flags"] == [] and r["cooldown_days_used"] == 14


def test_locked_sections_cannot_change_silently(run):
    plan, _, _, _ = run
    with pytest.raises(LockedError):
        plan.update("design", {"test_days": 28})
    plan.d["design"]["test_days"] = 28                 # an edit behind the plan's back
    with pytest.raises(LockedError):
        plan.save()
    plan.d["design"]["test_days"] = 21
    plan.save()
    text = plan.path.read_text().replace("cooldown_days: 14", "cooldown_days: 7")
    plan.path.write_text(text)                         # someone edits the file by hand
    with pytest.raises(LockedError):
        TestPlan.load(plan.path)
    plan.path.write_text(text.replace("cooldown_days: 7", "cooldown_days: 14"))
    plan = TestPlan.load(plan.path)
    plan.record_deviation("design", "cooldown_days", 21, "client asked for a longer cooldown", "Anand")
    plan.save()
    again = TestPlan.load(plan.path)
    dev = again.d["deviations"][-1]
    assert dev["from"] == 14 and dev["to"] == 21 and dev["reason"].startswith("client")


def test_gates_and_readout_close_the_test(run):
    plan, _, _, _ = run
    plan = TestPlan.load(plan.path)
    with pytest.raises(PlanError, match="already approved"):
        wf.approve(plan, "G3", "Anand")
    wf.approve(plan, "G4", "Anand")
    with pytest.raises(PlanError, match="decision must be"):
        wf.write_readout(plan, "maybe", "Keep spend", "Anand")
    report = wf.write_readout(plan, "keep", "Keep spend: cost per incremental customer is under target.", "Anand")
    assert plan.status == "closed" and wf.next_step(plan)["step"] == "done"
    md = (plan.folder / report).read_text()
    assert "Decision: KEEP" in md and "For the MMM" in md and "Retest by 2027-09-05" in md
    mmm = plan.section("readout")["mmm_calibration"]
    lo, hi = mmm["interval"]
    assert lo <= mmm["incremental_kpi_per_1000_gbp"] <= hi and mmm["incremental_kpi_per_1000_gbp"] > 0
    assert wf.list_tests(plan.folder.parent)[0]["status"] == "closed"


def test_out_of_order_steps_are_refused(files, tmp_path):
    plan = TestPlan.new("t2", tmp_path)
    with pytest.raises(PlanError, match="needs 'brief_approved'"):
        wf.run_data(plan, str(files / "history.csv"))
    with pytest.raises(PlanError, match="missing"):
        wf.approve(plan, "G1", "Anand")
    wf.set_brief(plan, **BRIEF)
    with pytest.raises(PlanError, match="G2 approves a chosen design"):
        wf.approve(plan, "G2", "Anand")
    wf.approve(plan, "G1", "Anand")
    with pytest.raises(PlanError, match="approved"):
        wf.set_brief(plan, kpi="orders")
    wf.reopen_brief(plan, "Anand", "KPI should be orders")
    assert plan.status == "draft" and wf.set_brief(plan, kpi="orders") == []
    with pytest.raises(PlanError):
        wf.set_brief(plan, test_type="ab_test")
    with pytest.raises(PlanError, match="fraction"):
        wf.set_brief(plan, expected_lift=-8)


def test_launch_must_start_after_the_design_data(run, tmp_path, files):
    plan = TestPlan.new("t3", tmp_path)
    wf.set_brief(plan, **BRIEF)
    wf.approve(plan, "G1", "Anand")
    wf.run_data(plan, str(files / "history.csv"))
    wf.run_design(plan, test_days=(21,), n_treatment=(5,), candidates=4)
    wf.choose_design(plan, 21, 5)
    wf.approve(plan, "G2", "Anand")
    with pytest.raises(PlanError, match="must start after"):
        wf.run_launch(plan, "2026-06-01")
    with pytest.raises(PlanError, match="duplicate|already exists"):
        TestPlan.new("t3", tmp_path)


def test_outside_events_found_during_the_test(world42):
    """January 2027 in this world holds regional promotions in East of England and
    Yorkshire (see world_events); outside_events should list them from the controls alone."""
    from geo_engine.analysis import estimate_lift, outside_events
    from geo_engine.ingest import ingest
    from geo_engine.map_geo import map_geo
    geos = ["B", "BL", "CB", "HU", "HX", "KY", "LA", "LU", "NE", "SP"]
    observed, _, _ = world42.run_test(GeoTestSpec(geos, "2027-01-11", test_days=21, cooldown_days=14))
    g = map_geo(ingest("x.csv", raw=observed.astype(str)).data)
    controls = [x for x in g["geo"].unique() if x not in geos and x != "DY"]
    r = estimate_lift(g, geos, "2027-01-11", 21, cooldown_days=14, controls=controls)
    events = outside_events(g, r)
    found = {e["region"]: e for e in events}
    assert {"East of England", "Yorkshire and the Humber"} <= set(found)
    assert found["East of England"]["test_geos_in_region"] == ["CB", "LU"]
    assert all(e["direction"] == "spike" for e in events if e["region"] in ("East of England",
                                                                          "Yorkshire and the Humber"))
