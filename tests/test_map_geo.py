import pandas as pd
import pytest

from geo_engine.map_geo import geo_summary, map_geo


def test_area_totals_equal_district_totals(ingested42, areas42):
    d = ingested42.data
    assert areas42["geo"].nunique() == 121
    assert len(areas42) == 121 * d["date"].nunique()
    by_area = d.groupby(["date", "area"])["kpi"].sum().rename_axis(["date", "geo"])
    pd.testing.assert_series_equal(areas42.set_index(["date", "geo"])["kpi"], by_area, check_names=False)


def test_region_level(ingested42):
    r = map_geo(ingested42.data, unit="region")
    assert r["geo"].nunique() == 12
    assert r["kpi"].sum() == ingested42.data["kpi"].sum()


def test_custom_lookup_district_beats_area(ingested42):
    d = ingested42.data
    lookup = {a: "Rest of UK" for a in d["area"].unique()}
    lookup.update({"M": "Manchester", "M1": "Manchester city centre"})
    c = map_geo(d, unit="custom", lookup=lookup)
    totals = c.groupby("geo")["kpi"].sum()
    assert totals["Manchester city centre"] == d.loc[d["postcode"] == "M1", "kpi"].sum()
    assert totals["Manchester"] == d.loc[(d["area"] == "M") & (d["postcode"] != "M1"), "kpi"].sum()
    assert totals.sum() == d["kpi"].sum()


def test_custom_lookup_gaps_rejected(ingested42):
    with pytest.raises(ValueError, match="no entry in the lookup"):
        map_geo(ingested42.data, unit="custom", lookup={"M": "Manchester"})


def test_geo_summary_shares(areas42):
    s = geo_summary(areas42)
    assert s["share_pct"].sum() == pytest.approx(100)
    assert s.index[-1] in {"ZE", "LD", "KW", "WC", "EC"}  # a tiny area comes last
