"""One-shot backfill: create Vision Board boards for every seen.db row that
doesn't already have one.

Strategy: re-runs the pipeline with an expanded Zillow window (doz=30) and
Craigslist's full peninsula scrape, so we get current data for as many old
listings as possible. For listings still in seen.db but no longer on the
sources, we skip — can't create a board without data.

    ./venv/bin/python backfill.py [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

import config
from filters import filter_listing, normalize_address
from scrapers.base import Listing
from scrapers.craigslist import fetch_craigslist_listings
from scrapers.zillow import fetch_zillow_details, fetch_zillow_listings
from seen import SeenDB
from vision_board import create_board_with_images, is_configured as vboard_configured

load_dotenv()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print what would be done; no inserts.")
    parser.add_argument("--doz", type=int, default=30, help="Expand Zillow days-on-zillow window for backfill (default 30)")
    parser.add_argument("--limit", type=int, default=None, help="Cap number of boards to create (for smoke-testing)")
    args = parser.parse_args()

    if not vboard_configured() and not args.dry_run:
        print("[backfill] SUPABASE_URL or SUPABASE_SERVICE_KEY missing — set in .env first", file=sys.stderr)
        return 1

    # Expand Zillow window for backfill
    original_doz = config.DAYS_ON_ZILLOW
    config.DAYS_ON_ZILLOW = args.doz
    print(f"[backfill] using doz={args.doz} (was {original_doz})")

    print("[backfill] fetching expanded scrape...")
    raw: list[Listing] = []
    try:
        raw.extend(fetch_zillow_listings())
    except Exception as e:
        print(f"[zillow] FAILED: {e}", file=sys.stderr)
    try:
        raw.extend(fetch_craigslist_listings(detail_limit=None))
    except Exception as e:
        print(f"[cl] FAILED: {e}", file=sys.stderr)
    print(f"[backfill] fetched {len(raw)} raw listings")

    db = SeenDB(config.SEEN_DB_PATH)
    try:
        # Enrich Zillow detail for everything (since we want full data for backfill)
        zillow_raw = [l for l in raw if l.source == "zillow"]
        fetch_zillow_details(zillow_raw)

        # Index raw by listing_id for quick lookup
        by_id: dict[str, Listing] = {l.id: l for l in raw}

        needs_backfill = db.list_all_without_board()
        print(f"[backfill] {len(needs_backfill)} seen.db rows lack a board")

        created = 0
        missing_data = 0
        for listing_id, source, norm_addr, city in needs_backfill:
            if args.limit is not None and created >= args.limit:
                print(f"[backfill] hit --limit {args.limit}, stopping")
                break
            listing = by_id.get(listing_id)
            if not listing:
                missing_data += 1
                continue

            # Validate it would still pass current filters (sanity check)
            result = filter_listing(listing)
            if not result.passes:
                print(f"[backfill] {listing_id}: would-be reject ({result.reason}); creating board anyway")

            if args.dry_run:
                print(f"[backfill] DRY: would create board for {listing_id}: {listing.address}")
                continue

            board_id = create_board_with_images(listing)
            if board_id:
                db.set_board_id(listing_id, board_id)
                created += 1

        print(f"[backfill] created {created} boards, skipped {missing_data} (listing no longer on source)")
        if args.dry_run:
            print("[backfill] dry-run complete; no DB writes")
    finally:
        db.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
