from __future__ import annotations

import json
import os
import urllib.parse
from decimal import Decimal
from typing import Any

from apify_client import ApifyClient

import config
from config import (
    APIFY_ACTOR,
    MAX_PRICE,
    MIN_BATHS,
    MIN_BEDS,
    SEARCH_BOUNDS,
)
from scrapers.base import Listing

DETAIL_ACTOR = "maxcopell/zillow-detail-scraper"


def _build_search_url() -> str:
    """Build a Zillow rentals search URL.

    Required: /homes/for_rent/ path + searchQueryState with mapBounds and
    the rent flags (fr:true, fsba/fsbo/nc/cmsn/auc/fore:false). The path
    alone does NOT enforce rent-only; flags do. The /{city}/rentals/ path
    silently returns 0 results without a customRegionId.
    """
    bounds = SEARCH_BOUNDS

    filter_state: dict[str, Any] = {
        "sort": {"value": "days"},
        # Property type: single-family only
        "sfh": {"value": True},
        "tow": {"value": False},
        "mf": {"value": False},
        "con": {"value": False},
        "apa": {"value": False},
        "manu": {"value": False},
        # Rent-only flags
        "fr": {"value": True},
        "fsba": {"value": False},
        "fsbo": {"value": False},
        "nc": {"value": False},
        "cmsn": {"value": False},
        "auc": {"value": False},
        "fore": {"value": False},
        # Numeric filters
        "mp": {"max": MAX_PRICE},
        "beds": {"min": MIN_BEDS},
        "baths": {"min": MIN_BATHS},
    }
    if config.DAYS_ON_ZILLOW:
        filter_state["doz"] = {"value": str(config.DAYS_ON_ZILLOW)}

    query_state = {
        "isMapVisible": True,
        "mapBounds": bounds,
        "filterState": filter_state,
        "isListVisible": True,
    }
    encoded = urllib.parse.quote(json.dumps(query_state, separators=(",", ":")), safe="")
    return f"https://www.zillow.com/homes/for_rent/?searchQueryState={encoded}"


def fetch_zillow_listings() -> list[Listing]:
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        raise RuntimeError("APIFY_TOKEN not set in environment")

    client = ApifyClient(token)
    search_url = _build_search_url()

    run_input = {
        "searchUrls": [{"url": search_url}],
        "extractionMethod": "PAGINATION_WITH_ZOOM_IN",
        "maxItems": 200,
    }

    print(f"[zillow] running {APIFY_ACTOR} (doz={config.DAYS_ON_ZILLOW}, bounds={SEARCH_BOUNDS})...")
    run = client.actor(APIFY_ACTOR).call(run_input=run_input, max_total_charge_usd=Decimal("1.00"))

    results: list[Listing] = []
    errors = 0
    for item in client.dataset(run["defaultDatasetId"]).iterate_items():
        if not isinstance(item, dict):
            continue
        if "error" in item and len(item.keys()) <= 2:
            errors += 1
            print(f"[zillow] actor returned error record: {item['error']}")
            continue
        try:
            results.append(_parse_item(item))
        except Exception as e:  # noqa: BLE001 — log and continue
            print(f"[zillow] parse failed for item: {e}")
    if errors:
        print(f"[zillow] {errors} error records skipped")
    print(f"[zillow] parsed {len(results)} listings")
    return results


