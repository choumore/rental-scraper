from __future__ import annotations

import json
import random
import re
import time
from typing import Optional

import requests
from bs4 import BeautifulSoup

from config import (
    CL_REGION,
    CL_REQUEST_DELAY_RANGE,
    CL_SUBDOMAIN,
    MAX_PRICE,
    MIN_BATHS,
    MIN_BEDS,
)
from scrapers.base import Listing

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]


def _ua_headers() -> dict:
    return {"User-Agent": random.choice(USER_AGENTS)}


def _jitter_sleep() -> None:
    lo, hi = CL_REQUEST_DELAY_RANGE
    time.sleep(random.uniform(lo, hi))


def _build_search_url() -> str:
    base = f"https://{CL_SUBDOMAIN}.craigslist.org/search/{CL_REGION}/apa"
    params = (
        f"min_bedrooms={MIN_BEDS}"
        f"&min_bathrooms={int(MIN_BATHS)}"
        f"&max_price={MAX_PRICE}"
        f"&housing_type=6"  # 6 = house
    )
    return f"{base}?{params}"


def fetch_craigslist_listings(detail_limit: Optional[int] = None) -> list[Listing]:
    search_url = _build_search_url()
    print(f"[cl] fetching search: {search_url}")
    r = requests.get(search_url, headers=_ua_headers(), timeout=20)
    if r.status_code != 200:
        print(f"[cl] search HTTP {r.status_code}, aborting")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    cards = _parse_search_cards(soup)
    print(f"[cl] search returned {len(cards)} cards")

    if detail_limit is not None:
        cards = cards[:detail_limit]

    listings: list[Listing] = []
    for i, card in enumerate(cards):
        if i > 0:
            _jitter_sleep()
        listing = _fetch_detail(card)
        if listing:
            listings.append(listing)
    return listings


def _parse_search_cards(soup: BeautifulSoup) -> list[dict]:
    """Combine the static <li> result cards with the LD+JSON itemList.

    The static list gives URL/title/price/city. The LD+JSON adds beds/baths/lat-lng.
    They're parallel arrays in the same order.
    """
    static_items: list[dict] = []
    for li in soup.select("li.cl-static-search-result"):
        a = li.find("a", href=True)
        if not a:
            continue
        title_el = li.select_one(".title")
        price_el = li.select_one(".price")
        loc_el = li.select_one(".location")
        static_items.append({
            "url": a["href"],
            "title": (title_el.get_text(strip=True) if title_el else "").strip(),
            "price": _parse_price(price_el.get_text(strip=True) if price_el else ""),
            "city": (loc_el.get_text(strip=True) if loc_el else "").strip(),
        })

    ld_items: list[dict] = []
    ld_script = soup.find("script", id="ld_searchpage_results")
    if ld_script and ld_script.string:
        try:
            data = json.loads(ld_script.string)
            for entry in data.get("itemListElement", []):
                item = entry.get("item", {}) if isinstance(entry, dict) else {}
                ld_items.append(item)
        except json.JSONDecodeError as e:
            print(f"[cl] LD+JSON parse failed: {e}")

    # Zip by position; LD list may be empty if Craigslist changed format
    cards: list[dict] = []
    for i, s in enumerate(static_items):
        ld = ld_items[i] if i < len(ld_items) else {}
        cards.append({
            **s,
            "beds": ld.get("numberOfBedrooms"),
            "baths": ld.get("numberOfBathroomsTotal"),
            "lat": ld.get("latitude"),
            "lng": ld.get("longitude"),
            "ld_address": ld.get("address", {}) if isinstance(ld.get("address"), dict) else {},
        })
    return cards


def _parse_price(s: str) -> int:
    digits = "".join(c for c in s if c.isdigit())
    return int(digits) if digits else 0


