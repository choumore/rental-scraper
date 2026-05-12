"""Render current pipeline output as a static HTML page.

Reads listings via the same fetch+filter pipeline as main.py (dry-run mode —
doesn't write to seen.db), then renders a single-file HTML report with
client-side sort + filter controls.

    ./venv/bin/python report.py [--open]
"""
from __future__ import annotations

import argparse
import html
import json
import subprocess
import sys
import urllib.parse
from datetime import datetime, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

PACIFIC_TZ = ZoneInfo("America/Los_Angeles")

from dotenv import load_dotenv

from config import (
    MAX_DISTANCE_MILES,
    MAX_PRICE,
    MIN_BATHS,
    MIN_BEDS,
    MOVE_BY_DATE,
    SEEN_DB_PATH,
)
from filters import (
    FilterResult,
    filter_listing,
    miles_from_synapse,
    normalize_address,
)
from scrapers.base import Listing
from scrapers.craigslist import fetch_craigslist_listings
from scrapers.zillow import fetch_zillow_details, fetch_zillow_listings
from seen import SeenDB

load_dotenv()


def main() -> int:
    parser = argparse.ArgumentParser(description="Render pipeline output to HTML")
    parser.add_argument("--out", default="report.html", help="Output HTML path")
    parser.add_argument("--open", action="store_true", help="Open in default browser")
    parser.add_argument("--cl-detail-limit", type=int, default=None)
    args = parser.parse_args()

    print("[report] fetching...")
    raw: list[Listing] = []
    try:
        raw.extend(fetch_zillow_listings())
    except Exception as e:  # noqa: BLE001
        print(f"[zillow] FAILED: {e}", file=sys.stderr)
    try:
        raw.extend(fetch_craigslist_listings(detail_limit=args.cl_detail_limit))
    except Exception as e:  # noqa: BLE001
        print(f"[cl] FAILED: {e}", file=sys.stderr)
    print(f"[report] {len(raw)} raw listings")

    db = SeenDB(SEEN_DB_PATH)
    try:
        # Pre-filter so we only pay the detail-actor for listings that passed
        # cheap checks (price/beds/baths/distance). Then re-classify with the
        # enriched data so availability/pets/description filters fire.
        kept_pre_enrich, _ = _classify(raw, db)
        fetch_zillow_details([tup[0] for tup in kept_pre_enrich])
        kept, rejected = _classify(raw, db)
    finally:
        db.close()

    html_text = _render(kept, rejected, raw_count=len(raw))
    with open(args.out, "w") as f:
        f.write(html_text)
    print(f"[report] wrote {args.out} ({len(kept)} matches, {len(rejected)} rejected)")

    if args.open:
        subprocess.run(["open", args.out], check=False)
    return 0


def _classify(
    raw: list[Listing], db: SeenDB
) -> tuple[list[tuple[Listing, FilterResult, str, str, bool]], list[tuple[Listing, str]]]:
    kept = []
    rejected = []
    for l in raw:
        result = filter_listing(l)
        if not result.passes:
            rejected.append((l, result.reason))
            continue

        city_label = l.city or ""
        norm_addr = normalize_address(l.address)
        already_seen = db.is_seen_by_id(l.id)
        cross_dup = db.is_seen_by_address(norm_addr, city_label) if not already_seen else None

        kept.append((l, result, norm_addr, city_label, bool(already_seen or cross_dup)))
    return kept, rejected


# ---------- freshness ----------


def _days_old(listing: Listing) -> int:
    """Days since the listing was posted. 999 for unknown."""
    pa = (listing.posted_at or "").strip()
    if not pa:
        return 999

    # ISO datetime (Craigslist e.g. "2026-05-09T12:34:56-0700")
    try:
        cleaned = pa
        # Python 3.9 needs colon-separated tz: "-0700" -> "-07:00"
        if len(cleaned) >= 5 and cleaned[-5] in ("+", "-") and cleaned[-3] != ":":
            cleaned = cleaned[:-2] + ":" + cleaned[-2:]
        cleaned = cleaned.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return max(0, int((now - dt).total_seconds() / 86400))
    except (ValueError, TypeError):
        pass

    # Numeric (Zillow daysOnZillow stored as string)
    try:
        return max(0, int(float(pa)))
    except (ValueError, TypeError):
        pass

    return 999