def _parse_item(item: dict) -> Listing:
    zpid = item.get("zpid") or item.get("id") or "unknown"
    home_info = (item.get("hdpData") or {}).get("homeInfo", {}) if isinstance(item.get("hdpData"), dict) else {}

    street = (
        item.get("addressStreet")
        or home_info.get("streetAddress")
        or _street_from_full(item.get("address", ""))
        or ""
    )
    city = item.get("addressCity") or home_info.get("city") or ""

    detail_url = item.get("detailUrl") or ""
    if detail_url and not detail_url.startswith("http"):
        detail_url = "https://www.zillow.com" + detail_url

    lat = _to_float_or_none(
        (item.get("latLong") or {}).get("latitude") if isinstance(item.get("latLong"), dict) else None
    ) or _to_float_or_none(home_info.get("latitude"))
    lng = _to_float_or_none(
        (item.get("latLong") or {}).get("longitude") if isinstance(item.get("latLong"), dict) else None
    ) or _to_float_or_none(home_info.get("longitude"))

    sqft = _to_int(item.get("area") or home_info.get("livingArea") or 0)

    return Listing(
        id=f"zillow_{zpid}",
        source="zillow",
        url=detail_url,
        address=item.get("address") or street,
        city=city,
        price=_to_int(item.get("unformattedPrice") or home_info.get("price") or item.get("price") or 0),
        beds=_to_float(item.get("beds") or home_info.get("bedrooms") or 0),
        baths=_to_float(item.get("baths") or home_info.get("bathrooms") or 0),
        description="",
        image_urls=_collect_photos(item),
        property_type=home_info.get("homeType") or "",
        posted_at=_extract_posted_at(item, home_info),
        lat=lat,
        lng=lng,
        sqft=sqft,
        available_date="",  # Zillow search response doesn't expose move-in date
    )


def _to_float_or_none(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_zillow_details(listings: list[Listing]) -> int:
    """Enrich Zillow listings IN PLACE with detail-page data via the detail actor.

    Adds: full description, availability_date, pets_allowed, additional photos.
    Returns count of listings successfully enriched. Skips non-Zillow listings.
    Cost: ~$0.0017 per Zillow URL. Caller should pass already-filtered listings
    to avoid paying for detail data we'd reject anyway.
    """
    zillow_urls: list[tuple[str, str]] = []  # (zpid, url) pairs
    for l in listings:
        if l.source != "zillow":
            continue
        if "/homedetails/" not in l.url:
            continue
        zpid = l.id.replace("zillow_", "")
        zillow_urls.append((zpid, l.url))

    if not zillow_urls:
        return 0

    token = os.environ.get("APIFY_TOKEN")
    if not token:
        print("[zillow-detail] APIFY_TOKEN not set; skipping detail enrichment")
        return 0

    client = ApifyClient(token)
    print(f"[zillow-detail] fetching {len(zillow_urls)} detail pages via {DETAIL_ACTOR}...")
    run_input = {"startUrls": [{"url": u} for _zpid, u in zillow_urls]}
    try:
        run = client.actor(DETAIL_ACTOR).call(run_input=run_input)
    except Exception as e:  # noqa: BLE001
        print(f"[zillow-detail] actor call failed: {e}")
        return 0

    details_by_zpid: dict[str, dict] = {}
    for item in client.dataset(run["defaultDatasetId"]).iterate_items():
        if not isinstance(item, dict) or "error" in item:
            continue
        zpid = str(item.get("zpid") or "")
        if zpid:
            details_by_zpid[zpid] = item

    enriched = 0
    for l in listings:
        if l.source != "zillow":
            continue
        zpid = l.id.replace("zillow_", "")
        detail = details_by_zpid.get(zpid)
        if not detail:
            continue
        _merge_detail(l, detail)
        enriched += 1

    print(f"[zillow-detail] enriched {enriched}/{len(zillow_urls)} listings")
    return enriched


def _merge_detail(listing: Listing, detail: dict) -> None:
    """Copy useful fields from a detail-actor result into a Listing in place.

    The fields we care about live under `resoFacts` (not top-level) — that's
    where Zillow puts MLS-sourced rental data. The actor exposes them as:
      resoFacts.availabilityDate  — Unix milliseconds epoch
      resoFacts.allowedPets        — list[str], e.g. ['Cats', 'Small Dogs']
                                     empty list → no pets allowed
                                     None → unknown
    """
    reso = detail.get("resoFacts") if isinstance(detail.get("resoFacts"), dict) else {}

    avail_ms = reso.get("availabilityDate")
    if isinstance(avail_ms, (int, float)) and avail_ms > 0:
        from datetime import datetime, timezone
        try:
            listing.available_date = (
                datetime.fromtimestamp(avail_ms / 1000, tz=timezone.utc).date().isoformat()
            )
        except (ValueError, OSError, OverflowError):
            pass

    desc = detail.get("description")
    if isinstance(desc, str) and desc.strip():
        listing.description = desc.strip()

    allowed_pets = reso.get("allowedPets")
    if isinstance(allowed_pets, list):
        if allowed_pets:
            listing.pets_allowed = ", ".join(str(p) for p in allowed_pets if p)
        else:
            listing.pets_allowed = "no"
    # If allowed_pets is None / missing, leave as "" (unknown).

    photos = detail.get("responsivePhotos") or detail.get("photos") or []
    if isinstance(photos, list) and photos:
        urls: list[str] = []
        seen_urls: set[str] = set()
        for p in photos:
            url = _photo_url(p)
            if url and url not in seen_urls:
                urls.append(url)
                seen_urls.add(url)
        if urls:
            listing.image_urls = urls


def _photo_url(p) -> str:
    if isinstance(p, str) and p.startswith("http"):
        return p
    if not isinstance(p, dict):
        return ""
    direct = p.get("url") or p.get("href")
    if isinstance(direct, str) and direct.startswith("http"):
        return direct
    mixed = p.get("mixedSources") or {}
    jpeg = mixed.get("jpeg") if isinstance(mixed, dict) else None
    if isinstance(jpeg, list) and jpeg:
        # Pick the largest by width
        best = max(jpeg, key=lambda x: x.get("width", 0) if isinstance(x, dict) else 0)
        if isinstance(best, dict):
            u = best.get("url")
            if isinstance(u, str) and u.startswith("http"):
                return u
    return ""


def _extract_posted_at(item: dict, home_info: dict) -> str:
    """Prefer flexFieldText ('1 hour ago' / '2 days ago'). Otherwise return
    daysOnZillow as a numeric string. Critically: 0 is a valid value (posted today)."""
    fft = item.get("flexFieldText")
    if isinstance(fft, str) and fft.strip():
        return fft.strip()
    doz = home_info.get("daysOnZillow")
    if isinstance(doz, (int, float)):
        return str(int(doz))
    if isinstance(doz, str) and doz.strip():
        return doz.strip()
    return ""


def _street_from_full(addr: str) -> str:
    if "," in addr:
        return addr.split(",")[0].strip()
    return addr


def _to_int(v: Any) -> int:
    if isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        digits = "".join(c for c in v if c.isdigit())
        return int(digits) if digits else 0
    return 0


def _to_float(v: Any) -> float:
    if isinstance(v, bool):
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip().rstrip("+"))
        except ValueError:
            return 0.0
    return 0.0


