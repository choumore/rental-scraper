"""Telegram notifier for new rental matches.

Reads bot token + chat id from env (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).
HTML parse mode — supports <b>, <i>, <a>.
"""
from __future__ import annotations

import html
import os
import urllib.parse

import requests

from filters import miles_from_synapse
from scrapers.base import Listing

_TG_API = "https://api.telegram.org/bot{token}/sendMessage"


def _maps_url(addr: str, city: str) -> str:
    query = addr.strip()
    if city and city.lower() not in query.lower():
        query = f"{query}, {city.strip()}, CA"
    return "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(query)


def format_listing_message(
    listing: Listing, flags: dict, city_label: str
) -> str:
    src_pretty = (listing.source or "listing").title()
    lines: list[str] = [f"🏠 <b>New match — {html.escape(src_pretty)}</b>", ""]

    price_html = f"<b>${listing.price:,}/mo</b>" if listing.price else "<b>$?</b>"
    specs = [f"{listing.beds:g}BR", f"{listing.baths:g}BA"]
    if listing.sqft:
        specs.append(f"{listing.sqft:,} sqft")
    lines.append(f"{price_html} · {' · '.join(specs)}")

    miles = miles_from_synapse(listing.lat, listing.lng)
    loc = listing.address or "(no street address)"
    if miles is not None:
        loc = f"{loc} ({miles:.1f} mi from Synapse)"
    lines.append(f"📍 {html.escape(loc)}")

    if flags.get("late_move_in") and listing.available_date:
        lines.append(
            f"⚠ Available {html.escape(listing.available_date)} (after 7/1, may be negotiable)"
        )
    elif listing.available_date and not flags.get("unknown_availability"):
        lines.append(f"📅 Available {html.escape(listing.available_date)}")

    soft = [k for k in ("renovation", "backyard") if flags.get(k)]
    if soft:
        lines.append(f"✨ {', '.join(soft)}")

    lines.append("")
    lines.append(
        f'<a href="{html.escape(listing.url)}">View on {html.escape(src_pretty)} →</a>'
    )
    if listing.address:
        maps = _maps_url(listing.address, city_label)
        lines.append(f'<a href="{html.escape(maps)}">Open in Maps →</a>')

    return "\n".join(lines)


def send_telegram(message: str) -> bool:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        print("[notify] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set; skipping")
        return False

    try:
        r = requests.post(
            _TG_API.format(token=token),
            json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"[notify] HTTP request failed: {e}")
        return False

    if r.status_code != 200:
        print(f"[notify] Telegram HTTP {r.status_code}: {r.text[:240]}")
        return False
    return True


def notify_new_listing(listing: Listing, flags: dict, city_label: str) -> bool:
    return send_telegram(format_listing_message(listing, flags, city_label))
