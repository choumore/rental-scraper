"""End-to-end refresh — GitHub Actions cron entrypoint.

Same pipeline as report.py, but:
  - Sends Telegram for each new match (not already in seen.db)
  - Marks seen on notification success (retries on next run if Telegram fails)
  - Renders HTML using the PRE-run classification so newly-notified listings
    show without a "seen" tag on this run's snapshot of the site
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

from config import MAX_NEW_PER_POLL, SEEN_DB_PATH
from notify import notify_new_listing
from report import _classify, _render
from scrapers.base import Listing
from scrapers.craigslist import fetch_craigslist_listings
from scrapers.zillow import fetch_zillow_details, fetch_zillow_listings
from seen import SeenDB
from vision_board import (
    create_board_with_images,
    is_configured as vboard_configured,
    mark_board_gone,
)

STALE_DAYS = 5  # listings unseen for this many days get marked Gone

load_dotenv()


def main() -> int:
    started = datetime.now(timezone.utc).isoformat()
    print(f"[cron] start {started}")

    raw: list[Listing] = []
    try:
        raw.extend(fetch_zillow_listings())
    except Exception as e:  # noqa: BLE001 — fail-soft per-source
        print(f"[zillow] FAILED: {e}", file=sys.stderr)
    try:
        raw.extend(fetch_craigslist_listings(detail_limit=None))
    except Exception as e:  # noqa: BLE001
        print(f"[cl] FAILED: {e}", file=sys.stderr)
    print(f"[cron] fetched {len(raw)} raw listings")

    db = SeenDB(SEEN_DB_PATH)
    try:
        kept_pre_enrich, _rej_pre = _classify(raw, db)
        listings_to_enrich = [tup[0] for tup in kept_pre_enrich]
        fetch_zillow_details(listings_to_enrich)

        kept_pre, rejected = _classify(raw, db)

        # Bump last_seen_at for every kept listing currently visible.
        # Listings that don't reappear for STALE_DAYS days will get marked Gone.
        for l, _r, _na, _cl, _seen in kept_pre:
            db.update_last_seen(l.id)

        new_subset = [
            (l, r, na, cl) for l, r, na, cl, seen in kept_pre if not seen
        ]
        print(f"[cron] {len(new_subset)} new matches (cap {MAX_NEW_PER_POLL})")

        vboard_on = vboard_configured()
        if not vboard_on:
            print("[cron] vision-board NOT configured; Telegram only")

        notified = 0
        attempted = 0
        boards_created = 0
        for listing, result, norm_addr, city_label in new_subset[:MAX_NEW_PER_POLL]:
            attempted += 1

            # Create board + upload images BEFORE notification so the message
            # can include the vision-board link (currently doesn't, but board_id
            # is durable proof we processed this listing).
            board_id: Optional[str] = None
            if vboard_on:
                board_id = create_board_with_images(listing)
                if board_id:
                    boards_created += 1

            ok = notify_new_listing(listing, result.flags, city_label)
            if ok:
                db.mark_seen(listing.id, listing.source, norm_addr, city_label)
                if board_id:
                    db.set_board_id(listing.id, board_id)
                notified += 1
                print(f"[cron] notified + marked seen: {listing.id}")
            else:
                # Notification failed. Don't mark seen (we'll retry next run).
                # If board was already created, save its ID so we don't dup next run.
                if board_id:
                    db.mark_seen(listing.id, listing.source, norm_addr, city_label)
                    db.set_board_id(listing.id, board_id)
                    print(
                        f"[cron] {listing.id}: board created but notify failed — marked seen so next run retries notify only"
                    )
                else:
                    print(f"[cron] notification failed for {listing.id} — will retry next run")
        print(f"[cron] notified {notified}/{attempted}, boards created {boards_created}")

        # Mark gone: listings with boards that haven't been seen in STALE_DAYS days
        if vboard_on:
            stale = db.list_stale_active(STALE_DAYS)
            if stale:
                print(f"[cron] marking {len(stale)} stale boards as Gone")
                for listing_id, board_id in stale:
                    if mark_board_gone(board_id):
                        db.mark_status(listing_id, "gone")
                        print(f"[cron] gone: {listing_id} (board {board_id[:8]}…)")
    finally:
        db.close()

    html_text = _render(kept_pre, rejected, raw_count=len(raw))

    with open("report.html", "w") as f:
        f.write(html_text)
    print("[cron] wrote report.html")

    os.makedirs("dist", exist_ok=True)
    with open("dist/index.html", "w") as f:
        f.write(html_text)
    print("[cron] wrote dist/index.html")

    print(f"[cron] done {datetime.now(timezone.utc).isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
