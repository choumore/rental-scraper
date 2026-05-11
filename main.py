from __future__ import annotations

import argparse
import sys
from typing import Optional

from dotenv import load_dotenv

from config import MAX_NEW_PER_POLL, SEEN_DB_PATH
from filters import (
    FilterResult,
    filter_listing,
    miles_from_synapse,
    normalize_address,
)
from scrapers.base import Listing
from scrapers.craigslist import fetch_craigslist_listings
from scrapers.zillow import fetch_zillow_listings
from seen import SeenDB, reset_seen_db

load_dotenv()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="rental-scraper: poll Zillow + Craigslist, filter, dedup",
    )
    parser.add_argument(
        "--source",
        choices=["zillow", "craigslist", "all"],
        default="all",
        help="Which source(s) to fetch from",
    )
    parser.add_argument(
        "--cl-detail-limit",
        type=int,
        default=None,
        help="Cap Craigslist detail-page fetches (default: all)",
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Skip the filter/dedup pipeline; print raw scraper output (Day 1 mode)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't write to seen.db (lets you re-test the same listings)",
    )
    parser.add_argument(
        "--reset-seen",
        action="store_true",
        help="Delete seen.db before running",
    )
    args = parser.parse_args()

    if args.smoke_test:
        return _run_smoke_test(args)
    return _run_pipeline(args)


# ---------- pipeline mode (default) ----------


def _run_pipeline(args) -> int:
    if args.reset_seen:
        print(f"[seen] resetting {SEEN_DB_PATH}")
        reset_seen_db(SEEN_DB_PATH)

    raw = _fetch_all(args)
    print(f"\n--- pipeline: {len(raw)} raw listings ---")

    db = SeenDB(SEEN_DB_PATH)
    try:
        matches, stats = _filter_and_dedup(raw, db, dry_run=args.dry_run)
        _print_summary(stats, len(matches))
        _print_matches_and_mark_seen(matches, db, dry_run=args.dry_run)
        print(f"\n  seen.db now: {db.count()} entries")
    finally:
        db.close()
    return 0


def _fetch_all(args) -> list[Listing]:
    raw: list[Listing] = []
    if args.source in ("zillow", "all"):
        try:
            raw.extend(fetch_zillow_listings())
        except Exception as e:  # noqa: BLE001
            print(f"[zillow] FAILED: {e}", file=sys.stderr)
    if args.source in ("craigslist", "all"):
        try:
            raw.extend(fetch_craigslist_listings(detail_limit=args.cl_detail_limit))
        except Exception as e:  # noqa: BLE001
            print(f"[cl] FAILED: {e}", file=sys.stderr)
    return raw


def _filter_and_dedup(
    raw: list[Listing],
    db: SeenDB,
    dry_run: bool,
) -> tuple[list[tuple[Listing, FilterResult, str, str]], dict]:
    stats = {
        "raw": len(raw),
        "seen_skip": 0,
        "cross_dups": 0,
        "rejected": {},
    }
    matches: list[tuple[Listing, FilterResult, str, str]] = []

    for l in raw:
        if db.is_seen_by_id(l.id):
            stats["seen_skip"] += 1
            continue

        result = filter_listing(l)
        if not result.passes:
            stats["rejected"][result.reason] = stats["rejected"].get(result.reason, 0) + 1
            continue

        city_label = l.city or ""
        norm_addr = normalize_address(l.address)

        other = db.is_seen_by_address(norm_addr, city_label)
        if other:
            stats["cross_dups"] += 1
            print(f"  [dedup] {l.id} ({l.source}) matches earlier {other}")
            if not dry_run:
                db.mark_seen(l.id, l.source, norm_addr, city_label)
            continue

        matches.append((l, result, norm_addr, city_label))

    return matches, stats


def _print_summary(stats: dict, match_count: int) -> None:
    print("\n--- pipeline summary ---")
    print(f"  raw fetched:           {stats['raw']}")
    print(f"  already seen (by id):  {stats['seen_skip']}")
    print(f"  cross-source dups:     {stats['cross_dups']}")
    rejected_total = sum(stats["rejected"].values())
    print(f"  rejected by filters:   {rejected_total}")
    for reason, n in sorted(stats["rejected"].items(), key=lambda x: -x[1]):
        print(f"    {n:3d}x {reason}")
    capped = min(match_count, MAX_NEW_PER_POLL)
    deferred = max(0, match_count - MAX_NEW_PER_POLL)
    print(
        f"  matches:               {match_count} "
        f"(showing {capped}{f', deferring {deferred}' if deferred else ''})"
    )


def _print_matches_and_mark_seen(
    matches: list[tuple[Listing, FilterResult, str, str]],
    db: SeenDB,
    dry_run: bool,
) -> None:
    for i, (l, result, norm_addr, city_label) in enumerate(matches):
        if i >= MAX_NEW_PER_POLL:
            # Deferred — don't mark seen, will be picked up next poll.
            break
        flag_chips = [f"+{k}" for k, v in result.flags.items() if v]
        flag_str = " ".join(flag_chips) if flag_chips else "(none)"
        price_str = f"${l.price:,}/mo" if l.price else "$?"
        miles = miles_from_synapse(l.lat, l.lng)
        dist_str = f"{miles:.1f}mi" if miles is not None else "?mi"
        print(
            f"\n  [{l.source:10s}] {price_str} | {l.beds:g}BR/{l.baths:g}BA | {city_label} | {dist_str} from Synapse"
        )
        print(f"    {l.address}")
        print(f"    {l.url}")
        print(
            f"    flags: {flag_str}  |  photos: {len(l.image_urls)}  |  desc: {len(l.description)} chars"
        )
        if not dry_run:
            db.mark_seen(l.id, l.source, norm_addr, city_label)


# ---------- smoke-test mode (Day 1 behavior) ----------


def _run_smoke_test(args) -> int:
    rc = 0
    if args.source in ("craigslist", "all"):
        try:
            listings = fetch_craigslist_listings(detail_limit=args.cl_detail_limit or 5)
            print(f"\n=== Craigslist: {len(listings)} listings ===")
            for l in listings:
                _print_raw(l)
        except Exception as e:  # noqa: BLE001
            print(f"[cl] FAILED: {e}", file=sys.stderr)
            rc = 1
    if args.source in ("zillow", "all"):
        try:
            listings = fetch_zillow_listings()
            print(f"\n=== Zillow: {len(listings)} listings ===")
            for l in listings:
                _print_raw(l)
        except Exception as e:  # noqa: BLE001
            print(f"[zillow] FAILED: {e}", file=sys.stderr)
            rc = 1
    return rc


def _print_raw(l: Listing) -> None:
    price = f"${l.price:,}" if l.price else "$?"
    beds = f"{l.beds:g}BR" if l.beds else "?BR"
    baths = f"{l.baths:g}BA" if l.baths else "?BA"
    location = " · ".join(x for x in [l.address, l.city] if x) or "(no address)"
    print(f"  {price} | {beds}/{baths} | {location}")
    print(f"    {l.url}")
    print(f"    images: {len(l.image_urls)} | desc: {len(l.description)} chars")


if __name__ == "__main__":
    sys.exit(main())
