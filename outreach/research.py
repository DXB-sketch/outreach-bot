"""Deep research on shortlisted leads: everything needed to write a specific pitch and build a mockup.

1. crawl()          plain code: homepage plus up to 4 key pages (about, services, contact, reviews),
                    logo, photos, brand colours, fonts, phone/email, schema.org business data.
2. build_profile()  strong LLM turns the crawl into a structured client profile.
                    Anything presented as a customer quote is checked word-for-word against the
                    crawled text and dropped if it isn't there, so nothing is invented.
3. fallback_profile()  facts-only profile when no LLM is configured.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from . import audit as audit_mod
from .llm import LLM, LLMError

PAGE_KEYWORDS = {
    "about": ("about", "our-story", "who-we-are", "our-team", "meet"),
    "services": ("service", "what-we-do", "our-work", "products", "menu", "treatments", "pricing", "prices"),
    "contact": ("contact", "find-us", "enquir", "get-a-quote", "request-a-quote", "location", "book"),
    "reviews": ("testimonial", "review", "feedback", "gallery", "projects", "portfolio"),
}
TEMPLATES = ("trades", "health", "hospitality", "professional", "retail", "lifestyle")
PAGE_TEXT_LIMIT = 3500
HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
PHONE_RE = re.compile(r"(?:\+?61\s?|\b0)[2-478](?:[ -]?\d){8}\b|\b1[38]00(?:[ -]?\d){6}\b")


# ---------------------------------------------------------------- crawl

def _visible_text(soup: BeautifulSoup) -> str:
    soup = BeautifulSoup(str(soup), "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "form"]):
        tag.extract()
    return re.sub(r"\s+", " ", soup.get_text(" ")).strip()


def _headings(soup: BeautifulSoup) -> list[str]:
    out = []
    for h in soup.find_all(["h1", "h2", "h3"]):
        text = re.sub(r"\s+", " ", h.get_text(" ")).strip()
        if 2 < len(text) < 90 and text not in out:
            out.append(text)
    return out[:20]


def pick_pages(soup: BeautifulSoup, base_url: str) -> dict[str, str]:
    """One internal URL per page type, chosen from the homepage links (earlier keywords win)."""
    host = urlparse(base_url).hostname
    best: dict[str, tuple[int, str]] = {}
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"].split("#")[0])
        parsed = urlparse(href)
        if parsed.hostname != host or parsed.scheme not in ("http", "https"):
            continue
        if re.search(r"\.(pdf|jpe?g|png|gif|webp|zip|docx?)$", parsed.path, re.I) or parsed.path in ("", "/"):
            continue
        label = re.sub(r"\s+", "-", f"{parsed.path} {a.get_text(' ')}".lower().strip())
        for kind, words in PAGE_KEYWORDS.items():
            rank = next((i for i, w in enumerate(words) if w in label), None)
            if rank is not None and (kind not in best or rank < best[kind][0]):
                best[kind] = (rank, href)
    chosen: dict[str, str] = {}
    for kind, (_, href) in best.items():
        if href not in chosen.values():
            chosen[kind] = href
    return chosen


def _jsonld(soup: BeautifulSoup) -> list[dict]:
    out = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for item in items:
            if isinstance(item, dict) and item.get("@type") not in ("WebSite", "WebPage", "BreadcrumbList", "SiteNavigationElement"):
                out.append({k: item[k] for k in (
                    "@type", "name", "description", "telephone", "email", "address", "openingHours",
                    "openingHoursSpecification", "areaServed", "aggregateRating", "priceRange", "sameAs",
                    "foundingDate", "slogan") if k in item})
    return [x for x in out if len(x) > 1][:5]


def _is_neutral(hex_colour: str) -> bool:
    h = hex_colour.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return max(r, g, b) - min(r, g, b) < 24  # whites, blacks and greys


def extract_assets(soup: BeautifulSoup, base_url: str, css_text: str = "") -> dict:
    def absolute(src):
        return urljoin(base_url, src) if src and not src.startswith("data:") else None

    logo = None
    for img in soup.find_all("img"):
        attrs = " ".join([img.get("src", ""), img.get("alt", ""), " ".join(img.get("class", [])), img.get("id", "")]).lower()
        if "logo" in attrs:
            logo = absolute(img.get("src") or img.get("data-src"))
            if logo:
                break
    icon = soup.find("link", rel=lambda r: r and any("apple-touch-icon" in x or x == "icon" for x in r))
    og = soup.find("meta", property="og:image")

    images = []
    for img in soup.find_all("img"):
        src = absolute(img.get("data-src") or img.get("src"))
        if not src or src == logo or re.search(r"(logo|icon|sprite|pixel|badge|\.svg)", src, re.I):
            continue
        try:
            if int(str(img.get("width", "400")).rstrip("px")) < 250:
                continue
        except ValueError:
            pass
        images.append({"src": src, "alt": (img.get("alt") or "").strip()[:120]})
    images = list({i["src"]: i for i in images}.values())[:12]

    theme = soup.find("meta", attrs={"name": "theme-color"})
    inline_css = " ".join(t.get_text() for t in soup.find_all("style")) + " ".join(
        el.get("style", "") for el in soup.find_all(style=True))
    counts = Counter(c.lower() for c in HEX_RE.findall(inline_css + " " + css_text) if not _is_neutral(c))
    colours = [c for c, _ in counts.most_common(5)]
    if theme and theme.get("content", "").startswith("#") and not _is_neutral(theme["content"]):
        colours = [theme["content"].lower()] + [c for c in colours if c != theme["content"].lower()]

    fonts = []
    for link in soup.find_all("link", href=True):
        if "fonts.googleapis.com" in link["href"]:
            fonts += [f.split(":")[0].replace("+", " ") for f in re.findall(r"family=([^&]+)", link["href"])]
    fonts += [f.strip(" '\"") for f in re.findall(r"font-family:\s*([^;,}]+)", inline_css + " " + css_text)]
    fonts = [f for f in dict.fromkeys(fonts) if f and not re.match(r"(inherit|initial|sans-serif|serif|monospace|var\()", f, re.I)][:3]

    return {
        "logo": logo or (absolute(icon.get("href")) if icon else None),
        "og_image": absolute(og.get("content")) if og else None,
        "images": images,
        "colours": colours[:4],
        "fonts": fonts,
    }


def _stylesheet_text(soup: BeautifulSoup, base_url: str, contact: str, limit: int = 2) -> str:
    host = urlparse(base_url).hostname
    texts = []
    for link in soup.find_all("link", rel=lambda r: r and "stylesheet" in r, href=True)[:6]:
        url = urljoin(base_url, link["href"])
        if urlparse(url).hostname != host:
            continue
        try:
            resp = httpx.get(url, headers={"User-Agent": audit_mod.BROWSER_UA}, timeout=15, follow_redirects=True)
            if resp.status_code == 200:
                texts.append(resp.text[:300_000])
        except Exception:
            continue
        if len(texts) >= limit:
            break
    return " ".join(texts)


def crawl(website: str, contact: str = "", delay: float = 1.0) -> dict:
    """Homepage plus up to four key pages. Returns {"ok", "pages", "assets", "facts"}."""
    fetched = audit_mod.fetch(website, contact, tries=1)
    html = fetched.get("html") if fetched.get("ok") else None
    base = fetched.get("final_url") or website
    if not html or len(_visible_text(BeautifulSoup(html, "html.parser")).split()) < 150:
        rendered = audit_mod.render(base)
        html = rendered or html
    if not html:
        return {"ok": False, "pages": [], "assets": {}, "facts": {}, "error": "; ".join(fetched.get("attempts", [])[-2:])}

    home = BeautifulSoup(html, "html.parser")
    pages = [{"kind": "home", "url": base, "title": (home.title.string or "").strip() if home.title and home.title.string else "",
              "headings": _headings(home), "text": _visible_text(home)[:PAGE_TEXT_LIMIT]}]
    raw_text = [home.get_text(" ")]
    jsonld = _jsonld(home)
    for kind, url in pick_pages(home, base).items():
        time.sleep(delay)
        sub = audit_mod.fetch(url, contact, tries=1)
        sub_html = sub.get("html") if sub.get("ok") else None
        if not sub_html:
            continue
        soup = BeautifulSoup(sub_html, "html.parser")
        if len(_visible_text(soup).split()) < 80:
            sub_html = audit_mod.render(url) or sub_html
            soup = BeautifulSoup(sub_html, "html.parser")
        pages.append({"kind": kind, "url": url, "title": (soup.title.string or "").strip() if soup.title and soup.title.string else "",
                      "headings": _headings(soup), "text": _visible_text(soup)[:PAGE_TEXT_LIMIT]})
        raw_text.append(soup.get_text(" "))
        jsonld += _jsonld(soup)

    all_text = " ".join(raw_text)
    phones = list(dict.fromkeys(re.sub(r"\s+", " ", p).strip() for p in PHONE_RE.findall(all_text)))[:3]
    emails = sorted({e.lower() for e in audit_mod.EMAIL_RE.findall(all_text)
                     if not e.lower().endswith(audit_mod.IGNORED_EMAIL_DOMAINS)})[:3]
    socials = sorted({a["href"] for a in home.find_all("a", href=True)
                      if re.search(r"(facebook|instagram|linkedin|tiktok|youtube)\.com", a["href"], re.I)})[:6]
    return {
        "ok": True,
        "pages": pages,
        "assets": extract_assets(home, base, _stylesheet_text(home, base, contact)),
        "facts": {"phones": phones, "emails": emails, "socials": socials, "schema_org": jsonld[:5]},
    }


# ---------------------------------------------------------------- profile

PROFILE_SYSTEM = """You are researching a small local business in South East Queensland for a one-person web design
studio that wants to pitch them a new website and build them a one-page mockup. Work only from the material
provided. Never invent services, prices, years in business, awards, staff names or reviews. If something is not
in the material, leave it out or list it under "unknowns"."""

PROFILE_PROMPT = """Business record:
{record}

