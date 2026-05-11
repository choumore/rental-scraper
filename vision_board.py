"""Vision Board (Supabase) integration.

Creates one board per rental listing. Uploads all images to storage. Inserts
image rows. Supports marking a board as "Gone" when the listing falls off
the sources.

Configured via env (read lazily so load_dotenv can run after import):
    SUPABASE_URL
    SUPABASE_SERVICE_KEY  (service-role JWT — bypasses RLS)
    VISION_BOARD_OWNER_ID  (defaults to choumore's auth uid)
    VISION_BOARD_CATEGORY_ID  (defaults to the existing 'Rental' category)
"""
from __future__ import annotations

import os
from typing import Optional

import requests

from filters import miles_from_synapse
from scrapers.base import Listing

BUCKET = "vision-board-images"
GONE_MARKER = " 🔴 GONE"
DEFAULT_OWNER_ID = "127dc33f-fcd8-4801-980d-f74e8aea1b1c"
DEFAULT_CATEGORY_ID = "19cbda88-aa0c-4917-852f-df7e30a5daca"


def _supa_url() -> str:
    return (os.environ.get("SUPABASE_URL") or "").rstrip("/")


def _service_key() -> str:
    return os.environ.get("SUPABASE_SERVICE_KEY") or ""


def _owner_id() -> str:
    return os.environ.get("VISION_BOARD_OWNER_ID") or DEFAULT_OWNER_ID


def _category_id() -> str:
    return os.environ.get("VISION_BOARD_CATEGORY_ID") or DEFAULT_CATEGORY_ID


def is_configured() -> bool:
    return bool(_supa_url() and _service_key())


def _api_headers(extra: Optional[dict] = None) -> dict:
    key = _service_key()
    h = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


# ---------- title + description ----------


def title_for(listing: Listing) -> str:
    """Address minus state/zip — '1785 Holly Ave, Menlo Park'."""
    addr = (listing.address or "").strip()
    if not addr:
        return f"{listing.source} {listing.id}"
    parts = [p.strip() for p in addr.split(",")]
    if len(parts) >= 2:
        return f"{parts[0]}, {parts[1]}"
    return addr


def description_for(listing: Listing) -> str:
    miles = miles_from_synapse(listing.lat, listing.lng)
    lines: list[str] = []

    price = f"${listing.price:,}/mo" if listing.price else "$?"
    specs = [f"{listing.beds:g} bed", f"{listing.baths:g} bath"]
    if listing.sqft:
        specs.append(f"{listing.sqft:,} sqft")
    lines.append(f"{price} · {' · '.join(specs)}")
    lines.append("")

    addr = listing.address or "(no address)"
    if miles is not None:
        addr += f" ({miles:.1f} mi from Synapse)"
    lines.append(f"📍 {addr}")

    if listing.available_date:
        lines.append(f"📅 Available {listing.available_date}")

    pets = (listing.pets_allowed or "").strip()
    if pets.lower() == "no":
        lines.append("🚫 No pets allowed")
    elif pets:
        lines.append(f"🐾 Pets: {pets}")

    lines.append("")
    lines.append(f"Source: {listing.source.title()}")
    lines.append(f"View original: {listing.url}")

    if listing.description:
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append(listing.description[:4000])

    return "\n".join(lines)


# ---------- board CRUD ----------


def create_board(listing: Listing) -> Optional[str]:
    """Insert a board for this listing. Returns board UUID on success."""
    if not is_configured():
        print("[vboard] SUPABASE_URL or SUPABASE_SERVICE_KEY missing — skipping")
        return None

    payload = {
        "owner_id": _owner_id(),
        "category_id": _category_id(),
        "title": title_for(listing),
        "description": description_for(listing),
        "is_public": True,
    }
    try:
        r = requests.post(
            f"{_supa_url()}/rest/v1/boards",
            headers=_api_headers({"Prefer": "return=representation"}),
            json=payload,
            timeout=30,
        )
    except requests.RequestException as e:
        print(f"[vboard] board create network error: {e}")
        return None

    if r.status_code not in (200, 201):
        print(f"[vboard] board create HTTP {r.status_code}: {r.text[:200]}")
        return None
    data = r.json()
    if not data:
        print("[vboard] board create returned empty body")
        return None
    return data[0].get("id")


