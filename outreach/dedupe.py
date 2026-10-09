"""Duplicate detection: the same business often appears more than once.

OpenStreetMap can map a business as both a point and a building outline, Google Places returns the same
place for several search terms, and names vary between sources ("Bob's Plumbing" vs
"Bob's Plumbing Services Pty Ltd"). Two records are treated as the same business when they share a
website domain or phone number, or have very similar names close together.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from urllib.parse import urlparse

from .sources import haversine_km, is_own_site

# Words that don't help tell businesses apart.
LEGAL_WORDS = {"pty", "ltd", "limited", "proprietary", "inc", "co", "company", "the", "and", "trust", "trading", "as", "t/a"}
GENERIC_WORDS = {
    "services", "service", "group", "qld", "queensland", "australia", "aus", "au", "solutions", "enterprises",
    "brisbane", "sunshine", "coast", "moreton", "bay", "north", "south", "east", "west",
}


def name_tokens(name: str, drop_generic: bool = False) -> list[str]:
    text = name.lower().replace("&", " and ").replace("'", "").replace("’", "")
    tokens = [t for t in re.split(r"[^a-z0-9]+", text) if t and t not in LEGAL_WORDS]
    if drop_generic:
        tokens = [t for t in tokens if t not in GENERIC_WORDS] or tokens
    return tokens


def name_similarity(a: str, b: str) -> float:
    ta, tb = name_tokens(a, True), name_tokens(b, True)
    if not ta or not tb:
        return 0.0
    sa, sb = set(ta), set(tb)
    # "Smith Plumbing" vs "Smith Plumbing Caboolture": one name contained in the other.
    if len(min(sa, sb, key=len)) >= 2 and (sa <= sb or sb <= sa):
        return 0.95
    return SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()


def phone_digits(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone.split(";")[0])
    if digits.startswith("61"):
        digits = "0" + digits[2:]
    return digits[-9:] if len(digits) >= 8 else None


def site_domain(url: str | None) -> str | None:
    if not url or not is_own_site(url):
        return None
    host = (urlparse(url if "://" in url else "https://" + url).hostname or "").lower()
    return host.removeprefix("www.") or None


def is_duplicate(a: dict, b: dict) -> bool:
    da, db = site_domain(a.get("website")), site_domain(b.get("website"))
    if da and da == db:
        return True
    pa, pb = phone_digits(a.get("phone")), phone_digits(b.get("phone"))
    if pa and pa == pb:
        return True

    have_coords = all(x.get(k) is not None for x in (a, b) for k in ("lat", "lon"))
    if have_coords:
        dist = haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
        if dist > 1.5:
            return False
        sim = name_similarity(a["name"], b["name"])
        return sim >= 0.85 or (sim >= 0.7 and dist <= 0.2)
    return name_similarity(a["name"], b["name"]) >= 0.92


def find_duplicate(candidates: list[dict], biz: dict) -> dict | None:
    for other in candidates:
        if is_duplicate(other, biz):
            return other
    return None