Website audit: {audit}

Website content (homepage and key pages, truncated):
{pages}

Structured data found on the site: {schema}

Return JSON with exactly these keys:
{{"business_name": "as they present it",
  "what_they_do": "one plain sentence",
  "services": [{{"name": "...", "description": "one sentence from their own wording"}}],
  "service_areas": ["suburbs or regions they mention"],
  "audience": "who their customers are",
  "tone": "how they talk about themselves, in a few words",
  "selling_points": ["specific things that set them apart, from the material"],
  "customer_quotes": [{{"quote": "copied EXACTLY, word for word, from the material", "by": "name as shown or empty"}}],
  "contact": {{"phone": "", "email": "", "address": "", "hours": ""}},
  "brand_style": "colours, imagery and feel of their current branding in one sentence",
  "template": "one of: {templates}",
  "website_problems": ["the most important problems with their current online presence, plain English, from the audit"],
  "pitch_angles": ["2-3 specific reasons a new website would win them more work, tied to the material"],
  "mockup_headline": "a short, specific homepage headline for them (no hype, no exclamation marks)",
  "mockup_subheading": "one supporting sentence",
  "mockup_cta": "call-to-action button text suited to how they get work (e.g. Get a free quote, Book online)",
  "unknowns": ["useful things the material did not answer"],
  "fit_notes": "1-2 sentences: anything that makes them a better or worse client than the score suggests"}}"""


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def verify_quotes(quotes: list, source_text: str) -> list[dict]:
    """Keep only quotes that genuinely appear in the crawled text."""
    haystack = _norm(source_text)
    kept = []
    for q in quotes or []:
        if not isinstance(q, dict):
            continue
        text = str(q.get("quote", "")).strip().strip('"“”')
        if len(text.split()) >= 4 and _norm(text) in haystack:
            kept.append({"quote": text, "by": str(q.get("by", "")).strip()})
    return kept[:4]


def _record(biz: dict) -> str:
    keys = ("name", "category", "address", "distance_km", "phone", "email", "website", "rating", "review_count")
    return json.dumps({k: biz.get(k) for k in keys if biz.get(k) is not None})


def _audit_summary(audit: dict | None) -> str:
    audit = audit or {}
    if not audit.get("has_website"):
        return "no website found" + (" (only a social/directory page)" if audit.get("social_only") else "")
    return f"status {audit.get('check_status')}; issues: " + ("; ".join(audit.get("issues", [])) or "none")


def build_profile(llm: LLM, biz: dict, audit: dict | None, crawled: dict) -> dict:
    pages = "\n\n".join(
        f"[{p['kind'].upper()}] {p['url']}\nTitle: {p['title']}\nHeadings: {' | '.join(p['headings'])}\n{p['text']}"
        for p in crawled.get("pages", [])
    ) or "(no website content available)"
    profile = llm.chat_json(
        "strong", "research", PROFILE_SYSTEM,
        PROFILE_PROMPT.format(
            record=_record(biz), audit=_audit_summary(audit), pages=pages[:14000],
            schema=json.dumps(crawled.get("facts", {}).get("schema_org", []))[:2000],
            templates=", ".join(TEMPLATES),
        ),
        max_tokens=2500,
    )
    source = " ".join(p["text"] for p in crawled.get("pages", [])) + " " + json.dumps(crawled.get("facts", {}))
    profile["customer_quotes"] = verify_quotes(profile.get("customer_quotes"), source)
    if profile.get("template") not in TEMPLATES:
        profile["template"] = guess_template(biz)
    return profile


def guess_template(biz: dict) -> str:
    from .score import category_text
    c = category_text(biz)
    groups = {
        "health": ("dent", "physio", "chiro", "osteo", "psycholog", "podiatr", "optometr", "veterinar", "massage", "clinic", "doctor", "health"),
        "hospitality": ("cafe", "restaurant", "bakery", "pub", "bar", "winery", "brewery", "caravan", "camp", "guest", "attraction", "food"),
        "professional": ("accountant", "accounting", "bookkeep", "financial", "insurance", "real estate", "estate agent", "mortgage", "architect", "surveyor", "consult"),
        "lifestyle": ("hair", "beauty", "fitness", "gym", "yoga", "dance", "tattoo", "photo", "wedding", "event", "dog", "horse"),
        "retail": ("florist", "gift", "clothes", "jewel", "furniture", "hardware", "nursery", "garden centre", "pet", "store", "shop"),
    }
    for template, words in groups.items():  # order matters: OSM files hairdressers etc. under shop=
        if any(" " + w in c for w in words):
            return template
    return "trades"


def fallback_profile(biz: dict, audit: dict | None, crawled: dict) -> dict:
    """Facts-only profile used when no LLM is configured. No generated copy."""
    services_page = next((p for p in crawled.get("pages", []) if p["kind"] == "services"), None)
    home = next((p for p in crawled.get("pages", []) if p["kind"] == "home"), None)
    headings = (services_page or home or {}).get("headings", [])[1:9]
    facts = crawled.get("facts", {})
    return {
        "business_name": biz["name"],
        "what_they_do": (biz.get("category") or "").split("=")[-1].replace("_", " ") or "unknown",
        "services": [{"name": h, "description": ""} for h in headings],
        "service_areas": [],
        "customer_quotes": [],
        "contact": {"phone": biz.get("phone") or next(iter(facts.get("phones", [])), ""),
                    "email": biz.get("email") or next(iter(facts.get("emails", [])), ""),
                    "address": biz.get("address") or "", "hours": (biz.get("tags") or {}).get("opening_hours", "")},
        "template": guess_template(biz),
        "website_problems": (audit or {}).get("issues", [])[:5],
        "pitch_angles": [],
        "unknowns": ["LLM not configured, so this is a facts-only profile"],
        "generated_by": "facts only",
    }


def profile_markdown(biz: dict, profile: dict, crawled: dict) -> str:
    assets = crawled.get("assets", {})
    facts = crawled.get("facts", {})
    contact = profile.get("contact") or {}

    def bullets(items, fmt=str):
        items = [fmt(i) for i in (items or []) if i]
        return "\n".join(f"- {i}" for i in items) or "- (none found)"

    return f"""# {profile.get('business_name') or biz['name']}: client profile