def _humanize_days(n: int) -> str:
    if n >= 999:
        return "posted date unknown"
    if n == 0:
        return "posted today"
    if n == 1:
        return "1 day ago"
    return f"{n} days ago"


def _google_maps_url(address: str, city: str) -> str:
    """Build a Google Maps search URL. Appends city + CA when the address
    doesn't already include the city, so Craigslist street-only addresses
    still resolve precisely."""
    query = address.strip()
    if city and city.lower() not in query.lower():
        query = f"{query}, {city.strip()}, CA"
    return "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(query)


# ---------- rendering ----------


_CSS = """
:root {
    --bg: #0e1116; --panel: #161b22; --panel2: #1c222b; --border: #30363d;
    --text: #e6edf3; --muted: #8b949e; --link: #58a6ff;
    --chip-bg: #1f6feb24; --chip-fg: #58a6ff; --chip-border: #1f6feb44;
    --pos: #3fb950; --warn: #d29922; --bad: #f85149;
    --zillow: #006aff; --craigslist: #551a8b;
}
* { box-sizing: border-box; }
body { background: var(--bg); color: var(--text); font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; padding: 24px; }
.wrap { max-width: 1400px; margin: 0 auto; }
header { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 24px; padding-bottom: 16px; border-bottom: 1px solid var(--border); }
header h1 { margin: 0; font-size: 22px; }
header .stamp { color: var(--muted); font-size: 12px; }
.summary-row { display: grid; grid-template-columns: minmax(220px, auto) 1fr; gap: 16px; margin-bottom: 24px; position: sticky; top: 0; z-index: 10; }
@media (max-width: 800px) { .summary-row { grid-template-columns: 1fr; } }
.stats { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 20px; padding: 14px 18px; background: var(--panel); border: 1px solid var(--border); border-radius: 8px; align-content: center; box-shadow: 0 4px 12px rgba(0,0,0,0.3); }
.stat { display: flex; align-items: baseline; gap: 6px; }
.stat .n { font-size: 18px; font-weight: 600; font-variant-numeric: tabular-nums; }
.stat .label { font-size: 11px; color: var(--muted); }
.stat.good .n { color: var(--pos); }
.stat.warn .n { color: var(--warn); }
.section-title { font-size: 13px; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; margin: 24px 0 12px; }
.controls { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.3); }
.controls-head { display: flex; align-items: center; gap: 12px; padding: 12px 16px; cursor: pointer; user-select: none; }
.controls-head .toggle { font-size: 12px; color: var(--muted); display: inline-flex; align-items: center; gap: 6px; }
.controls-head .toggle .caret { display: inline-block; transition: transform 0.15s; }
.controls.open .controls-head .toggle .caret { transform: rotate(90deg); }
.controls-head .sort-inline { margin-left: 4px; display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--muted); }
.controls-head .actions-row { margin-left: auto; display: flex; gap: 12px; align-items: center; }
.controls-body { display: none; padding: 0 16px 16px; border-top: 1px solid var(--border); }
.controls.open .controls-body { display: flex; flex-wrap: wrap; gap: 16px 24px; align-items: flex-end; padding-top: 16px; }
.control-group { display: flex; flex-direction: column; gap: 6px; }
.control-group label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; }
.control-group .row { display: flex; gap: 6px; align-items: center; }
.controls input, .controls select { background: var(--bg); color: var(--text); border: 1px solid var(--border); border-radius: 4px; padding: 6px 8px; font-size: 13px; font-family: inherit; }
.controls input { width: 84px; font-variant-numeric: tabular-nums; }
.controls input::placeholder { color: #4d5663; }
.controls input:focus, .controls select:focus { outline: none; border-color: var(--link); }
.controls .range-sep { color: var(--muted); }
.controls .cities { flex: 1 1 100%; }
.controls .city-list { display: flex; flex-wrap: nowrap; gap: 6px; overflow-x: auto; padding-bottom: 2px; scrollbar-width: thin; }
.controls .city-list::-webkit-scrollbar { height: 6px; }
.controls .city-list::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
.controls .city-check { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; cursor: pointer; padding: 5px 10px; background: var(--bg); border: 1px solid var(--border); border-radius: 999px; user-select: none; transition: border-color 0.1s, color 0.1s; white-space: nowrap; flex-shrink: 0; }
.controls .city-check input { width: auto; margin: 0; cursor: pointer; }
.controls .city-check.checked { border-color: var(--link); color: var(--link); background: #1f6feb14; }
.controls .match-count { font-size: 13px; color: var(--muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
.controls .reset-btn { background: var(--bg); color: var(--text); border: 1px solid var(--border); padding: 6px 12px; border-radius: 4px; cursor: pointer; font-size: 12px; font-family: inherit; }
.controls .reset-btn:hover { border-color: var(--link); color: var(--link); }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 16px; }
.card { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; display: flex; flex-direction: column; }
.card.seen { border-color: var(--warn); }
.card .photo { height: 200px; background: #000; background-size: cover; background-position: center; position: relative; }
.card .photo .badge { position: absolute; top: 8px; left: 8px; padding: 4px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; color: white; }
.card .photo .badge.zillow { background: var(--zillow); }
.card .photo .badge.craigslist { background: var(--craigslist); }
.card .photo .seen-tag { position: absolute; top: 8px; right: 8px; padding: 6px 12px; border-radius: 6px; font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; background: var(--warn); color: #1a1206; box-shadow: 0 2px 8px rgba(0,0,0,0.4); }
.card .photo .photo-count { position: absolute; bottom: 8px; right: 8px; padding: 2px 6px; border-radius: 4px; font-size: 11px; background: rgba(0,0,0,0.7); }
.card .photo .city-tag { position: absolute; bottom: 8px; left: 8px; padding: 4px 10px; border-radius: 4px; font-size: 11px; font-weight: 600; background: rgba(0,0,0,0.7); color: white; text-transform: capitalize; }
.card .body { padding: 14px; flex: 1; display: flex; flex-direction: column; }
.card .price-row { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 4px; }
.card .price { font-size: 20px; font-weight: 600; }
.card .view-link { font-size: 12px; color: var(--link); text-decoration: none; white-space: nowrap; }
.card .view-link:hover { text-decoration: underline; }
.card .specs-line { font-size: 13px; color: var(--muted); margin-bottom: 8px; display: flex; flex-wrap: wrap; gap: 6px; align-items: baseline; }
.card .specs-line .fresh { color: var(--pos); }
.card .specs-line .sep { color: #4d5663; }
.card .addr { font-size: 13px; margin-bottom: 8px; }
.card .addr a { color: var(--text); text-decoration: none; border-bottom: 1px dotted #4d5663; }
.card .addr a:hover { color: var(--link); border-bottom-color: var(--link); }
.card .flags { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 10px; }
.card .chip { background: var(--chip-bg); color: var(--chip-fg); border: 1px solid var(--chip-border); padding: 2px 8px; border-radius: 999px; font-size: 11px; }
.card .chip.distance { background: #3fb95024; color: var(--pos); border-color: #3fb95044; font-variant-numeric: tabular-nums; }
.card .chip.warn { background: #d2992224; color: var(--warn); border-color: #d2992244; }
.card .chip.unknown { background: #8b949e1f; color: var(--muted); border-color: #8b949e33; }
.card .avail { font-size: 12px; margin-bottom: 8px; font-variant-numeric: tabular-nums; }
.card .avail.late { color: var(--warn); }
.card .avail.ideal { color: var(--pos); }
.card .desc { font-size: 12px; color: var(--muted); margin-bottom: 12px; flex: 1; max-height: 80px; overflow: hidden; position: relative; }
.card .desc.empty { font-style: italic; color: #555; }
.card .desc::after { content: ""; position: absolute; bottom: 0; left: 0; right: 0; height: 24px; background: linear-gradient(transparent, var(--panel)); pointer-events: none; }
.rejected-list { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 0; overflow: hidden; }
.rejected-list details summary { cursor: pointer; padding: 12px 16px; font-size: 13px; color: var(--muted); border-bottom: 1px solid transparent; }
.rejected-list details[open] summary { border-bottom-color: var(--border); }
.rejected-list table { width: 100%; border-collapse: collapse; font-size: 12px; }
.rejected-list td { padding: 8px 12px; border-bottom: 1px solid var(--border); color: var(--muted); }
.rejected-list td:first-child { color: var(--bad); font-family: ui-monospace, "SF Mono", monospace; white-space: nowrap; }
.rejected-list td a { color: var(--link); text-decoration: none; }
.empty-state { padding: 32px; text-align: center; color: var(--muted); background: var(--panel); border: 1px solid var(--border); border-radius: 8px; }
"""


