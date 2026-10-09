"""Rule-based lead score, 0-100. Transparent on purpose: every point has a reason.

    need  (0-45)  how much they need a new website
    value (0-35)  how likely they are to pay hundreds to thousands for one
    fit   (0-20)  how reachable and local they are

An optional LLM review can nudge the total by up to ±10 (see pipeline.py).
Tune the weights here as you learn which leads actually convert.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date

# Keywords matched against the start of words in the category (e.g. "craft=plumber" → "craft plumber").
HIGH_VALUE = (
    "plumb", "electric", "builder", "construction", "carpent", "roof", "landscap", "lawn",
    "gardener", "painter", "hvac", "air condition", "pest", "fenc", "concret", "tiler", "glaz",
    "solar", "pool", "dentist", "dental", "physio", "chiro", "osteo", "psycholog", "podiatr",
    "optometr", "veterinar", "accountant", "accounting", "bookkeep", "tax advisor", "financial",
    "insurance", "real estate", "estate agent", "mortgage", "car repair", "mechanic", "auto",
    "tyre", "winery", "brewery", "wedding", "event", "architect", "surveyor", "cleaning",
    "removal", "storage", "childcare", "child care", "driving school", "excavat", "earthmov",
    "plaster", "cabinet", "kitchen", "bathroom", "renovat", "handyman", "arborist", "tree",
)
MID_VALUE = (
    "hairdresser", "hair", "beauty", "massage", "cafe", "restaurant", "bakery", "butcher",
    "florist", "pet", "dog", "fitness", "gym", "yoga", "dance", "horse", "farm", "nursery",
    "garden centre", "furniture", "hardware", "bicycle", "clothes", "jewel", "gift", "art",
    "photo", "tattoo", "pub", "bar", "attraction", "camp site", "caravan", "guest house",
    "bed and breakfast", "chalet", "rv park", "campground",
)
LOW_VALUE = ("convenience", "fast food", "kiosk", "newsagent", "charity", "second hand")

# Categories that are almost never a realistic client: big organisations, public bodies,
# and industries that buy websites through head office, booking platforms or specialist agencies.
EXCLUDED = (
    # accommodation (booking platforms and chains)
    "hotel", "motel", "resort", "hostel", "serviced apartment", "extended stay", "inn",
    # legal
    "lawyer", "legal", "solicitor", "barrister", "law firm", "notary", "conveyanc", "attorney",
    # hospitals, aged care and public health
    "hospital", "nursing home", "aged care", "retirement", "social facility",
    # public bodies and institutions
    "government", "council", "police", "fire station", "school", "university", "college",
    "secondary", "primary school",
    "kindergarten", "library", "place of worship", "church", "community centre", "post office",
    # big retail and finance
    "supermarket", "department store", "mall", "fuel", "petrol", "gas station", "bank", "atm",
    "chemist", "pharmacy", "car rental", "car dealer", "car sales",
)

# Whole words that rule a business out by name, whatever its category says.
EXCLUDED_NAME_WORDS = (
    "hospital", "hotel", "motel", "motor inn", "resort", "hostel", "lawyers", "lawyer", "law",
    "legal", "solicitors", "solicitor", "barristers", "conveyancing", "council", "school",
    "college", "university", "tafe", "church", "parish", "aged care", "retirement",
    "nursing home", "police",
)

# Small, private businesses whose names or categories contain an institution word ("school", "college").
NOT_INSTITUTIONS = ("driving", "dance", "music", "swim", "martial", "karate", "tutor", "art", "surf", "riding", "beauty", "hair", "dog", "barber")
INSTITUTION_WORDS = ("school", "college", "university", "academy", "secondary", "primary school")


def _words(text: str | None) -> str:
    return " " + re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip() + " "


def _extra_exclusions() -> tuple[str, ...]:
    """Your own exclusions: EXCLUDE_KEYWORDS=florist,real estate in .env."""
    return tuple(k.strip().lower() for k in os.environ.get("EXCLUDE_KEYWORDS", "").split(",") if k.strip())


def _matches(words: str, keywords) -> str | None:
    """First keyword found at the start of a word, ignoring "school"-type words for private businesses."""
    private = any(" " + k in words for k in NOT_INSTITUTIONS)
    return next((k for k in keywords if " " + k in words and not (private and k in INSTITUTION_WORDS)), None)


def category_text(biz_or_category) -> str:
    """Category plus any extra type labels (Google Places gives several)."""
    if isinstance(biz_or_category, dict):
        tags = biz_or_category.get("tags") or {}
        extra = " ".join(tags.get("types") or [])
        return _words(f"{biz_or_category.get('category') or ''} {extra}")
    return _words(biz_or_category)


def category_tier(biz_or_category) -> str:
    c = category_text(biz_or_category)
    if _matches(c, EXCLUDED + _extra_exclusions()):
        return "excluded"
    if _matches(c, HIGH_VALUE):
        return "high"
    if _matches(c, LOW_VALUE):
        return "low"
    if _matches(c, MID_VALUE):
        return "mid"
    return "unknown"


def excluded_reason(biz: dict) -> str | None:
    """Why this business is not a realistic client, or None. Used at discovery and scoring time."""
    c = category_text(biz)
    hit = _matches(c, EXCLUDED + _extra_exclusions())
    if hit:
        return f"Excluded category ({hit.strip()}: {biz.get('category')})"
    name = _words(biz.get("name"))
    private = any(" " + k in name for k in NOT_INSTITUTIONS)
    hit = next((w for w in EXCLUDED_NAME_WORDS + _extra_exclusions()
                if f" {w} " in name and not (private and w in INSTITUTION_WORDS)), None)
    if hit:
        return f"Excluded by name ('{hit}')"
    tags = biz.get("tags") or {}
    if tags.get("brand") or tags.get("brand:wikidata"):
        return f"Chain/franchise ({tags.get('brand') or 'brand tag'}), marketing decided at head office"
    if tags.get("businessStatus") in ("CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY"):
        return "Listed as closed"
    return None


@dataclass
class ScoreResult:
    score: int
    need: int
    value: int
    fit: int
    tier: str
    reasons: list[str] = field(default_factory=list)
    disqualified: str | None = None

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def disqualify(biz: dict) -> str | None:
    reason = excluded_reason(biz)
    if reason:
        return reason
    if biz.get("status") in ("bad", "skip", "won"):
        return f"Marked '{biz['status']}' by you"
    return None


def score_need(audit: dict | None, reasons: list[str]) -> int:
    if not audit:
        reasons.append("need: not audited yet (+10)")
        return 10
    if not audit.get("has_website"):
        if audit.get("social_only"):
            reasons.append("need: only a social/directory page, no website of their own (+30)")
            return 30
        if audit.get("website_search") in (None, "not searched"):
            reasons.append("need: no website listed and not searched for yet (+12)")
            return 12
        reasons.append(f"need: no website found after searching {audit['website_search']} (+28)")
        return 28
    status = audit.get("check_status")
    if audit.get("reachable") is None or status in ("blocked", "robots"):
        reasons.append("need: site couldn't be checked automatically, so look at it yourself (+8)")
        return 8
    if not audit.get("reachable"):
        label = {"dns_failed": "website domain no longer works", "parked": "domain shows a parked/placeholder page",
                 "broken": "website returns errors on every address tried"}.get(status, "website down or broken")
        reasons.append(f"need: {label} (+35)")
        return 35

    year_now = date.today().year
    rules = [
        (not audit.get("viewport"), 12, "not mobile-friendly"),
        (not audit.get("https"), 8, "no HTTPS"),
        ((audit.get("copyright_year") or year_now) <= year_now - 4, 8, "copyright 4+ years old"),
        (year_now - 4 < (audit.get("copyright_year") or year_now) <= year_now - 2, 4, "copyright 2-3 years old"),
        (bool(audit.get("outdated_tech")), 5, "outdated tech"),
        (bool(audit.get("diy_builder")), 4, "DIY site builder"),
        ((audit.get("load_ms") or 0) > 4000, 6, "very slow server"),
        (2500 < (audit.get("load_ms") or 0) <= 4000, 3, "slow server"),
        (not audit.get("title"), 3, "no page title"),
        (not audit.get("meta_description"), 3, "no meta description"),
        (not audit.get("has_h1"), 2, "no H1"),
        (not audit.get("tel_link"), 2, "no tap-to-call"),
        (not audit.get("has_form") and not audit.get("emails_found"), 2, "no contact path"),
        ((audit.get("word_count") or 999) < 150, 3, "thin content"),
    ]
    pts = 0
    for hit, weight, label in rules:
        if hit:
            pts += weight
            reasons.append(f"need: {label} (+{weight})")
    return min(pts, 45)


def score_value(biz: dict, tier: str, reasons: list[str]) -> int:
    tier_pts = {"high": 15, "mid": 9, "low": 3, "unknown": 6}[tier]
    reasons.append(f"value: {tier} value category (+{tier_pts})")
    pts = tier_pts

    reviews = biz.get("review_count")
    if reviews is None:
        pts += 4
        reasons.append("value: no review data (+4 neutral)")
    elif reviews >= 50:
        pts += 12
        reasons.append(f"value: {reviews} reviews, established and busy (+12)")
    elif reviews >= 20:
        pts += 8
        reasons.append(f"value: {reviews} reviews (+8)")
    elif reviews >= 5:
        pts += 4
        reasons.append(f"value: {reviews} reviews (+4)")
    else:
        reasons.append(f"value: only {reviews} reviews (+0)")

    if (biz.get("rating") or 0) >= 4.5 and (reviews or 0) >= 10:
        pts += 3
        reasons.append("value: highly rated, so a good site would convert well for them (+3)")
    tags = biz.get("tags") or {}
    if tags.get("opening_hours"):
        pts += 2
        reasons.append("value: has published opening hours (+2)")
    if biz.get("phone"):
        pts += 2
        reasons.append("value: has a business phone (+2)")
    if biz.get("address"):
        pts += 2
        reasons.append("value: has a street address (+2)")
    return min(pts, 35)


def score_fit(biz: dict, audit: dict | None, reasons: list[str]) -> int:
    pts = 0
    d = biz.get("distance_km")
    if d is None:
        pts += 4
        reasons.append("fit: distance unknown (+4)")
    else:
        dist_pts = 10 if d <= 10 else 7 if d <= 25 else 4 if d <= 50 else 1
        pts += dist_pts
        reasons.append(f"fit: {d} km away (+{dist_pts})")

    emails = [biz.get("email")] if biz.get("email") else (audit or {}).get("emails_found", [])
    if emails:
        pts += 6
        reasons.append("fit: public email address (+6)")
    elif biz.get("phone"):
        pts += 4
        reasons.append("fit: phone only (+4)")
    if biz.get("address") and d is not None and d <= 25:
        pts += 4
        reasons.append("fit: close enough to visit in person (+4)")
    return min(pts, 20)


def score_business(biz: dict, audit: dict | None) -> ScoreResult:
    tier = category_tier(biz)
    reason = disqualify(biz)
    if reason:
        return ScoreResult(0, 0, 0, 0, tier, [reason], disqualified=reason)
    reasons: list[str] = []
    need = score_need(audit, reasons)
    value = score_value(biz, tier, reasons)
    fit = score_fit(biz, audit, reasons)
    return ScoreResult(need + value + fit, need, value, fit, tier, reasons)


def pick_channel(biz: dict, audit: dict | None) -> str:
    """Best first-contact method. You always send it yourself."""
    has_email = bool(biz.get("email") or (audit or {}).get("emails_found"))
    close = biz.get("distance_km") is not None and biz["distance_km"] <= 25 and biz.get("address")
    no_site = audit is not None and not audit.get("has_website")
    if no_site and close:
        return "visit"   # no website: email is often unread; walk in with the mockup on your phone
    if has_email:
        return "email"
    if biz.get("phone"):
        return "phone"
    if close:
        return "visit"
    return "social"
