"""export_targeting: the location lists and checklist needed to launch a geo test.

Both Google Ads and Meta target UK postcodes at outward-code (district) level, e.g. "LS1"
or "RM17", not at area level ("LS"). So a test area is applied as the list of its
districts:
- holdout: the test areas' districts are added as EXCLUDED locations to every campaign
  in the channel being tested, for the test dates only.
- heavy-up: the test areas' districts are the TARGETED locations of a separate campaign
  carrying the extra budget; business-as-usual campaigns stay unchanged.

Google Ads takes up to 1,000 locations per bulk paste, so lists are split into parts of
1,000. Districts come from your own data: a district with no customers in your history
is not in it, so check each list against a full postcode district list before launch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .geography import AREA_TO_REGION

GOOGLE_BULK_LIMIT = 1000


@dataclass
class TargetingExport:
    files: dict[str, list[str]]
    test_districts: list[str]
    control_districts: int
    action: str
    warnings: list[str] = field(default_factory=list)


def _districts(postcode_data: pd.DataFrame, areas: list[str]) -> list[str]:
    d = postcode_data.loc[postcode_data["area"].isin(areas), ["postcode", "area"]].drop_duplicates()
    if (d["postcode"] == d["area"]).any():
        raise ValueError("these test areas only have area-level postcodes in your data; the ad platforms "
                         "need districts (e.g. LS1), so supply district-level data or a district list")
    def key(pc: str):
        m = re.match(r"^([A-Z]{1,2})(\d+)(.*)$", pc)
        return (m.group(1), int(m.group(2)), m.group(3)) if m else (pc, 0, "")
    return sorted(d["postcode"].unique(), key=key)


def export_targeting(postcode_data: pd.DataFrame, treatment: list[str], controls: list[str],
                     start: str, test_days: int, out_dir: str | Path, name: str = "test",
                     kind: str = "holdout", cooldown_days: int = 0) -> TargetingExport:
    """Write the location lists, a geo assignment table and a launch checklist into out_dir.

    postcode_data: ingest output (district-level postcodes). treatment/controls: postcode areas.
    """
    if kind not in ("holdout", "heavy_up"):
        raise ValueError("kind must be 'holdout' or 'heavy_up'")
    overlap = set(treatment) & set(controls)
    if overlap:
        raise ValueError(f"in both test and control: {sorted(overlap)}")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    test_d = _districts(postcode_data, treatment)
    ctrl_d = _districts(postcode_data, controls)
    action = "exclude" if kind == "holdout" else "include"
    files: dict[str, list[str]] = {"google_ads": [], "meta": []}

    parts = [test_d[i:i + GOOGLE_BULK_LIMIT] for i in range(0, len(test_d), GOOGLE_BULK_LIMIT)]
    for n, part in enumerate(parts, 1):
        suffix = f"_part{n}" if len(parts) > 1 else ""
        p = out / f"{name}_google_ads_{action}{suffix}.txt"
        p.write_text("\n".join(part) + "\n")
        files["google_ads"].append(str(p))
    p = out / f"{name}_meta_{action}.txt"
    p.write_text("\n".join(test_d) + "\n")
    files["meta"].append(str(p))

    kpi = postcode_data.groupby("area")["kpi"].sum()
    rows = []
    for area in sorted(postcode_data["area"].unique()):
        group = "test" if area in treatment else "control" if area in controls else "left out"
        n = postcode_data.loc[postcode_data["area"] == area, "postcode"].nunique()
        rows.append({"area": area, "region": AREA_TO_REGION.get(area, ""), "group": group,
                     "districts": n, "kpi_share_pct": round(100 * kpi.get(area, 0) / kpi.sum(), 2)})
    assign = pd.DataFrame(rows)
    p = out / f"{name}_geo_assignment.csv"
    assign.to_csv(p, index=False)
    files["assignment"] = [str(p)]

    s = pd.Timestamp(start)
    end = s + pd.Timedelta(days=test_days - 1)
    cool = end + pd.Timedelta(days=cooldown_days) if cooldown_days else None
    left_out = assign.loc[assign["group"] == "left out", "area"].tolist()
    spend_share = None
    if "spend" in postcode_data and postcode_data["spend"].notna().any():
        recent = postcode_data[postcode_data["date"] > postcode_data["date"].max() - pd.Timedelta(days=28)]
        by_area = recent.groupby("area")["spend"].sum()
        spend_share = float(by_area[by_area.index.isin(treatment)].sum() / by_area.sum())
    md = _checklist(name, kind, treatment, len(test_d), len(controls), s, end, cool, files, left_out,
                    spend_share)
    p = out / f"{name}_launch_checklist.md"
    p.write_text(md)
    files["checklist"] = [str(p)]

    warnings = ["Lists hold only districts that appear in your data. Check them against a full UK "
                "postcode district list: any missing district in a test area would keep receiving ads."]
    if len(parts) > 1:
        warnings.append(f"Google Ads list split into {len(parts)} parts of up to {GOOGLE_BULK_LIMIT}.")
    return TargetingExport(files, test_d, len(ctrl_d), action, warnings)


def _checklist(name, kind, treatment, n_districts, n_controls, start, end, cool, files, left_out,
               spend_share) -> str:
    d = lambda t: t.strftime("%a %d %b %Y")
    holdout = kind == "holdout"
    g = ", ".join(Path(f).name for f in files["google_ads"])
    m = Path(files["meta"][0]).name
    lines = [f"# Launch checklist: {name}", "",
             f"**{'Holdout' if holdout else 'Heavy-up'}** in {len(treatment)} postcode areas "
             f"({n_districts} districts): {', '.join(treatment)}. {n_controls} control areas"
             + (f"; left out: {', '.join(left_out)}" if left_out else "") + ".",
             f"Test: {d(start)} to {d(end)}" + (f"; cooldown to {d(cool)}" if cool else "") + ".", "",
             "## Before launch", "",
             "- [ ] Check the district lists against a full UK postcode district list; add any missing districts in the test areas.",
             f"- [ ] Google Ads: paste {g} into the bulk location tool with the country set to the UK, then fix anything it reports as not found.",
             f"- [ ] Meta: paste {m} into Locations > Add locations in bulk > postal codes, then fix anything not recognised.",
             "- [ ] Google Ads: set the location option to *Presence* (people in or regularly in the location), for targets and exclusions.",
             "- [ ] Freeze other changes in test and control areas for the test dates: pricing, promotions, other channels' geo splits.",
             "- [ ] Agree that no one moves budget between campaigns to make up for the paused areas.",
             "- [ ] Get sign-off on the locked test plan (gate G3).", "",
             f"## On {d(start)}", ""]
    if holdout:
        share = (f" (about {spend_share:.0%} of the channel's spend over the last 28 days of your data)"
                 if spend_share is not None else "")
        lines += ["- [ ] Add the district lists as **excluded** locations to every campaign in the channel being tested.",
                  f"- [ ] Cut each campaign's budget by the test areas' usual share of its spend{share}. Left "
                  "unchanged, the platform will spend the paused budget in the control areas, which inflates "
                  "the controls and makes the channel look more valuable than it is."]
    else:
        lines += ["- [ ] Launch the heavy-up campaign **targeting only** the district lists, with the extra budget.",
                  "- [ ] Leave business-as-usual campaigns unchanged."]
    lines += ["- [ ] Next day: check the platform's location report shows no (holdout) or extra (heavy-up) spend "
              "in the test districts as planned.", "",
              "## During the test", "",
              "- [ ] Weekly: run the monitoring checks (`pacing_report`, `leakage_check`). They read spend only, never results.",
              "- [ ] Log any outside event (regional promo, outage, competitor push) as a deviation.", "",
              f"## On {d(end + pd.Timedelta(days=1))}", "",
              f"- [ ] {'Remove the exclusions' if holdout else 'Stop the heavy-up campaign'} so the test areas return to normal."]
    if cool:
        lines += [f"- [ ] Wait for the cooldown to end ({d(cool)}) before the final readout."]
    return "\n".join(lines) + "\n"