_JS = """
(function() {
  const cards = Array.from(document.querySelectorAll('.card[data-price]'));
  const grid = document.querySelector('.grid');
  if (!grid || cards.length === 0) return;

  const $ = id => document.getElementById(id);
  const sortEl = $('sort-mode');
  const priceMin = $('price-min'), priceMax = $('price-max');
  const sqftMin = $('sqft-min'), sqftMax = $('sqft-max');
  const distMin = $('dist-min'), distMax = $('dist-max');
  const cityChecks = Array.from(document.querySelectorAll('.city-cb'));
  const matchCountEl = $('match-count');
  const resetBtn = $('reset-btn');

  const num = (el, key) => {
    const v = parseFloat(el.dataset[key]);
    return isNaN(v) ? 0 : v;
  };
  const getInput = (el, fallback) => {
    if (!el.value) return fallback;
    const n = parseFloat(el.value);
    return isNaN(n) ? fallback : n;
  };

  function applyAll() {
    const pMin = getInput(priceMin, -Infinity);
    const pMax = getInput(priceMax, Infinity);
    const sMin = getInput(sqftMin, -Infinity);
    const sMax = getInput(sqftMax, Infinity);
    const dMin = getInput(distMin, -Infinity);
    const dMax = getInput(distMax, Infinity);
    const sqftMinSet = sqftMin.value !== '';
    const sqftMaxSet = sqftMax.value !== '';

    const selected = new Set(cityChecks.filter(cb => cb.checked).map(cb => cb.value));
    const allCitiesOff = selected.size === 0;

    // Visual update for city pills
    cityChecks.forEach(cb => {
      cb.closest('.city-check').classList.toggle('checked', cb.checked);
    });

    let visible = 0;
    cards.forEach(c => {
      const price = num(c, 'price');
      const sqft = num(c, 'sqft');
      const dist = num(c, 'distance');
      const city = (c.dataset.city || '').toLowerCase();

      let passes = true;
      if (price < pMin || price > pMax) passes = false;
      if (sqft > 0) {
        if (sqft < sMin || sqft > sMax) passes = false;
      } else if (sqftMinSet || sqftMaxSet) {
        // Unknown sqft + user is filtering by sqft → exclude
        passes = false;
      }
      if (dist < dMin || dist > dMax) passes = false;
      if (allCitiesOff) {
        passes = false;
      } else if (city && !selected.has(city)) {
        passes = false;
      }

      c.style.display = passes ? '' : 'none';
      if (passes) visible++;
    });

    // Sort visible + hidden cards together (so toggling filters preserves order)
    const mode = sortEl.value;
    const cmp = pickComparator(mode);
    if (cmp) {
      const sorted = cards.slice().sort(cmp);
      sorted.forEach(c => grid.appendChild(c));
    }

    matchCountEl.textContent = `Showing ${visible} of ${cards.length}`;
  }

  function pickComparator(mode) {
    switch (mode) {
      case 'price-asc':  return (a,b) => num(a,'price') - num(b,'price');
      case 'price-desc': return (a,b) => num(b,'price') - num(a,'price');
      case 'sqft-asc':   return (a,b) => sqftCmp(a, b, true);
      case 'sqft-desc':  return (a,b) => sqftCmp(a, b, false);
      case 'distance-asc':  return (a,b) => num(a,'distance') - num(b,'distance');
      case 'distance-desc': return (a,b) => num(b,'distance') - num(a,'distance');
      case 'fresh':      return (a,b) => num(a,'days') - num(b,'days');
      case 'stale':      return (a,b) => num(b,'days') - num(a,'days');
      default: return null;
    }
  }

  function sqftCmp(a, b, asc) {
    const sa = num(a, 'sqft'), sb = num(b, 'sqft');
    // Push unknown (0) to the end regardless of direction
    if (sa === 0 && sb === 0) return 0;
    if (sa === 0) return 1;
    if (sb === 0) return -1;
    return asc ? sa - sb : sb - sa;
  }

  function reset() {
    [priceMin, priceMax, sqftMin, sqftMax, distMin, distMax].forEach(el => el.value = '');
    cityChecks.forEach(cb => cb.checked = true);
    sortEl.value = 'fresh';
    applyAll();
  }

  [priceMin, priceMax, sqftMin, sqftMax, distMin, distMax].forEach(el =>
    el.addEventListener('input', applyAll));
  sortEl.addEventListener('change', applyAll);
  cityChecks.forEach(cb => cb.addEventListener('change', applyAll));
  resetBtn.addEventListener('click', e => { e.stopPropagation(); reset(); });

  // Collapsable filter panel — toggle on header click, but don't toggle when
  // the user is interacting with the sort dropdown or reset button.
  const controls = document.getElementById('controls');
  const head = document.getElementById('controls-toggle');
  if (head && controls) {
    head.addEventListener('click', e => {
      if (e.target.closest('select, button, input')) return;
      controls.classList.toggle('open');
    });
    sortEl.addEventListener('click', e => e.stopPropagation());
  }

  applyAll();
})();
"""


