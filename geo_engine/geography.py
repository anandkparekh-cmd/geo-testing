"""UK postcode geography: areas, regions and postcode parsing.

The 121 postcode area codes are real. The area-to-region mapping is
approximate: several areas cross regional boundaries, and each is assigned to
the region holding most of it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

REGIONS: dict[str, list[str]] = {
    "London": ["E", "EC", "N", "NW", "SE", "SW", "W", "WC", "BR", "CR", "EN",
               "HA", "IG", "KT", "RM", "SM", "TW", "UB"],
    "South East": ["BN", "CT", "DA", "GU", "HP", "ME", "MK", "OX", "PO", "RG",
                   "RH", "SL", "SO", "TN"],
    "East of England": ["AL", "CB", "CM", "CO", "IP", "LU", "NR", "PE", "SG",
                        "SS", "WD"],
    "South West": ["BA", "BH", "BS", "DT", "EX", "GL", "PL", "SN", "SP", "TA",
                   "TQ", "TR"],
    "West Midlands": ["B", "CV", "DY", "HR", "ST", "TF", "WR", "WS", "WV"],
    "East Midlands": ["DE", "LE", "LN", "NG", "NN"],
    "Yorkshire and the Humber": ["BD", "DN", "HD", "HG", "HU", "HX", "LS", "S",
                                 "WF", "YO"],
    "North West": ["BB", "BL", "CA", "CH", "CW", "FY", "L", "LA", "M", "OL",
                   "PR", "SK", "WA", "WN"],
    "North East": ["DH", "DL", "NE", "SR", "TS"],
    "Wales": ["CF", "LD", "LL", "NP", "SA", "SY"],
    "Scotland": ["AB", "DD", "DG", "EH", "FK", "G", "HS", "IV", "KA", "KW",
                 "KY", "ML", "PA", "PH", "TD", "ZE"],
    "Northern Ireland": ["BT"],
}

AREA_TO_REGION = {a: r for r, areas in REGIONS.items() for a in areas}
AREAS = sorted(AREA_TO_REGION)
CROWN_DEPENDENCIES = {"GY", "JE", "IM"}  # outside UK ad-targeting regions; reported, not used

assert len(AREAS) == 121, "expected 121 UK postcode areas"

_FULL = re.compile(r"^([A-Z]{1,2})([0-9][0-9A-Z]?)\s*([0-9][A-Z]{2})$")    # SW1A 1AA, M11AE
_SECTOR = re.compile(r"^([A-Z]{1,2})([0-9][0-9A-Z]?)\s+([0-9])$")          # SW1A 1
_DISTRICT = re.compile(r"^([A-Z]{1,2})([0-9][0-9A-Z]?)$")                  # SW1A, M1
_AREA = re.compile(r"^([A-Z]{1,2})$")                                      # SW, M


@dataclass(frozen=True)
class ParsedPostcode:
    raw: str
    area: str | None
    district: str | None      # outward code, e.g. "SW1A"; None if only the area was given
    level: str                # "full", "sector", "district", "area" or "invalid"
    reason: str | None = None  # why it was rejected, when invalid


def parse_postcode(raw) -> ParsedPostcode:
    """Parse a UK postcode at any level of detail down to its district and area."""
    text = "" if raw is None else str(raw)
    s = re.sub(r"\s+", " ", text.strip().upper())
    for level, pattern in (("full", _FULL), ("sector", _SECTOR), ("district", _DISTRICT)):
        m = pattern.match(s)
        if m:
            area, district = m.group(1), m.group(1) + m.group(2)
            return _check_area(text, area, district, level)
    m = _AREA.match(s)
    if m:
        return _check_area(text, m.group(1), None, "area")
    return ParsedPostcode(text, None, None, "invalid", "not a UK postcode format")


def _check_area(raw: str, area: str, district: str | None, level: str) -> ParsedPostcode:
    if area in AREA_TO_REGION:
        return ParsedPostcode(raw, area, district, level)
    if area in CROWN_DEPENDENCIES:
        return ParsedPostcode(raw, None, None, "invalid", "Channel Islands or Isle of Man")
    return ParsedPostcode(raw, None, None, "invalid", f"unknown postcode area '{area}'")