def mark_board_gone(board_id: str) -> bool:
    """Append GONE_MARKER to the title (idempotent — won't append twice)."""
    if not is_configured():
        return False
    try:
        r = requests.get(
            f"{_supa_url()}/rest/v1/boards?id=eq.{board_id}&select=title",
            headers=_api_headers(),
            timeout=15,
        )
        if r.status_code != 200:
            print(f"[vboard] gone: fetch title failed {r.status_code}")
            return False
        rows = r.json()
        if not rows:
            print(f"[vboard] gone: board {board_id} not found")
            return False
        title = rows[0].get("title", "") or ""
    except requests.RequestException as e:
        print(f"[vboard] gone: fetch error: {e}")
        return False

    if GONE_MARKER.strip() in title:
        return True

    new_title = title.rstrip() + GONE_MARKER
    try:
        r = requests.patch(
            f"{_supa_url()}/rest/v1/boards?id=eq.{board_id}",
            headers=_api_headers(),
            json={"title": new_title},
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"[vboard] gone: patch error: {e}")
        return False
    if r.status_code not in (200, 204):
        print(f"[vboard] gone: patch HTTP {r.status_code}: {r.text[:200]}")
        return False
    return True


# ---------- image upload ----------


def _content_type_for(url: str) -> str:
    u = url.lower().split("?")[0]
    if u.endswith(".png"):
        return "image/png"
    if u.endswith(".webp"):
        return "image/webp"
    if u.endswith(".gif"):
        return "image/gif"
    return "image/jpeg"


def _upload_one(
    board_id: str, listing_id: str, image_url: str, sort_order: int
) -> bool:
    try:
        resp = requests.get(image_url, timeout=30)
    except requests.RequestException as e:
        print(f"[vboard] img {sort_order} download failed: {e}")
        return False
    if resp.status_code != 200:
        print(f"[vboard] img {sort_order} HTTP {resp.status_code}: {image_url[:80]}")
        return False
    img_bytes = resp.content
    content_type = _content_type_for(image_url)
    ext = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
    }.get(content_type, "jpg")
    storage_path = f"{_owner_id()}/rentals/{listing_id}/{sort_order:03d}.{ext}"

    upload_url = f"{_supa_url()}/storage/v1/object/{BUCKET}/{storage_path}"
    key = _service_key()
    headers = {
        "Authorization": f"Bearer {key}",
        "apikey": key,
        "Content-Type": content_type,
        "x-upsert": "true",
    }
    try:
        r = requests.post(upload_url, headers=headers, data=img_bytes, timeout=60)
    except requests.RequestException as e:
        print(f"[vboard] img {sort_order} storage upload error: {e}")
        return False
    if r.status_code not in (200, 201):
        print(f"[vboard] img {sort_order} storage HTTP {r.status_code}: {r.text[:200]}")
        return False

    img_row = {
        "board_id": board_id,
        "storage_path": storage_path,
        "sort_order": sort_order,
    }
    try:
        r = requests.post(
            f"{_supa_url()}/rest/v1/images",
            headers=_api_headers(),
            json=img_row,
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"[vboard] img {sort_order} row insert error: {e}")
        return False
    if r.status_code not in (200, 201):
        print(f"[vboard] img {sort_order} row HTTP {r.status_code}: {r.text[:200]}")
        return False
    return True


def upload_all_images(board_id: str, listing: Listing) -> int:
    """Upload every image from the listing. Returns successful count."""
    succeeded = 0
    for i, url in enumerate(listing.image_urls):
        if _upload_one(board_id, listing.id, url, i):
            succeeded += 1
    return succeeded


def create_board_with_images(listing: Listing) -> Optional[str]:
    """One-shot: create board + upload all images. Returns board_id, or None
    on board-create failure. Image upload failures are logged but don't abort —
    partial galleries are OK."""
    board_id = create_board(listing)
    if not board_id:
        return None
    uploaded = upload_all_images(board_id, listing)
    print(
        f"[vboard] board {board_id[:8]}… created · {uploaded}/{len(listing.image_urls)} images uploaded"
    )
    return board_id
