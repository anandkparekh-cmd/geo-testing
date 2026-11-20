"""UK postcode areas used by the synthetic data generator.

The 121 area codes are real. Regions are approximate (several areas cross
regional boundaries) and the size tiers are rough relative sizes, good enough
to give the synthetic world realistic structure. District codes generated
from these areas (e.g. "M1", "M2") are illustrative, not a real district list.
"""

from __future__ import annotations

from ..geography import REGIONS  # single source for areas and regions

# Rough relative size tiers. Anything not listed is tier 3 (mid-sized).
TIER_4_LARGE = {"B", "BT", "M", "SE", "SW", "G", "LS", "E", "BS", "NE", "S",
                "NG", "PE", "CM", "RG", "CF", "EH", "L", "LE", "ST", "CV",
                "TN", "SO", "PO", "GU", "BN", "KT"}
TIER_2_SMALL = {"AB", "PH", "HR", "LA", "CA", "DL", "TR", "TQ", "DT", "SP",
                "LL", "SY", "FK", "KA", "KY", "ML", "PA", "DD", "HX", "HD",
                "WD", "AL", "LU", "SG", "SL", "HP", "MK", "TF", "BB", "BL",
                "FY", "OL", "WN", "CW", "SR", "DH", "LN", "CO", "CT", "BA",
                "TA", "BR", "EN", "SM", "UB"}
TIER_1_TINY = {"ZE", "KW", "HS", "LD", "TD", "DG", "EC", "WC", "HG", "IV"}

TIER_WEIGHT = {1: 0.25, 2: 0.6, 3: 1.0, 4: 1.8}
TIER_DISTRICTS = {1: 2, 2: 4, 3: 6, 4: 10}


def tier(area: str) -> int:
    if area in TIER_4_LARGE:
        return 4
    if area in TIER_1_TINY:
        return 1
    if area in TIER_2_SMALL:
        return 2
    return 3


def area_table() -> list[dict]:
    """One row per postcode area: area code, region, size tier."""
    rows = []
    for region, areas in REGIONS.items():
        for area in areas:
            rows.append({"area": area, "region": region, "tier": tier(area)})
    rows.sort(key=lambda r: r["area"])
    return rows


AREAS = [r["area"] for r in area_table()]
assert len(AREAS) == 121 and len(set(AREAS)) == 121, "expected 121 unique UK postcode areas"