def _render(
    kept: list[tuple[Listing, FilterResult, str, str, bool]],
    rejected: list[tuple[Listing, str]],
    raw_count: int,
) -> str:
    stamp = datetime.now(PACIFIC_TZ).strftime("%Y-%m-%d %H:%M %Z")
    new_count = sum(1 for *_, already_seen in kept if not already_seen)
    seen_count = sum(1 for *_, already_seen in kept if already_seen)

    # Unique cities in insertion order (city.title() for display)
    cities_seen: list[str] = []
    seen_set: set[str] = set()
    for l, _r, _na, city_label, _seen in kept:
        c = (city_label or l.city or "").strip()
        if not c:
            continue
        key = c.lower()
        if key in seen_set:
            continue
        seen_set.add(key)
        cities_seen.append(c.title())
    cities_seen.sort()

    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        "<title>rental-scraper report</title>",
        f"<style>{_CSS}</style></head><body><div class='wrap'>",
        "<header>",
        "<h1>🏠 rental-scraper report</h1>",
        f"<span class='stamp'>generated {html.escape(stamp)} · ≤${MAX_PRICE:,}/mo · {MIN_BEDS}+BR / {MIN_BATHS}+BA · SFH · within {MAX_DISTANCE_MILES:g}mi of Synapse · move-in ≤ {MOVE_BY_DATE} (+grace)</span>",
        "</header>",
    ]

    stats_html = (
        "<div class='stats'>"
        f"<div class='stat'><span class='n'>{raw_count}</span><span class='label'>raw</span></div>"
        f"<div class='stat good'><span class='n'>{new_count}</span><span class='label'>new</span></div>"
        f"<div class='stat warn'><span class='n'>{seen_count}</span><span class='label'>seen</span></div>"
        f"<div class='stat'><span class='n'>{len(rejected)}</span><span class='label'>rejected</span></div>"
        "</div>"
    )
    controls_html = _render_controls(cities_seen) if kept else ""
    if kept:
        parts.append(f"<div class='summary-row'>{stats_html}{controls_html}</div>")
    else:
        parts.append(stats_html)

    parts.append("<div class='section-title'>Matches</div>")
    if not kept:
        parts.append("<div class='empty-state'>No listings passed the filters.</div>")
    else:
        parts.append("<div class='grid'>")
        for listing, result, _norm_addr, city_label, already_seen in kept:
            parts.append(_render_card(listing, result, city_label, already_seen))
        parts.append("</div>")

    if rejected:
        parts.append("<div class='section-title'>Rejected</div>")
        parts.append("<div class='rejected-list'><details><summary>")
        parts.append(f"{len(rejected)} listings filtered out (click to expand)")
        parts.append("</summary><table>")
        for listing, reason in rejected:
            label = listing.address or listing.url
            parts.append(
                f"<tr><td>{html.escape(reason)}</td>"
                f"<td>[{html.escape(listing.source)}]</td>"
                f"<td>{html.escape(label)}</td>"
                f"<td><a href='{html.escape(listing.url)}' target='_blank'>open</a></td></tr>"
            )
        parts.append("</table></details></div>")

    parts.append(f"<script>{_JS}</script>")
    parts.append("</div></body></html>")
    return "\n".join(parts)