{profile.get('what_they_do', '')}

**Template:** {profile.get('template')} · **Tone:** {profile.get('tone', '?')} · **Audience:** {profile.get('audience', '?')}
**Phone:** {contact.get('phone') or biz.get('phone') or '?'} · **Email:** {contact.get('email') or biz.get('email') or '?'}
**Address:** {contact.get('address') or biz.get('address') or '?'} · **Hours:** {contact.get('hours') or '?'}
**Website:** {biz.get('website') or 'none found'} · **Reviews:** {biz.get('review_count') if biz.get('review_count') is not None else '?'} (rating {biz.get('rating') or '?'})
**Profile by:** {profile.get('generated_by', 'LLM')}. Check facts before using them in a pitch.

## Services
{bullets(profile.get('services'), lambda s: f"**{s.get('name')}**: {s.get('description', '')}" if isinstance(s, dict) else s)}

## Service areas
{bullets(profile.get('service_areas'))}

## What sets them apart
{bullets(profile.get('selling_points'))}

## Customer quotes (verified word for word on their site)
{bullets(profile.get('customer_quotes'), lambda q: f'"{q["quote"]}" {("- " + q["by"]) if q.get("by") else ""}')}

## Website problems
{bullets(profile.get('website_problems'))}

## Pitch angles
{bullets(profile.get('pitch_angles'))}

