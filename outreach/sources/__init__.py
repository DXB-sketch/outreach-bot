"""Lead sources. Each source yields plain dicts ready for db.upsert_business."""

from __future__ import annotations

import math
import re
from urllib.parse import urlparse

import httpx


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def normalise_website(url: str | None) -> str | None:
    if not url:
        return None
    # OSM sometimes stores several values: "https://a.com.au;https://facebook.com/a"
    parts = [u for u in re.split(r"[;,\s]+", url.strip()) if u]
    if not parts:
        return None
    url = next((u for u in parts if is_own_site(u if "://" in u else "https://" + u)), parts[0])
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


# Website hosts that are not the business's own site.
NOT_OWN_SITE = (
    "facebook.com", "fb.com", "instagram.com", "linktr.ee", "google.com", "goo.gl", "g.page",
    "business.site", "yellowpages.com.au", "hipages.com.au", "oneflare.com.au", "truelocal.com.au",
    "localsearch.com.au", "startlocal.com.au", "hotfrog.com.au", "yelp.com", "yelp.com.au",
    "wordofmouth.com.au", "airtasker.com", "gumtree.com.au", "linkedin.com", "youtube.com",
    "tiktok.com", "x.com", "twitter.com", "tripadvisor.com", "tripadvisor.com.au", "booking.com",
    "airbnb.com", "airbnb.com.au", "abr.business.gov.au", "wikipedia.org", "seek.com.au",
    "healthengine.com.au", "hotdoc.com.au", "realestate.com.au", "domain.com.au", "bing.com",
    "duckduckgo.com", "wa.me", "m.me",
)


def is_own_site(url: str | None) -> bool:
    if not url:
        return False
    host = (urlparse(url).hostname or "").lower()
    return not any(host == d or host.endswith("." + d) for d in NOT_OWN_SITE)


def dedupe_key(name: str, website: str | None, lat: float | None, lon: float | None) -> str:
    if website and is_own_site(website):
        host = (urlparse(website).hostname or "").lower().removeprefix("www.")
        if host:
            return "web:" + host
    slug = re.sub(r"[^a-z0-9]", "", name.lower())
    if lat is not None and lon is not None:
        return f"name:{slug}@{lat:.2f},{lon:.2f}"
    return "name:" + slug


def geocode(area: str, user_agent: str) -> tuple[float, float]:
    """Resolve a place name to coordinates with OpenStreetMap Nominatim."""
    resp = httpx.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": area, "format": "json", "limit": 1, "countrycodes": "au"},
        headers={"User-Agent": user_agent},
        timeout=30,
    )
    resp.raise_for_status()
    results = resp.json()
    if not results:
        raise ValueError(f"Could not geocode area: {area!r}")
    return float(results[0]["lat"]), float(results[0]["lon"])
