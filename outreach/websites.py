"""Find a business's website when the lead source didn't list one.

OpenStreetMap rarely records websites, so "no website in the data" usually does NOT mean "no website".
Methods, cheapest and most reliable first:

1. email domain  bob@bobsplumbing.com.au → bobsplumbing.com.au
2. Google Places   (needs GOOGLE_PLACES_API_KEY; also fills in phone, rating and review count)
3. Brave Search    (needs BRAVE_API_KEY)
4. domain guesses  bobsplumbing.com.au, bobs-plumbing.com.au, bobsplumbing.com …

A candidate is only accepted when the page clearly belongs to the business (its name or phone number
appears on it), so a lookalike domain owned by someone else isn't mistaken for theirs.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from . import audit as audit_mod
from .dedupe import name_similarity, name_tokens, phone_digits
from .sources import haversine_km, is_own_site, normalise_website

FREE_MAIL = ("gmail.com", "hotmail.com", "outlook.com", "yahoo.com", "yahoo.com.au", "bigpond.com",
             "bigpond.net.au", "icloud.com", "live.com", "live.com.au", "optusnet.com.au", "iinet.net.au",
             "tpg.com.au", "internode.on.net", "me.com", "aol.com", "msn.com", "protonmail.com", "proton.me")
TLDS = (".com.au", ".com", ".net.au", ".au")


def _resolves(host: str) -> bool:
    return audit_mod.resolves(host)


def candidate_domains(name: str, suburb: str | None = None, limit: int = 16) -> list[str]:
    tokens = name_tokens(name)
    core = name_tokens(name, drop_generic=True)
    bases = ["".join(tokens), "-".join(tokens), "".join(core)]
    if suburb:
        bases.append("".join(core) + re.sub(r"[^a-z]", "", suburb.lower()))
    if len(core) >= 3:
        bases.append("".join(core[:2]))      # "Smith Bros Landscaping Supplies" → smithbros
    if core and len(core[0]) >= 6:
        bases.append(core[0])                # distinctive first word: "Bunnings Warehouse" → bunnings
    bases = [b for b in dict.fromkeys(bases) if 4 <= len(b) <= 40]
    return [b + t for b in bases for t in TLDS][:limit]


def page_matches(html: str, biz: dict) -> bool:
    """Does this page belong to this business?"""
    if audit_mod.is_parked(html):
        return False
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.extract()
    raw = soup.get_text(" ") + " " + (soup.title.string if soup.title and soup.title.string else "")
    text = " " + re.sub(r"[^a-z0-9]+", " ", raw.lower().replace("&", " and ").replace("'", "").replace("’", "")) + " "
    phone = phone_digits(biz.get("phone"))
    if phone and phone[-8:] in re.sub(r"\D", "", soup.get_text(" ")):
        return True
    words = [t for t in name_tokens(biz["name"], drop_generic=True) if len(t) >= 3] or name_tokens(biz["name"])
    if not words:
        return False
    hits = sum(f" {w} " in text or f" {w}s " in text for w in words)
    return hits / len(words) >= (1.0 if len(words) <= 2 else 0.67)


def _verify(url: str, biz: dict, contact: str) -> str | None:
    fetched = audit_mod.fetch(url, contact, tries=1)
    if fetched.get("ok") and page_matches(fetched["html"], biz):
        return fetched["final_url"]
    if fetched.get("blocked"):
        html = audit_mod.render(url)
        if html and page_matches(html, biz):
            return url
    return None


def from_email(biz: dict, contact: str) -> str | None:
    email = (biz.get("email") or "").lower()
    if "@" not in email:
        return None
    domain = email.split("@", 1)[1].strip()
    if domain in FREE_MAIL or not _resolves(domain):
        return None
    # The email domain is the business's own, so accept it if it serves anything that isn't parked.
    fetched = audit_mod.fetch(f"https://{domain}/", contact, tries=1)
    if fetched.get("ok") and not audit_mod.is_parked(fetched["html"]):
        return fetched["final_url"]
    return None


def from_places(biz: dict, api_key: str) -> dict | None:
    """Look the business up on Google Places. Returns the matching place's details, or None."""
    query = f"{biz['name']} {biz.get('address') or ''}".strip()
    body: dict = {"textQuery": query, "regionCode": "AU", "pageSize": 5}
    if biz.get("lat") is not None and biz.get("lon") is not None:
        body["locationBias"] = {"circle": {"center": {"latitude": biz["lat"], "longitude": biz["lon"]}, "radius": 2000.0}}
    resp = httpx.post(
        "https://places.googleapis.com/v1/places:searchText",
        json=body,
        headers={"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": ",".join("places." + f for f in (
            "id", "displayName", "location", "websiteUri", "nationalPhoneNumber", "rating", "userRatingCount"))},
        timeout=30,
    )
    resp.raise_for_status()
    for place in resp.json().get("places", []):
        name = place.get("displayName", {}).get("text", "")
        if name_similarity(name, biz["name"]) < 0.6:
            continue
        loc = place.get("location", {})
        if biz.get("lat") is not None and loc.get("latitude") is not None:
            if haversine_km(biz["lat"], biz["lon"], loc["latitude"], loc["longitude"]) > 3:
                continue
        return {
            "website": normalise_website(place.get("websiteUri")),
            "phone": place.get("nationalPhoneNumber"),
            "rating": place.get("rating"),
            "review_count": place.get("userRatingCount"),
        }
    return None


