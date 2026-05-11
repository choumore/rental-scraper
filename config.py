from __future__ import annotations

MAX_PRICE = 12000
MIN_PRICE = 6000  # filters out room-rentals miscategorized as whole-house listings
MIN_BEDS = 3
MIN_BATHS = 2

# Geographic filter: within ~15 min drive of Synapse School (3375 Edison Way, Menlo Park).
# Haversine straight-line distance — approximates drive time, doesn't account for
# road network or traffic. Generous to catch listings near 101/280 corridors;
# manual review filters false-positives over the hills (Portola Valley, Woodside).
SYNAPSE_COORDS = (37.4764, -122.1995)
MAX_DISTANCE_MILES = 6.0

# Zillow's search endpoint requires a rectangular mapBounds. One box around
# Synapse covering the full 6-mile circle — actual radius filter happens
# per-listing in filters.py.
SEARCH_BOUNDS = {"west": -122.31, "east": -122.09, "south": 37.39, "north": 37.56}

APIFY_ACTOR = "maxcopell/zillow-scraper"
# Zillow's "doz=1" only matches listings posted TODAY. Use 2 to catch yesterday too.
# doz=7 = last week (good for backfill / first run). doz=2 = last ~48h (steady state).
DAYS_ON_ZILLOW = 2

CL_SUBDOMAIN = "sfbay"
CL_REGION = "pen"
CL_REQUEST_DELAY_RANGE = (5, 15)

POLL_INTERVAL_MINUTES = 240

# Move-in deadline — hard cutoff. Listings available after MOVE_BY_DATE + GRACE_DAYS
# are rejected. Listings between MOVE_BY_DATE and the grace window get a
# `late_move_in` flag (potentially negotiable with owner). Unparseable dates
# pass with an `unknown_availability` flag.
MOVE_BY_DATE = "2026-07-01"
GRACE_DAYS = 31  # ≈ one calendar month — captures 8/1 inclusively (user's example)

# Hard exclude — any of these in title+description rejects the listing
EXCLUDE_KEYWORDS = ["townhouse", "town house", "townhome", "condo", "apartment", "no pets"]

# Soft signals — surfaced as flags on the board, not used for filtering
RENOVATION_KEYWORDS = ["renovat", "remodel", "updated", "new kitchen", "new bath"]
BACKYARD_KEYWORDS = ["backyard", "back yard", "yard", "garden"]

# Cap on new listings created per poll cycle — prevents notification floods on first run
MAX_NEW_PER_POLL = 10

# Path to the dedup SQLite file
SEEN_DB_PATH = "seen.db"
