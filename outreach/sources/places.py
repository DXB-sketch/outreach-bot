"""Google Places API (New) Text Search: much better local coverage, needs a key.

Cost note: requesting websiteUri / rating / userRatingCount puts each request on a
higher-priced SKU. Check current pricing and free monthly allowance before running
large searches. Each query here is one request returning up to 20 places per page.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import httpx

from . import dedupe_key, haversine_km, normalise_website

ENDPOINT = "https://places.googleapis.com/v1/places:searchText"
FIELDS = ",".join(
    "places." + f
    for f in (
        "id", "displayName", "formattedAddress", "location", "websiteUri",
        "nationalPhoneNumber", "rating", "userRatingCount", "primaryType",
        "types", "businessStatus",
    )
) + ",nextPageToken"

DEFAULT_QUERIES = [
    "plumber", "electrician", "builder", "landscaper", "roofer", "painter",
    "air conditioning", "pest control", "cleaning service", "mechanic",
    "dentist", "physiotherapist", "chiropractor", "veterinarian",
    "accountant", "lawyer", "real estate agency", "hairdresser", "beauty salon",
    "cafe", "restaurant", "accommodation", "winery", "florist", "dog groomer",
]


def search(api_key: str, text: str, lat: float, lon: float, radius_km: float, pages: int = 1) -> Iterator[dict]:
    body: dict = {
        "textQuery": text,
        "regionCode": "AU",
        "locationBias": {
            "circle": {
                "center": {"latitude": lat, "longitude": lon},
                "radius": min(radius_km * 1000, 50000.0),
            }
        },
    }
    headers = {"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": FIELDS}
    for _ in range(pages):
        resp = httpx.post(ENDPOINT, json=body, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        yield from data.get("places", [])
        token = data.get("nextPageToken")
        if not token:
            break
        body["pageToken"] = token
        time.sleep(2)


def to_business(place: dict, centre: tuple[float, float], query: str) -> dict:
    name = place.get("displayName", {}).get("text", "")
    loc = place.get("location", {})
    lat, lon = loc.get("latitude"), loc.get("longitude")
    website = normalise_website(place.get("websiteUri"))
    return {
        "source": "places",
        "source_id": place["id"],
        "name": name,
        "category": f"places={place.get('primaryType') or query}",
        "address": place.get("formattedAddress"),
        "lat": lat,
        "lon": lon,
        "distance_km": round(haversine_km(centre[0], centre[1], lat, lon), 1) if lat and lon else None,
        "website": website,
        "phone": place.get("nationalPhoneNumber"),
        "rating": place.get("rating"),
        "review_count": place.get("userRatingCount"),
        "tags": {"types": place.get("types", []), "businessStatus": place.get("businessStatus")},
        "dedupe_key": dedupe_key(name, website, lat, lon),
    }


def discover(api_key: str, area: str, lat: float, lon: float, radius_km: float,
             queries: list[str] | None = None, pages: int = 1) -> Iterator[dict]:
    for q in queries or DEFAULT_QUERIES:
        for place in search(api_key, f"{q} near {area}", lat, lon, radius_km, pages):
            biz = to_business(place, (lat, lon), q)
            if biz["name"] and (biz["distance_km"] is None or biz["distance_km"] <= radius_km * 1.5):
                yield biz