def from_brave(biz: dict, api_key: str, suburb: str | None, contact: str) -> str | None:
    query = f'"{biz["name"]}" {suburb or "QLD"}'
    resp = httpx.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "country": "AU", "count": 8},
        headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    for result in resp.json().get("web", {}).get("results", []):
        url = result.get("url")
        if url and is_own_site(url):
            found = _verify(f"https://{urlparse(url).hostname}/", biz, contact)
            if found:
                return found
    return None


def from_guesses(biz: dict, suburb: str | None, contact: str) -> str | None:
    for domain in candidate_domains(biz["name"], suburb):
        if _resolves(domain):
            found = _verify(f"https://{domain}/", biz, contact)
            if found:
                return found
    return None


def suburb_of(biz: dict) -> str | None:
    tags = biz.get("tags") or {}
    if tags.get("addr:suburb") or tags.get("addr:city"):
        return tags.get("addr:suburb") or tags.get("addr:city")
    parts = [p.strip() for p in (biz.get("address") or "").split(",") if p.strip()]
    for part in reversed(parts):
        words = re.sub(r"\b(QLD|Queensland|Australia|\d{4})\b", "", part).strip()
        if words and not re.search(r"\d", words):
            return words
    return None


def find_website(biz: dict, places_key: str = "", brave_key: str = "", contact: str = "") -> dict:
    """Returns {"website": url|None, "method": str, "searched": [...], "places": {...}|None}."""
    searched = []
    suburb = suburb_of(biz)

    searched.append("email domain")
    url = from_email(biz, contact)
    if url:
        return {"website": url, "method": "email domain", "searched": searched, "places": None}

    places = None
    if places_key:
        searched.append("Google Places")
        try:
            places = from_places(biz, places_key)
        except httpx.HTTPError as exc:
            searched[-1] += f" (failed: {type(exc).__name__})"
        if places and places.get("website") and is_own_site(places["website"]):
            return {"website": places["website"], "method": "Google Places", "searched": searched, "places": places}

    if brave_key:
        searched.append("Brave Search")
        try:
            url = from_brave(biz, brave_key, suburb, contact)
        except httpx.HTTPError as exc:
            searched[-1] += f" (failed: {type(exc).__name__})"
            url = None
        if url:
            return {"website": url, "method": "Brave Search", "searched": searched, "places": places}

    searched.append("domain guesses")
    url = from_guesses(biz, suburb, contact)
    if url:
        return {"website": url, "method": "domain guess", "searched": searched, "places": places}
    return {"website": None, "method": None, "searched": searched, "places": places}