def _fetch_detail(card: dict) -> Optional[Listing]:
    url = card["url"]
    pid = _extract_post_id(url)
    try:
        r = requests.get(url, headers=_ua_headers(), timeout=20)
    except requests.RequestException as e:
        print(f"[cl] {pid}: request failed ({e}), skipping")
        return None

    if r.status_code != 200:
        print(f"[cl] {pid}: HTTP {r.status_code}, skipping")
        return None

    soup = BeautifulSoup(r.text, "html.parser")

    description = _extract_description(soup)
    images = _extract_images(soup)
    address = _extract_address(soup)
    posted_at = _extract_posted_at(soup)

    # Prefer detail-page values if richer, fallback to search-card values
    beds = float(card.get("beds") or 0)
    baths = float(card.get("baths") or 0)
    if beds == 0 or baths == 0:
        bb = _extract_beds_baths_from_detail(soup)
        if beds == 0:
            beds = bb[0]
        if baths == 0:
            baths = bb[1]

    # City from search card already; fallback to detail-page address parsing
    city = card.get("city", "") or _city_from_address(address)

    lat = _to_float_or_none(card.get("lat"))
    lng = _to_float_or_none(card.get("lng"))
    sqft = _extract_sqft(soup)
    available_date = _extract_available_date(description)

    return Listing(
        id=f"cl_{pid}",
        source="craigslist",
        url=url,
        address=address,
        city=city,
        price=card.get("price", 0) or _extract_price_from_detail(soup),
        beds=beds,
        baths=baths,
        description=description,
        image_urls=images,
        property_type="house",
        posted_at=posted_at,
        lat=lat,
        lng=lng,
        sqft=sqft,
        available_date=available_date,
    )


def _extract_sqft(soup: BeautifulSoup) -> int:
    """Pull square footage from Craigslist attribute groups.

    Patterns: "1840ft2", "1840 ft2", "1840 sqft", "1,840 sq ft".
    """
    text_blobs: list[str] = []
    for sel in [".attrgroup span", ".shared-line-bubble", ".housing"]:
        for el in soup.select(sel):
            text_blobs.append(el.get_text(" ", strip=True))
    joined = " ".join(text_blobs).lower().replace(",", "")
    # Craigslist renders <sup>2</sup> as a separate "2" with a space.
    # Allow optional whitespace between "ft" and "2".
    m = re.search(r"(\d{3,5})\s*(?:ft\s*2|ft²|sq\s*ft|sqft)\b", joined)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return 0
    return 0


# Month-name lookup for date parsing. Lowercase keys.
_MONTH_NAMES = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def _extract_available_date(description: str) -> str:
    """Best-effort parse of move-in date from a Craigslist listing body.

    Returns ISO YYYY-MM-DD on success, empty string otherwise.

    Supported patterns (case-insensitive):
      - "available now" / "available immediately" / "asap" / "move in immediately"
      - "available [Month] [DD]" / "available [Month] [DD], YYYY"
      - "available M/D" / "available M/D/YYYY" / "available M/D/YY"
      - "move-in: [date]" / "move in: [date]"
    """
    from datetime import date
    if not description:
        return ""
    text = description.lower()

    # Specific-date patterns FIRST. "available now" is the fallback because
    # listings often say BOTH "Available May 15th" AND "available now to schedule a tour".

    # Pattern 1: "[available|move-in|ready] <Month> <Day>[, <Year>]"
    m = re.search(
        r"(?:available|move[- ]?in|ready)[^.\n]{0,30}?"
        r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
        r"aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
        r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:[,\s]+(\d{2,4}))?\b",
        text,
    )
    if m:
        month = _MONTH_NAMES.get(m.group(1))
        day = int(m.group(2))
        year_raw = m.group(3)
        year = _resolve_year(year_raw, month or 1)
        if month and year:
            try:
                return date(year, month, day).isoformat()
            except ValueError:
                pass

    # Pattern 2: "[available|move-in|ready] M/D[/YY[YY]]"
    m = re.search(
        r"(?:available|move[- ]?in|ready)[^.\n]{0,30}?"
        r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b",
        text,
    )
    if m:
        month = int(m.group(1))
        day = int(m.group(2))
        year_raw = m.group(3)
        year = _resolve_year(year_raw, month)
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            pass

    # Fallback: explicit "right now" phrasing without any specific date in the listing.
    # Require context-binding tokens so we don't match random "asap" mentions.
    if re.search(r"\bavailable\s+(now|immediately)\b", text):
        return date.today().isoformat()
    if re.search(r"\bmove[- ]?in\s+(now|immediately|asap)\b", text):
        return date.today().isoformat()
    if re.search(r"\bready\s+(now|immediately)\b", text):
        return date.today().isoformat()

    return ""


