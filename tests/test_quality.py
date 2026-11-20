import pandas as pd
import pytest

from geo_engine.geography import AREA_TO_REGION
from geo_engine.ingest import ingest
from geo_engine.map_geo import map_geo
from geo_engine.quality import quality_report


@pytest.fixture(scope="module")
def report42(areas42, ingested42):
    return quality_report(areas42, ingested42.missing_dates)


def _overlaps(events, where, start, end):
    e = events[events["where"] == where]
    return bool(((e["start"] <= end) & (e["end"] >= start)).any())


def test_clean_synthetic_history_is_usable(report42):
    assert report42.status == "ready with flags"
    results = {c["check"]: c["result"] for c in report42.checks}
    for name in ("History length", "Dates with no data", "National drops", "Zero runs",
                 "Level shifts", "Dominant geos", "Spend coverage"):
        assert results[name] == "pass", name
    low = set(report42.geo_flags.loc[report42.geo_flags["recommendation"] == "avoid as test geo", "geo"])
    assert {"ZE", "KW", "LD"} <= low


def test_finds_most_regional_promos_with_few_false_alarms(report42, events42):
    promos = events42[events42["type"] == "regional_promo"]
    regional = report42.events[report42.events["scope"] == "region"]
    recall = promos.apply(lambda t: _overlaps(regional, t["where"], t["start"], t["end"]), axis=1).mean()
    precision = regional.apply(lambda e: _overlaps(promos, e["where"], e["start"], e["end"]), axis=1).mean()
    assert recall >= 0.6 and precision >= 0.85


def test_single_geo_alerts_are_mostly_real(report42, events42):
    local = report42.events[report42.events["scope"] == "geo"]

    def real_any(e):  # matches an outage in that geo, or a promo in its region
        t = events42[events42["where"].isin([e["where"], AREA_TO_REGION[e["where"]]])]
        return bool(((t["start"] <= e["end"]) & (t["end"] >= e["start"])).any())
    assert len(local) > 0
    assert local.apply(real_any, axis=1).mean() >= 0.65


def _corrupt(history: pd.DataFrame) -> pd.DataFrame:
    h = history.copy()
    dates = pd.to_datetime(h["date"])
    area = h["postcode"].str.extract(r"^([A-Z]+)", expand=False)
    gap = (area == "LS") & dates.between("2026-03-02", "2026-03-07")       # 6-day tracking gap
    h.loc[gap, "kpi"] = 0
    shift = (area == "NG") & (dates >= "2026-05-01")                          # NG share doubles
    h.loc[shift, "kpi"] = h.loc[shift, "kpi"] * 2
    lost = dates.isin(pd.to_datetime(["2025-06-10", "2025-06-11"]))           # export gap
    return h[~lost]


def test_corrupted_data_is_caught(history42):
    r = ingest("corrupt.csv", raw=_corrupt(history42).astype(str))
    q = quality_report(map_geo(r.data), r.missing_dates)
    res = {c["check"]: c["result"] for c in q.checks}
    assert res["Dates with no data"] == "warn" and r.missing_dates == ["2025-06-10", "2025-06-11"]
    assert res["Zero runs"] == "warn" and res["Level shifts"] == "warn"
    flags = q.geo_flags.set_index("geo")
    assert flags.loc["LS", "recommendation"] == "review" and "zero run of 6" in flags.loc["LS", "flags"]
    assert flags.loc["NG", "recommendation"] == "review" and "share up" in flags.loc["NG", "flags"]
    assert q.status == "ready with flags"


def test_short_history_not_ready(areas42):
    short = areas42[areas42["date"] >= areas42["date"].max() - pd.Timedelta(days=149)]
    short.attrs["unit"] = "postcode_area"
    q = quality_report(short)
    assert q.status == "not ready"
    assert q.checks[0]["result"] == "fail"


def test_weekly_data_runs(ingested42):
    d = ingested42.data.copy()
    d["date"] = d["date"] - pd.to_timedelta(d["date"].dt.dayofweek, unit="D")  # week starting Monday
    weekly = d.groupby(["date", "postcode", "area"], as_index=False)[["kpi", "spend"]].sum()
    r = ingest("weekly.csv", raw=weekly.astype(str))
    assert r.frequency == "weekly"
    q = quality_report(map_geo(r.data))
    assert q.meta["frequency"] == "weekly" and q.status in ("ready", "ready with flags")
    low = q.geo_flags.set_index("geo").loc["ZE", "flags"]
    assert "low volume" in low  # per-day volume threshold applies to weekly data too