def _collect_photos(item: dict) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    # Single hero image
    hero = item.get("imgSrc")
    if isinstance(hero, str) and hero.startswith("http"):
        urls.append(hero)
        seen.add(hero)

    # Carousel — shape: list of {"mixedSources": {"jpeg": [{"url": ..., "width": ...}, ...]}, ...}
    carousel = item.get("carouselPhotosComposable") or item.get("photos") or []
    if isinstance(carousel, list):
        for p in carousel:
            if isinstance(p, str) and p.startswith("http") and p not in seen:
                urls.append(p)
                seen.add(p)
            elif isinstance(p, dict):
                u = p.get("url") or p.get("href")
                if isinstance(u, str) and u.startswith("http") and u not in seen:
                    urls.append(u)
                    seen.add(u)
                    continue
                mixed = p.get("mixedSources") or {}
                jpeg = mixed.get("jpeg") if isinstance(mixed, dict) else None
                if isinstance(jpeg, list) and jpeg:
                    # Pick the largest by width if available
                    best = max(
                        jpeg,
                        key=lambda x: x.get("width", 0) if isinstance(x, dict) else 0,
                    )
                    if isinstance(best, dict):
                        u2 = best.get("url")
                        if isinstance(u2, str) and u2.startswith("http") and u2 not in seen:
                            urls.append(u2)
                            seen.add(u2)
    return urls
