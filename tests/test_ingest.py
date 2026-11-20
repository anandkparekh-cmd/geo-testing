import pandas as pd
import pytest

from geo_engine.geography import parse_postcode
from geo_engine.ingest import IngestError, ingest


@pytest.mark.parametrize("raw, level, area, district", [
    ("SW1A 1AA", "full", "SW", "SW1A"),
    ("sw1a1aa", "full", "SW", "SW1A"),
    ("  M1 1AE ", "full", "M", "M1"),
    ("EC1A 1BB", "full", "EC", "EC1A"),
    ("B33 8TH", "full", "B", "B33"),
    ("SW1A 1", "sector", "SW", "SW1A"),
    ("LS1", "district", "LS", "LS1"),
    ("M", "area", "M", None),
    ("bt", "area", "BT", None),
])
def test_parse_valid_postcodes(raw, level, area, district):
    p = parse_postcode(raw)
    assert (p.level, p.area, p.district) == (level, area, district)


@pytest.mark.parametrize("raw, reason", [
    ("JE2 3AB", "Channel Islands"),
    ("GY1", "Channel Islands"),
    ("XX1 1AA", "unknown postcode area"),
    ("12345", "not a UK postcode"),
    ("", "not a UK postcode"),
    (None, "not a UK postcode"),
])
def test_parse_invalid_postcodes(raw, reason):
    p = parse_postcode(raw)
    assert p.level == "invalid" and reason in p.reason


def _messy() -> pd.DataFrame:
    """A realistic export: UK dates, full postcodes, currency, and assorted problems."""
    rows = []
    for d in pd.date_range("2025-01-01", "2025-01-10"):
        if d == pd.Timestamp("2025-01-05"):
            continue  # a whole date missing from the export
        for pc, n in [("M1 1AE", 5), ("m1 2ab", 3), ("LS1 4DY", 7), ("B33 8TH", 4)]:
            if d == pd.Timestamp("2025-01-03") and pc == "B33 8TH":
                continue  # one postcode missing on one date
            rows.append({"Order Date": d.strftime("%d/%m/%Y"), "Billing Postcode": pc,
                         "New Customers": str(n), "Cost (£)": "£1,234.50"})
    rows.append({"Order Date": "02/01/2025", "Billing Postcode": "LS1 4DY",
                 "New Customers": "2", "Cost (£)": "£10.00"})          # duplicate date + postcode
    rows.append({"Order Date": "03/01/2025", "Billing Postcode": "JE2 3AB",
                 "New Customers": "9", "Cost (£)": "£5"})              # Jersey: dropped
    rows.append({"Order Date": "03/01/2025", "Billing Postcode": "not known",
                 "New Customers": "1", "Cost (£)": ""})                # bad postcode: dropped
    rows.append({"Order Date": "31/02/2025", "Billing Postcode": "M1 1AE",
                 "New Customers": "1", "Cost (£)": "£1"})              # impossible date: dropped
    rows.append({"Order Date": "04/01/2025", "Billing Postcode": "M1 1AE",
                 "New Customers": "n/a", "Cost (£)": "£1"})            # non-numeric KPI: dropped
    return pd.DataFrame(rows)


def test_messy_file_needs_postcode_column_named():
    with pytest.raises(IngestError, match="postcode"):
        ingest("export.csv", raw=_messy())


def test_messy_file_cleaned_and_every_fix_logged():
    r = ingest("export.csv", column_map={"postcode": "Billing Postcode"}, raw=_messy())
    d = r.data
    assert r.column_map == {"postcode": "Billing Postcode", "date": "Order Date",
                            "kpi": "New Customers", "spend": "Cost (£)"}
    assert r.frequency == "daily" and r.postcode_level == "district"
    assert d["date"].min() == pd.Timestamp("2025-01-01") and d["date"].max() == pd.Timestamp("2025-01-10")
    assert sorted(d["postcode"].unique()) == ["B33", "LS1", "M1"]   # M1 1AE and M1 2AB collapse into M1
    assert len(d) == 10 * 3                                          # full grid, missing date included
    assert r.missing_dates == ["2025-01-05"]
    # KPI: 9 dates x (5+3+7+4) - 4 (B33 missing on 3 Jan) + 2 (duplicate) = 169
    assert d["kpi"].sum() == 169
    assert d.loc[(d["date"] == "2025-01-03") & (d["postcode"] == "B33"), "kpi"].item() == 0
    assert d.loc[d["date"] == "2025-01-01", "spend"].sum() == pytest.approx(4 * 1234.50)
    checks = {i["check"] for i in r.issues}
    assert {"date format", "unreadable dates", "non-numeric KPI", "unusable postcodes",
            "duplicate rows", "dates with no data", "postcode detail", "zero-filled"} <= checks


def test_weekly_and_area_level_file():
    rows = [{"week": str(d.date()), "postcode_area": a, "orders": 10}
            for d in pd.date_range("2025-01-06", periods=8, freq="7D") for a in ["M", "LS"]]
    r = ingest("weekly.csv", raw=pd.DataFrame(rows))
    assert r.frequency == "weekly" and r.postcode_level == "area"
    assert set(r.data["postcode"]) == {"M", "LS"} and r.data["spend"].isna().all()


def test_ambiguous_kpi_column_must_be_chosen():
    df = pd.DataFrame({"date": ["2025-01-01", "2025-01-02", "2025-01-03"], "postcode": ["M1"] * 3,
                       "orders": ["1", "2", "3"], "revenue": ["10", "20", "30"]})
    with pytest.raises(IngestError, match="orders.*revenue|revenue.*orders"):
        ingest("x.csv", raw=df)
    assert ingest("x.csv", column_map={"kpi": "revenue"}, raw=df).data["kpi"].sum() == 60


def test_irregular_dates_rejected():
    df = pd.DataFrame({"date": ["2025-01-01", "2025-01-04", "2025-01-07", "2025-01-10"],
                       "postcode": ["M1"] * 4, "kpi": ["1"] * 4})
    with pytest.raises(IngestError, match="daily or weekly"):
        ingest("x.csv", raw=df)


def test_synthetic_history_round_trip(history42, ingested42):
    d = ingested42.data
    assert d["kpi"].sum() == history42["kpi"].sum()
    assert d["spend"].sum() == pytest.approx(history42["spend"].sum())
    assert d["postcode"].nunique() == history42["postcode"].nunique() and d["area"].nunique() == 121
    assert [i["check"] for i in ingested42.issues] == ["rows read"]  # clean file: nothing to fix
    again = ingest("synthetic_history.csv", raw=history42.astype(str))
    assert again.data_version == ingested42.data_version
