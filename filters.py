from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, timedelta

from config import (
    BACKYARD_KEYWORDS,
    EXCLUDE_KEYWORDS,
    GRACE_DAYS,
    MAX_DISTANCE_MILES,
    MAX_PRICE,
    MIN_BATHS,
    MIN_BEDS,
    MIN_PRICE,
    MOVE_BY_DATE,
    RENOVATION_KEYWORDS,
    SYNAPSE_COORDS,
)
from scrapers.base import Listing


@dataclass
class FilterResult:
    passes: bool
    reason: str = ""
    flags: dict[str, bool] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.flags is None:
            self.flags = {}


# Street-suffix variants we collapse to a canonical form.
_SUFFIX_MAP = {
    "street": "st", "st.": "st", "st": "st",
    "avenue": "ave", "ave.": "ave", "ave": "ave",
    "road": "rd", "rd.": "rd", "rd": "rd",
    "drive": "dr", "dr.": "dr", "dr": "dr",
    "boulevard": "blvd", "blvd.": "blvd", "blvd": "blvd",
    "court": "ct", "ct.": "ct", "ct": "ct",
    "circle": "cir", "cir.": "cir", "cir": "cir",
    "lane": "ln", "ln.": "ln", "ln": "ln",
    "place": "pl", "pl.": "pl", "pl": "pl",
    "way": "way",
    "terrace": "ter", "ter.": "ter", "ter": "ter",
    "parkway": "pkwy", "pkwy.": "pkwy", "pkwy": "pkwy",
    "highway": "hwy", "hwy.": "hwy", "hwy": "hwy",
    "trail": "trl", "trl.": "trl", "trl": "trl",
    "north": "n", "n.": "n", "n": "n",
    "south": "s", "s.": "s", "s": "s",
    "east": "e", "e.": "e", "e": "e",
    "west": "w", "w.": "w", "w": "w",
}

# Canonical street suffixes — used to truncate trailing noise like "near X" or
# "between Y and Z" that Craigslist appends to addresses.
_CANONICAL_SUFFIXES = {
    "st", "ave", "rd", "dr", "blvd", "ct", "cir", "ln", "pl", "way",
    "ter", "pkwy", "hwy", "trl",
}


def _truncate_at_suffix(tokens: list[str]) -> list[str]:
    """Cut tokens at the first street suffix, inclusive. Robust to trailing noise."""
    for i, tok in enumerate(tokens):
        if tok in _CANONICAL_SUFFIXES:
            return tokens[: i + 1]
    return tokens


def normalize_address(addr: str) -> str:
    """Normalize a street address for cross-source dedup.

    Strips unit/apt suffixes, normalizes Street/St/St. variants, lowercases,
    collapses whitespace. Cross-source dedup key.
    """
    if not addr:
        return ""

    s = addr.lower().strip()

    # Strip trailing ", City, State Zip" portion if present — keep only street
    s = s.split(",")[0].strip()

    # Strip unit/apt/# patterns
    s = re.sub(r"\s+(apt|apartment|unit|suite|ste|#)\s*\S+\s*$", "", s)
    s = re.sub(r"\s+#\S+\s*$", "", s)

    # Replace punctuation with space
    s = re.sub(r"[.,;]", " ", s)

    # Tokenize, normalize each token via suffix map
    tokens = [t for t in s.split() if t]
    canon = [_SUFFIX_MAP.get(t, t) for t in tokens]

    # Truncate trailing noise after the street suffix ("near X", "between Y and Z").
    canon = _truncate_at_suffix(canon)

    return " ".join(canon)


def normalize_city(city: str) -> str:
    """Lowercase + collapse whitespace. Empty string if missing."""
    if not city:
        return ""
    return re.sub(r"\s+", " ", city.strip().lower())


def haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two points in miles."""
    r_miles = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r_miles * c


def miles_from_synapse(lat: float | None, lng: float | None) -> float | None:
    if lat is None or lng is None:
        return None
    s_lat, s_lng = SYNAPSE_COORDS
    return haversine_miles(lat, lng, s_lat, s_lng)


@dataclass
class AvailabilityVerdict:
    passes: bool
    reason: str = ""
    late_move_in: bool = False
    unknown: bool = False
    parsed_date: str = ""


def check_availability(available_date_iso: str) -> AvailabilityVerdict:
    """Apply the move-by / grace-window policy to an ISO date string.

      ≤ MOVE_BY_DATE                 → pass, ideal
      MOVE_BY_DATE → +GRACE_DAYS     → pass, flagged late_move_in
      > MOVE_BY_DATE + GRACE_DAYS    → reject
      empty or unparseable           → pass, flagged unknown
    """
    if not available_date_iso:
        return AvailabilityVerdict(passes=True, unknown=True)
    try:
        d = date.fromisoformat(available_date_iso)
    except ValueError:
        return AvailabilityVerdict(passes=True, unknown=True)

    move_by = date.fromisoformat(MOVE_BY_DATE)
    grace_end = move_by + timedelta(days=GRACE_DAYS)

    if d <= move_by:
        return AvailabilityVerdict(passes=True, parsed_date=d.isoformat())
    if d <= grace_end:
        return AvailabilityVerdict(passes=True, late_move_in=True, parsed_date=d.isoformat())
    return AvailabilityVerdict(
        passes=False,
        reason=f"available_after_grace ({d.isoformat()} > {grace_end.isoformat()})",
        parsed_date=d.isoformat(),
    )


def score_flags(text: str) -> dict[str, bool]:
    """Soft signals — returned as a dict, never used for filtering."""
    t = (text or "").lower()
    return {
        "renovation": any(kw in t for kw in RENOVATION_KEYWORDS),
        "backyard": any(kw in t for kw in BACKYARD_KEYWORDS),
    }


def filter_listing(l: Listing) -> FilterResult:
    """Hard filters, fail-fast. Returns FilterResult with passes/reason/flags.

    flags is always populated regardless of pass/fail (useful for diagnostics).
    """
    blob = f"{l.address} {l.description}".lower()
    flags = score_flags(blob)

    if not l.price or l.price <= 0:
        return FilterResult(False, "no_price", flags)
    if l.price < MIN_PRICE:
        return FilterResult(False, f"price_under_min ({l.price} < {MIN_PRICE})", flags)
    if l.price > MAX_PRICE:
        return FilterResult(False, f"price_over_max ({l.price} > {MAX_PRICE})", flags)
    if l.beds < MIN_BEDS:
        return FilterResult(False, f"beds_under_min ({l.beds} < {MIN_BEDS})", flags)
    if l.baths < MIN_BATHS:
        return FilterResult(False, f"baths_under_min ({l.baths} < {MIN_BATHS})", flags)

    distance = miles_from_synapse(l.lat, l.lng)
    if distance is None:
        return FilterResult(False, "no_coordinates", flags)
    if distance > MAX_DISTANCE_MILES:
        return FilterResult(
            False, f"too_far ({distance:.1f}mi > {MAX_DISTANCE_MILES}mi)", flags
        )

    # Hard property-type exclusions via description keywords. Catches mis-tagged
    # townhomes/condos that slipped past source-side property-type filters.
    title_desc = f"{l.address} {l.description}".lower()
    for kw in EXCLUDE_KEYWORDS:
        if kw in title_desc:
            return FilterResult(False, f"exclude_keyword ({kw!r})", flags)

    avail = check_availability(l.available_date)
    if not avail.passes:
        return FilterResult(False, avail.reason, flags)
    if avail.late_move_in:
        flags["late_move_in"] = True
    if avail.unknown:
        flags["unknown_availability"] = True

    return FilterResult(True, "ok", flags)
