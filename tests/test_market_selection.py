import pytest

from geo_engine.geography import AREA_TO_REGION
from geo_engine.market_selection import DesignConstraints, select_markets
from geo_engine.power import Panel
from geo_engine.quality import quality_report

SMALL = dict(test_days=(21, 28), n_treatment=(5, 10), candidates_per_size=8)


@pytest.fixture(scope="module")
def panel(areas42):
    return Panel(areas42)


@pytest.fixture(scope="module")
def flags(areas42):
    return quality_report(areas42).geo_flags


@pytest.fixture(scope="module")
def shortlist(panel, flags):
    c = DesignConstraints(**SMALL, exclude_regions_from_test=("London",), exclude=("M",),
                          expected_lift=-0.08, seed=1)
    return select_markets(panel, flags, c)


def _groups(sl):
    return [set(t.split(", ")) for t in sl.all_candidates["treatment"]]


def test_one_best_option_per_length_and_size(shortlist):
    o = shortlist.options
    assert len(o) == 4 and set(zip(o["test_days"], o["n_treatment"])) == {(21, 5), (21, 10), (28, 5), (28, 10)}


def test_exclusions_respected(shortlist, flags):
    avoid = set(flags.loc[flags["recommendation"] == "avoid as test geo", "geo"])
    review = set(flags.loc[flags["recommendation"] == "review", "geo"])
    for g in _groups(shortlist):
        assert not any(AREA_TO_REGION[x] == "London" for x in g)
        assert not (g & avoid) and not (g & review) and "M" not in g
    assert "M" in shortlist.eligibility["left_out"]
    assert shortlist.eligibility["control_pool"] == 121 - len(review | {"M"})


def test_kpi_share_limits(shortlist):
    s = shortlist.all_candidates["kpi_share_pct"]
    assert s.min() >= 2 and s.max() <= 30


def test_recommendation_meets_cautious_power(shortlist):
    r = shortlist.recommended
    assert r is not None and r["power_cautious_pct"] >= 80
    assert "Recommended" in shortlist.markdown and r["treatment"] in shortlist.markdown


def test_same_seed_same_shortlist(panel, flags, shortlist):
    c = DesignConstraints(**SMALL, exclude_regions_from_test=("London",), exclude=("M",),
                          expected_lift=-0.08, seed=1)
    again = select_markets(panel, flags, c)
    assert again.options["treatment"].tolist() == shortlist.options["treatment"].tolist()


def test_no_recommendation_for_an_undetectable_effect(panel, flags):
    c = DesignConstraints(test_days=(21,), n_treatment=(5,), candidates_per_size=4, expected_lift=-0.005)
    sl = select_markets(panel, flags, c)
    assert sl.recommended is None and "No option reaches" in sl.markdown


def test_impossible_share_limits_rejected(panel, flags):
    with pytest.raises(ValueError, match="KPI share"):
        select_markets(panel, flags, DesignConstraints(test_days=(21,), n_treatment=(5,),
                                                       candidates_per_size=4, max_kpi_share=0.001))
