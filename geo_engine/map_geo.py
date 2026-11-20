"""Aggregate postcode-level data up to the geo unit used for testing."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .geography import AREA_TO_REGION

UNITS = ("postcode_district", "postcode_area", "region", "custom")


def map_geo(data: pd.DataFrame, unit: str = "postcode_area",
            lookup: dict[str, str] | pd.DataFrame | str | Path | None = None) -> pd.DataFrame:
    """Aggregate standard-format data (from ingest) to one row per geo per date.

    unit:   "postcode_district", "postcode_area" (default), "region", or "custom".
    lookup: for "custom" only: postcode district or area -> geo name, as a dict, a
            two-column DataFrame/CSV (key, geo). District keys win over area keys,
            so you can map most of an area one way and split out a few districts.
    Returns columns: date, geo, kpi, spend.
    """
    if unit not in UNITS:
        raise ValueError(f"unit must be one of {UNITS}")
    df = data
    if unit == "postcode_district":
        area_only = df["postcode"] == df["area"]
        if area_only.any():
            raise ValueError(f"{df.loc[area_only, 'postcode'].nunique()} postcodes are area-level only; "
                             "use unit='postcode_area' or fix the file")
        geo = df["postcode"]
    elif unit == "postcode_area":
        geo = df["area"]
    elif unit == "region":
        geo = df["area"].map(AREA_TO_REGION)
    else:
        mapping = _load_lookup(lookup)
        geo = df["postcode"].map(mapping).fillna(df["area"].map(mapping))
        unmapped = geo.isna()
        if unmapped.any():
            missing = sorted(df.loc[unmapped, "postcode"].unique())
            raise ValueError(f"{len(missing)} postcodes have no entry in the lookup, e.g. {missing[:10]}")

    out = (df.assign(geo=geo.to_numpy())
             .groupby(["date", "geo"], as_index=False)[["kpi", "spend"]].sum(min_count=1)
             .sort_values(["date", "geo"], ignore_index=True))
    out.attrs["unit"] = unit
    return out


def geo_summary(geo_data: pd.DataFrame) -> pd.DataFrame:
    """Per geo: mean KPI per period and share of the national total, largest first."""
    g = geo_data.groupby("geo")["kpi"].agg(mean_kpi="mean", total_kpi="sum")
    g["share_pct"] = 100 * g["total_kpi"] / g["total_kpi"].sum()
    return g.sort_values("total_kpi", ascending=False)


def _load_lookup(lookup) -> dict[str, str]:
    if lookup is None:
        raise ValueError("unit='custom' needs a lookup")
    if isinstance(lookup, dict):
        return {str(k).upper().replace(" ", ""): v for k, v in lookup.items()}
    if isinstance(lookup, (str, Path)):
        lookup = pd.read_csv(lookup, dtype=str)
    if lookup.shape[1] < 2:
        raise ValueError("lookup needs two columns: postcode district or area, then geo name")
    keys = lookup.iloc[:, 0].str.upper().str.replace(" ", "", regex=False)
    return dict(zip(keys, lookup.iloc[:, 1]))
