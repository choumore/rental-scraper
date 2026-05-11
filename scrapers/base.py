from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Listing:
    id: str
    source: str
    url: str
    address: str
    city: str
    price: int
    beds: float
    baths: float
    description: str
    image_urls: list[str] = field(default_factory=list)
    property_type: str = ""
    posted_at: str = ""
    lat: float | None = None
    lng: float | None = None
    sqft: int = 0
    available_date: str = ""  # ISO YYYY-MM-DD; empty if unparseable
