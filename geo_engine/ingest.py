"""Load a KPI-by-postcode file into the standard format.

Standard format, one row per postcode district (or area) per date:
    date (datetime), postcode (district outward code, or area code if that is all
    the file gives), area, kpi, spend (NaN when the file has no spend column)

Every problem found is logged in `issues` rather than silently fixed, so the
Data agent can show you exactly what was changed and why.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .geography import parse_postcode

STANDARD = ("date", "postcode", "kpi", "spend")
REQUIRED = ("date", "postcode", "kpi")
ALIASES = {
    "date": ["date", "day", "week", "week_start", "week_commencing", "period", "order_date", "dt"],
    "postcode": ["postcode", "post_code", "postal_code", "postcode_district", "district",
                 "outward_code", "postcode_area", "area", "zip", "zipcode"],
    "kpi": ["kpi", "conversions", "orders", "sales", "new_customers", "customers", "signups",
            "sign_ups", "leads", "revenue", "transactions"],
    "spend": ["spend", "cost", "media_spend", "ad_spend", "spend_gbp", "cost_gbp"],
}


class IngestError(ValueError):
    """The file cannot be used as it stands. The message says what to fix."""


@dataclass
class IngestResult:
    data: pd.DataFrame
    source: str
    data_version: str
    frequency: str                    # "daily" or "weekly"
    postcode_level: str               # most detailed level available for most of the KPI
    column_map: dict[str, str]        # standard name -> column in your file
    missing_dates: list[str] = field(default_factory=list)   # dates with no rows at all
    issues: list[dict] = field(default_factory=list)

    @property
    def date_range(self) -> tuple[str, str]:
        return (str(self.data["date"].min().date()), str(self.data["date"].max().date()))

    def summary(self) -> str:
        d = self.data
        lines = [
            f"Source: {self.source} (version {self.data_version})",
            f"Columns used: " + ", ".join(f"{k} = '{v}'" for k, v in self.column_map.items()),
            f"Dates: {self.date_range[0]} to {self.date_range[1]}, {self.frequency}, "
            f"{d['date'].nunique()} periods",
            f"Postcodes: {d['postcode'].nunique()} at {self.postcode_level} level, "
            f"in {d['area'].nunique()} postcode areas",
            f"Total KPI: {d['kpi'].sum():,.0f}"
            + (f"; total spend: {d['spend'].sum():,.0f}" if d['spend'].notna().any() else "; no spend column"),
        ]
        for i in self.issues:
            lines.append(f"[{i['severity']}] {i['check']}: {i['detail']}")
        return "\n".join(lines)


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")


def _resolve_columns(columns: list[str], column_map: dict[str, str] | None,
                     required: tuple[str, ...] = REQUIRED) -> dict[str, str]:
    column_map = dict(column_map or {})
    unknown = set(column_map) - set(STANDARD)
    if unknown:
        raise IngestError(f"column_map keys must be among {STANDARD}; got {sorted(unknown)}")
    for std, col in column_map.items():
        if col not in columns:
            raise IngestError(f"column '{col}' (given for {std}) is not in the file. Columns: {columns}")
    normed = {_norm(c): c for c in columns}
    for std in STANDARD:
        if std in column_map:
            continue
        hits = [normed[a] for a in ALIASES[std] if a in normed and normed[a] not in column_map.values()]
        if std in normed and normed[std] in hits:
            hits = [normed[std]]  # a column named exactly 'postcode', 'kpi' etc. wins
        if len(hits) > 1:
            raise IngestError(f"more than one column could be the {std}: {hits}. "
                              f"Say which one, e.g. column_map={{'{std}': '{hits[0]}'}}")
        if hits:
            column_map[std] = hits[0]
    missing = [s for s in required if s not in column_map]
    if missing:
        raise IngestError(f"could not find a column for {missing}. Columns in the file: {columns}. "
                          f"Say which is which, e.g. column_map={{'kpi': 'New Customers'}}")
    return column_map


def _to_number(s: pd.Series) -> pd.Series:
    cleaned = s.astype("string").str.replace(r"[£$€,\s]", "", regex=True).replace("", pd.NA)
    return pd.to_numeric(cleaned, errors="coerce")


def _parse_dates(s: pd.Series, issues: list[dict]) -> pd.Series:
    s = s.astype("string").str.strip()
    iso = pd.to_datetime(s, format="ISO8601", errors="coerce")
    if iso.notna().mean() > 0.99:
        return iso.dt.normalize()
    uk = pd.to_datetime(s, dayfirst=True, errors="coerce", format="mixed")
    if uk.notna().mean() > iso.notna().mean():
        issues.append({"severity": "info", "check": "date format",
                       "detail": "dates read as day/month/year (UK order)"})
        return uk.dt.normalize()
    return iso.dt.normalize()


def _frequency(dates: pd.Series) -> str:
    u = np.sort(dates.dropna().unique())
    if len(u) < 3:
        raise IngestError("fewer than 3 distinct dates; cannot work out daily or weekly frequency")
    step = pd.Series(np.diff(u)).dt.days.median()
    if step == 1:
        return "daily"
    if step == 7:
        return "weekly"
    raise IngestError(f"dates are {step:.0f} days apart on average; only daily or weekly data is supported")


def _read(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in (".xlsx", ".xls"):
        return pd.read_excel(path, dtype=str)
    return pd.read_csv(path, dtype=str, skipinitialspace=True)


def ingest(path: str | Path, column_map: dict[str, str] | None = None,
           raw: pd.DataFrame | None = None, spend_only: bool = False) -> IngestResult:
    """Read a CSV (or .xlsx, .csv.gz, .zip) of KPI by postcode and date into the standard format.

    column_map: standard name -> your column name, only needed where auto-detection fails,
                e.g. {"kpi": "New Customers", "postcode": "Billing Postcode"}.
    raw:        pass an already-loaded DataFrame instead of reading `path` (path is then a label).
    spend_only: for monitoring files (e.g. a platform location report): needs date, postcode and
                spend; any KPI column is ignored and kpi is set to missing.
    """
    path = Path(path)
    df = raw.astype("string") if raw is not None else _read(path)
    if df.empty:
        raise IngestError("the file has no rows")
    issues: list[dict] = []
    required = ("date", "postcode", "spend") if spend_only else REQUIRED
    cmap = _resolve_columns(list(df.columns), column_map, required)
    if spend_only:
        cmap.pop("kpi", None)
    n_in = len(df)

    out = pd.DataFrame({"date": _parse_dates(df[cmap["date"]], issues),
                        "raw_postcode": df[cmap["postcode"]].astype("string"),
                        "kpi": _to_number(df[cmap["kpi"]]) if not spend_only else 0.0})
    out["spend"] = _to_number(df[cmap["spend"]]) if "spend" in cmap else np.nan

    bad_date = out["date"].isna()
    if bad_date.any():
        ex = df.loc[bad_date, cmap["date"]].dropna().unique()[:3].tolist()
        issues.append({"severity": "warning", "check": "unreadable dates",
                       "detail": f"{bad_date.sum():,} rows dropped, e.g. {ex}"})
    bad_kpi = out["kpi"].isna() & ~bad_date
    if bad_kpi.any():
        ex = df.loc[bad_kpi, cmap["kpi"]].dropna().unique()[:3].tolist()
        issues.append({"severity": "warning", "check": "non-numeric KPI",
                       "detail": f"{bad_kpi.sum():,} rows dropped, e.g. {ex}"})
    out = out[~bad_date & ~bad_kpi]

    # Postcodes: parse each distinct value once
    parsed = {p: parse_postcode(p) for p in out["raw_postcode"].dropna().unique()}
    level = out["raw_postcode"].map(lambda p: parsed[p].level if p in parsed else "invalid")
    invalid = level == "invalid"
    if invalid.any():
        reasons = (out.loc[invalid, "raw_postcode"]
                   .map(lambda p: parsed[p].reason if p in parsed else "blank").value_counts())
        examples = out.loc[invalid, "raw_postcode"].dropna().unique()[:5].tolist()
        share = out.loc[invalid, "kpi"].sum() / max(out["kpi"].sum(), 1e-9)
        issues.append({"severity": "warning", "check": "unusable postcodes",
                       "detail": f"{invalid.sum():,} rows ({share:.1%} of KPI) dropped; "
                                 f"reasons: {dict(reasons)}; e.g. {examples}"})
    out, level = out[~invalid].copy(), level[~invalid]
    if out.empty:
        raise IngestError("no rows left after removing unreadable dates, KPI values and postcodes")
    out["area"] = out["raw_postcode"].map(lambda p: parsed[p].area)
    out["postcode"] = out["raw_postcode"].map(lambda p: parsed[p].district or parsed[p].area)

    kpi_by_level = out.groupby(level)["kpi"].sum()
    area_only_share = kpi_by_level.get("area", 0) / max(kpi_by_level.sum(), 1e-9)
    if 0 < area_only_share < 1:
        issues.append({"severity": "warning", "check": "mixed postcode detail",
                       "detail": f"{area_only_share:.1%} of KPI has only an area-level postcode; "
                                 "analysis at postcode-area level is fine, district level is not"})
    postcode_level = "area" if area_only_share > 0.5 else "district"
    if level.isin(["full", "sector"]).any():
        issues.append({"severity": "info", "check": "postcode detail",
                       "detail": "full or sector postcodes shortened to districts (outward codes)"})

    neg = out["kpi"] < 0
    if neg.any():
        issues.append({"severity": "warning", "check": "negative KPI",
                       "detail": f"{neg.sum():,} rows below zero (refunds or corrections?); kept as is"})
    if "spend" in cmap:
        blank = out["spend"].isna()
        if blank.any():
            issues.append({"severity": "info", "check": "blank spend",
                           "detail": f"{blank.sum():,} rows with blank spend treated as 0"})
        out["spend"] = out["spend"].fillna(0.0)

    # Collapse to one row per date and postcode
    dupes = out.duplicated(["date", "raw_postcode"]).sum()
    if dupes:
        issues.append({"severity": "warning", "check": "duplicate rows",
                       "detail": f"{dupes:,} rows repeat a date and postcode already seen; values summed"})
    out = (out.groupby(["date", "postcode", "area"])[["kpi", "spend"]]
           .sum(min_count=1).reset_index())

    # Fill the date x postcode grid; zeros for postcodes absent on a date
    freq = _frequency(out["date"])
    step = "D" if freq == "daily" else "7D"
    all_dates = pd.date_range(out["date"].min(), out["date"].max(), freq=step)
    missing_dates = sorted(set(all_dates) - set(out["date"]))
    if missing_dates:
        shown = [str(d.date()) for d in missing_dates[:5]]
        issues.append({"severity": "warning", "check": "dates with no data",
                       "detail": f"{len(missing_dates)} dates have no rows at all (export gap or tracking "
                                 f"outage?), e.g. {shown}; filled with zero, flagged in the quality report"})
    if freq == "weekly" and out["date"].dt.dayofweek.nunique() > 1:
        raise IngestError("weekly data must use the same weekday for every week")
    pcs = out[["postcode", "area"]].drop_duplicates().sort_values("postcode")
    grid = pd.MultiIndex.from_product([all_dates, pcs["postcode"]], names=["date", "postcode"])
    full = out.set_index(["date", "postcode"])[["kpi", "spend"]].reindex(grid)
    filled = int(full["kpi"].isna().sum())
    full["kpi"] = full["kpi"].fillna(0)
    if "spend" in cmap:
        full["spend"] = full["spend"].fillna(0.0)
    full = full.reset_index().merge(pcs, on="postcode", how="left")
    full = full[["date", "postcode", "area", "kpi", "spend"]].sort_values(["date", "postcode"],
                                                                         ignore_index=True)
    full["kpi"] = full["kpi"].astype("float64") if not spend_only else np.nan
    full["spend"] = full["spend"].astype("float64")
    full["postcode"] = full["postcode"].astype(str)
    full["area"] = full["area"].astype(str)
    if filled:
        issues.append({"severity": "info", "check": "zero-filled",
                       "detail": f"{filled:,} postcode-{'days' if freq == 'daily' else 'weeks'} "
                                 "with no row filled with 0"})
    issues.insert(0, {"severity": "info", "check": "rows read",
                      "detail": f"{n_in:,} rows in; {len(full):,} rows out "
                                f"({full['date'].nunique()} dates x {len(pcs)} postcodes)"})

    digest = hashlib.sha256(pd.util.hash_pandas_object(full, index=False).to_numpy().tobytes())
    return IngestResult(data=full, source=str(path), data_version=digest.hexdigest()[:12],
                        frequency=freq, postcode_level=postcode_level, column_map=cmap,
                        missing_dates=[str(d.date()) for d in missing_dates], issues=issues)