def _render_controls(cities: list[str]) -> str:
    city_chips = "".join(
        f"<label class='city-check checked'>"
        f"<input type='checkbox' class='city-cb' value='{html.escape(c.lower())}' checked>"
        f"{html.escape(c)}"
        f"</label>"
        for c in cities
    )
    return f"""<div class='controls' id='controls'>
      <div class='controls-head' id='controls-toggle'>
        <span class='toggle'><span class='caret'>▶</span> Filters</span>
        <div class='sort-inline'>
          <span>Sort:</span>
          <select id='sort-mode'>
            <option value='fresh'>Newest first</option>
            <option value='stale'>Oldest first</option>
            <option value='price-asc'>Price (low → high)</option>
            <option value='price-desc'>Price (high → low)</option>
            <option value='sqft-desc'>Sqft (large → small)</option>
            <option value='sqft-asc'>Sqft (small → large)</option>
            <option value='distance-asc'>Distance (near → far)</option>
            <option value='distance-desc'>Distance (far → near)</option>
          </select>
        </div>
        <div class='actions-row'>
          <span class='match-count' id='match-count'></span>
          <button class='reset-btn' id='reset-btn' type='button'>Reset</button>
        </div>
      </div>
      <div class='controls-body'>
        <div class='control-group'>
          <label>Price ($/mo)</label>
          <div class='row'>
            <input type='number' id='price-min' placeholder='min'>
            <span class='range-sep'>–</span>
            <input type='number' id='price-max' placeholder='max'>
          </div>
        </div>
        <div class='control-group'>
          <label>Sqft</label>
          <div class='row'>
            <input type='number' id='sqft-min' placeholder='min'>
            <span class='range-sep'>–</span>
            <input type='number' id='sqft-max' placeholder='max'>
          </div>
        </div>
        <div class='control-group'>
          <label>Distance (mi)</label>
          <div class='row'>
            <input type='number' step='0.1' id='dist-min' placeholder='min'>
            <span class='range-sep'>–</span>
            <input type='number' step='0.1' id='dist-max' placeholder='max'>
          </div>
        </div>
        <div class='control-group cities'>
          <label>Cities</label>
          <div class='city-list'>{city_chips}</div>
        </div>
      </div>
    </div>"""