def _resolve_year(year_raw: Optional[str], month: int) -> int:
    """Resolve a year reference. If empty, assume current year (or next if month is in the past)."""
    from datetime import date
    today = date.today()
    if year_raw:
        y = int(year_raw)
        return 2000 + y if y < 100 else y
    # No year given. Assume current year if month is current+future, else next year.
    if month >= today.month:
        return today.year
    return today.year + 1


def _to_float_or_none(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _extract_post_id(url: str) -> str:
    m = re.search(r"/(\d{8,})\.html", url)
    if m:
        return m.group(1)
    return url.rstrip("/").split("/")[-1].split(".")[0]


def _extract_price_from_detail(soup: BeautifulSoup) -> int:
    for sel in ["span.price", ".price"]:
        el = soup.select_one(sel)
        if el:
            digits = "".join(c for c in el.get_text() if c.isdigit())
            if digits:
                return int(digits)
    return 0


def _extract_beds_baths_from_detail(soup: BeautifulSoup) -> tuple[float, float]:
    beds = 0.0
    baths = 0.0
    text_blobs: list[str] = []
    for sel in [".attrgroup span", ".shared-line-bubble", ".housing"]:
        for el in soup.select(sel):
            text_blobs.append(el.get_text(" ", strip=True))
    joined = " ".join(text_blobs).lower()

    m = re.search(r"(\d+(?:\.\d+)?)\s*br\b", joined)
    if m:
        try:
            beds = float(m.group(1))
        except ValueError:
            pass
    m = re.search(r"(\d+(?:\.\d+)?)\s*ba\b", joined)
    if m:
        try:
            baths = float(m.group(1))
        except ValueError:
            pass
    return beds, baths


def _extract_description(soup: BeautifulSoup) -> str:
    el = soup.select_one("#postingbody")
    if not el:
        return ""
    for noise in el.select(".print-information, .print-qrcode-container, .notices"):
        noise.decompose()
    return el.get_text("\n", strip=True)


def _extract_images(soup: BeautifulSoup) -> list[str]:
    urls: list[str] = []
    for a in soup.select("#thumbs a[href]"):
        href = a.get("href", "")
        if isinstance(href, str) and href.startswith("http"):
            urls.append(href)
    if not urls:
        for img in soup.select(".slide img[src], .gallery img[src]"):
            src = img.get("src", "")
            if isinstance(src, str) and src.startswith("http"):
                urls.append(src)
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _extract_address(soup: BeautifulSoup) -> str:
    el = soup.select_one(".mapaddress")
    if not el:
        return ""
    text = el.get_text(strip=True)
    # When no real address is provided, Craigslist's map widget surfaces a
    # bare "google map" link — reject these placeholders so cross-source dedup
    # falls back cleanly to listing_id only.
    if text.lower() in ("google map", "view on map", "view map", ""):
        return ""
    return text


def _extract_posted_at(soup: BeautifulSoup) -> str:
    el = soup.select_one("time.date.timeago[datetime], time[datetime]")
    if el:
        dt = el.get("datetime", "")
        if isinstance(dt, str):
            return dt
    return ""


def _city_from_address(addr: str) -> str:
    # Best-effort: "123 Main St, Palo Alto, CA" → "Palo Alto"
    if "," in addr:
        parts = [p.strip() for p in addr.split(",")]
        if len(parts) >= 2:
            return parts[-2]
    return ""
