"""OpenStreetMap (Overpass API): free, no key, patchy coverage in rural areas."""

from __future__ import annotations

import time
from collections.abc import Iterator

import httpx

from . import dedupe_key, haversine_km, normalise_website

MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

AMENITY = "restaurant|cafe|dentist|doctors|clinic|veterinary|car_wash|childcare|driving_school|pub|bar|fast_food"
TOURISM = "hotel|motel|guest_house|caravan_site|camp_site|chalet|apartment|attraction"
LEISURE = "fitness_centre|sports_centre|horse_riding|golf_course|dance"

# Tag keys checked (in order) to name a business's category.
CATEGORY_KEYS = ("craft", "healthcare", "office", "shop", "amenity", "tourism", "leisure")


def build_query(lat: float, lon: float, radius_m: int) -> str:
    around = f"(around:{radius_m},{lat},{lon})"
    return f"""
[out:json][timeout:120];
(
  nwr{around}["name"]["craft"];
  nwr{around}["name"]["healthcare"];
  nwr{around}["name"]["office"]["office"!~"^(government|diplomatic|religion)$"];
  nwr{around}["name"]["shop"]["shop"!="vacant"];
  nwr{around}["name"]["amenity"~"^({AMENITY})$"];
  nwr{around}["name"]["tourism"~"^({TOURISM})$"];
  nwr{around}["name"]["leisure"~"^({LEISURE})$"];
);
out center tags;
"""


def fetch(lat: float, lon: float, radius_km: float, user_agent: str) -> list[dict]:
    query = build_query(lat, lon, int(radius_km * 1000))
    last_error: Exception | None = None
    for url in MIRRORS:
        try:
            resp = httpx.post(url, data={"data": query}, headers={"User-Agent": user_agent}, timeout=180)
            resp.raise_for_status()
            return resp.json().get("elements", [])
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            time.sleep(2)
    raise RuntimeError(f"All Overpass mirrors failed: {last_error}")


def to_business(el: dict, centre: tuple[float, float]) -> dict | None:
    tags = el.get("tags", {})
    name = tags.get("name")
    if not name:
        return None
    lat = el.get("lat") or el.get("center", {}).get("lat")
    lon = el.get("lon") or el.get("center", {}).get("lon")
    category = next((f"{k}={tags[k]}" for k in CATEGORY_KEYS if k in tags), None)
    website = normalise_website(tags.get("website") or tags.get("contact:website") or tags.get("url"))
    address = ", ".join(
        p for p in (
            " ".join(p for p in (tags.get("addr:housenumber"), tags.get("addr:street")) if p),
            tags.get("addr:suburb") or tags.get("addr:city"),
            tags.get("addr:postcode"),
        ) if p
    ) or None
    return {
        "source": "osm",
        "source_id": f"{el['type']}/{el['id']}",
        "name": name,
        "category": category,
        "address": address,
        "lat": lat,
        "lon": lon,
        "distance_km": round(haversine_km(centre[0], centre[1], lat, lon), 1) if lat and lon else None,
        "website": website,
        "email": tags.get("email") or tags.get("contact:email"),
        "phone": tags.get("phone") or tags.get("contact:phone"),
        "tags": tags,
        "dedupe_key": dedupe_key(name, website, lat, lon),
    }


def discover(lat: float, lon: float, radius_km: float, user_agent: str) -> Iterator[dict]:
    for el in fetch(lat, lon, radius_km, user_agent):
        biz = to_business(el, (lat, lon))
        if biz:
            yield biz