def _render_card(
    listing: Listing,
    result: FilterResult,
    city_label: str,
    already_seen: bool,
) -> str:
    photo = listing.image_urls[0] if listing.image_urls else ""
    photo_style = f"background-image: url('{html.escape(photo)}');" if photo else ""
    photo_count_badge = (
        f"<div class='photo-count'>📷 {len(listing.image_urls)}</div>"
        if listing.image_urls
        else ""
    )
    seen_tag = "<div class='seen-tag'>SEEN</div>" if already_seen else ""

    city_display = (city_label or listing.city or "").strip()
    city_tag = (
        f"<div class='city-tag'>{html.escape(city_display.title())}</div>"
        if city_display
        else ""
    )

    # Skip the unknown_availability chip — surface the unknown state by hiding
    # the availability row entirely. Only render late_move_in + soft flags.
    flag_chips_parts = []
    for k, v in result.flags.items():
        if not v or k == "unknown_availability":
            continue
        if k == "late_move_in":
            flag_chips_parts.append("<span class='chip warn'>⚠ late move-in</span>")
        else:
            flag_chips_parts.append(f"<span class='chip'>+{html.escape(k)}</span>")
    flag_chips = "".join(flag_chips_parts)

    desc = (listing.description or "").strip()
    if desc:
        desc_html = html.escape(desc[:300]) + ("…" if len(desc) > 300 else "")
        desc_class = "desc"
    else:
        desc_html = "(no description from search result)"
        desc_class = "desc empty"

    price_str = f"${listing.price:,}/mo" if listing.price else "$?"
    specs_parts = [f"{listing.beds:g} bed", f"{listing.baths:g} bath"]
    if listing.sqft:
        specs_parts.append(f"{listing.sqft:,} sqft")
    pets = (listing.pets_allowed or "").strip()
    if pets.lower() == "no":
        specs_parts.append("🚫 no pets")
    elif pets:
        specs_parts.append(f"🐾 {pets}")

    days = _days_old(listing)
    freshness_text = _humanize_days(days)
    fresh_class = "fresh" if 0 <= days <= 2 else ""
    specs_html_parts: list[str] = []
    for i, part in enumerate(specs_parts):
        if i > 0:
            specs_html_parts.append("<span class='sep'>·</span>")
        specs_html_parts.append(f"<span>{html.escape(part)}</span>")
    if freshness_text and days < 999:
        specs_html_parts.append("<span class='sep'>·</span>")
        specs_html_parts.append(
            f"<span class='{fresh_class}'>{html.escape(freshness_text)}</span>"
        )
    specs_line = "".join(specs_html_parts)

    addr_text = listing.address or ""
    if addr_text:
        maps_url = _google_maps_url(addr_text, city_display)
        addr_html = (
            f"<a href='{html.escape(maps_url)}' target='_blank' rel='noopener' "
            f"title='Open in Google Maps'>{html.escape(addr_text)}</a>"
        )
    else:
        addr_html = "(no street address)"
    url_safe = html.escape(listing.url)
    source_safe = html.escape(listing.source)
    miles = miles_from_synapse(listing.lat, listing.lng)
    miles_value = f"{miles:.2f}" if miles is not None else ""
    dist_chip = (
        f"<span class='chip distance'>{miles:.1f} mi from Synapse</span>"
        if miles is not None
        else ""
    )

    avail_html = _render_availability(listing.available_date, result.flags)

    data_attrs = (
        f"data-price='{int(listing.price or 0)}' "
        f"data-sqft='{int(listing.sqft or 0)}' "
        f"data-distance='{miles_value}' "
        f"data-days='{days}' "
        f"data-city='{html.escape(city_display.lower())}'"
    )

    return (
        f"<div class='card{' seen' if already_seen else ''}' {data_attrs}>"
        f"<div class='photo' style=\"{photo_style}\">"
        f"<div class='badge {source_safe}'>{source_safe}</div>"
        f"{city_tag}"
        f"{seen_tag}{photo_count_badge}"
        f"</div>"
        f"<div class='body'>"
        f"<div class='price-row'>"
        f"<span class='price'>{price_str}</span>"
        f"<a class='view-link' href='{url_safe}' target='_blank' rel='noopener'>view original →</a>"
        f"</div>"
        f"<div class='specs-line'>{specs_line}</div>"
        f"<div class='addr'>{addr_html}</div>"
        f"{avail_html}"
        f"<div class='flags'>{dist_chip}{flag_chips}</div>"
        f"<div class='{desc_class}'>{desc_html}</div>"
        f"</div></div>"
    )


def _render_availability(available_iso: str, flags: dict) -> str:
    # Hide entirely when availability is unknown
    if flags.get("unknown_availability"):
        return ""
    if not available_iso:
        return ""
    label = f"available {html.escape(available_iso)}"
    css = "avail late" if flags.get("late_move_in") else "avail ideal"
    return f"<div class='{css}'>📅 {label}</div>"


def _consume(seq: Iterable) -> list:
    return list(seq)


if __name__ == "__main__":
    sys.exit(main())
