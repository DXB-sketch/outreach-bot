"""Rule-based lead score, 0-100. Transparent on purpose: every point has a reason.

    need  (0-45)  how much they need a new website
    value (0-35)  how likely they are to pay hundreds to thousands for one
    fit   (0-20)  how reachable and local they are

An optional LLM review can nudge the total by up to ±10 (see pipeline.py).
Tune the weights here as you learn which leads actually convert.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

# Keywords matched against the category (e.g. "craft=plumber", "places=dentist").
HIGH_VALUE = (
    "plumb", "electric", "builder", "construction", "carpent", "roof", "landscap",
    "gardener", "painter", "hvac", "air_condition", "air conditioning", "pest", "fenc",
    "concret", "tiler", "glaz", "solar", "pool", "dentist", "physio", "chiro", "osteo",
    "psycholog", "podiatr", "optometr", "veterinar", "lawyer", "legal", "accountant",
    "tax_advisor", "financial", "insurance", "real_estate", "estate_agent", "mortgage",
    "car_repair", "mechanic", "auto", "tyres", "winery", "brewery", "guest_house",
    "hotel", "motel", "caravan", "accommodation", "wedding", "event", "architect",
    "surveyor", "cleaning", "removal", "storage", "childcare", "driving_school",
)
MID_VALUE = (
    "hairdresser", "beauty", "massage", "cafe", "restaurant", "bakery", "butcher",
    "florist", "pet", "dog", "fitness", "gym", "yoga", "dance", "horse", "farm",
    "nursery", "garden_centre", "furniture", "hardware", "bicycle", "clothes",
    "jewel", "gift", "art", "photo", "tattoo", "pub", "bar", "attraction", "camp_site",
)
LOW_VALUE = ("convenience", "fast_food", "kiosk", "newsagent", "charity", "second_hand")

# Categories that are almost never a fit (big organisations, public bodies).
EXCLUDED = ("supermarket", "fuel", "bank", "post_office", "government", "department_store",
            "chemist", "pharmacy", "mall", "car_rental", "atm")


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


def category_tier(category: str | None) -> str:
    c = (category or "").lower()
    if any(k in c for k in EXCLUDED):
        return "excluded"
    if any(k in c for k in HIGH_VALUE):
        return "high"
    if any(k in c for k in LOW_VALUE):
        return "low"
    if any(k in c for k in MID_VALUE):
        return "mid"
    return "unknown"


def disqualify(biz: dict, tier: str) -> str | None:
    tags = biz.get("tags") or {}
    if tags.get("brand") or tags.get("brand:wikidata"):
        return f"Chain/franchise ({tags.get('brand') or 'brand tag'}), marketing decided at head office"
    if tags.get("businessStatus") in ("CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY"):
        return "Listed as closed"
    if tier == "excluded":
        return f"Category unlikely to buy ({biz.get('category')})"
    if biz.get("status") in ("bad", "skip", "won"):
        return f"Marked '{biz['status']}' by you"
    return None


def score_need(audit: dict | None, reasons: list[str]) -> int:
    if not audit:
        reasons.append("need: not audited yet (+10)")
        return 10
    if not audit.get("has_website"):
        pts = 28 if not audit.get("social_only") else 30
        reasons.append(f"need: {'only a social/directory page' if audit.get('social_only') else 'no website'} (+{pts})")
        return pts
    if not audit.get("reachable"):
        reasons.append("need: website down or broken (+35)")
        return 35
    if audit.get("robots_blocked"):
        reasons.append("need: site blocks crawlers, unknown quality (+8)")
        return 8

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
    tier = category_tier(biz.get("category"))
    reason = disqualify(biz, tier)
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