## Mockup copy
- Headline: {profile.get('mockup_headline', '')}
- Subheading: {profile.get('mockup_subheading', '')}
- Button: {profile.get('mockup_cta', '')}

## Brand assets
- Style: {profile.get('brand_style', '')}
- Logo: {assets.get('logo') or 'not found'}
- Colours: {', '.join(assets.get('colours', [])) or 'not found'}
- Fonts: {', '.join(assets.get('fonts', [])) or 'not found'}
- Photos: {len(assets.get('images', []))} found
{chr(10).join(f"  - {i['src']}" for i in assets.get('images', [])[:6])}
- Socials: {', '.join(facts.get('socials', [])) or 'none found'}

## Unknowns (ask or check)
{bullets(profile.get('unknowns'))}

## Fit notes
{profile.get('fit_notes', '')}

## Pages read
{bullets([f"{p['kind']}: {p['url']}" for p in crawled.get('pages', [])])}
"""


def research_business(llm: LLM | None, biz: dict, audit: dict | None, contact: str = "") -> dict:
    """Crawl + profile. Returns {"profile", "crawl"}; the crawl keeps assets for the mockup stage."""
    crawled = {"ok": False, "pages": [], "assets": {}, "facts": {}}
    if (audit or {}).get("has_website") and (audit or {}).get("reachable") is not False:
        crawled = crawl(biz["website"], contact)
    profile = None
    if llm:
        try:
            profile = build_profile(llm, biz, audit, crawled)
            profile["generated_by"] = f"LLM ({llm.last_served_by or 'unknown model'})"
        except (LLMError, ValueError) as exc:
            profile = None
            print(f"    LLM research failed, using facts only: {exc}")
    profile = profile or fallback_profile(biz, audit, crawled)
    for page in crawled.get("pages", []):
        page["text"] = page["text"][:1500]  # keep the stored crawl small
    return {"profile": profile, "crawl": crawled}
