"""Website audit: plain code, no LLM. Fetches one page and checks for common problems."""

from __future__ import annotations

import re
import time
from datetime import date
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .sources import is_own_site

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
COPYRIGHT_RE = re.compile(r"(?:©|&copy;|copyright)\s*(?:\d{4}\s*[-–]\s*)?((?:19|20)\d{2})", re.I)
JQUERY_OLD_RE = re.compile(r"jquery[-.]?(1\.\d+(?:\.\d+)?)(?:\.min)?\.js", re.I)
DIY_BUILDERS = ("wix", "weebly", "godaddy", "site123", "jimdo", "webnode", "yola", "mobirise")
IGNORED_EMAIL_DOMAINS = ("example.com", "sentry.io", "wixpress.com", "domain.com", "email.com")


def _robots_allows(url: str, user_agent: str, client: httpx.Client) -> bool:
    parsed = urlparse(url)
    rp = robotparser.RobotFileParser()
    try:
        resp = client.get(f"{parsed.scheme}://{parsed.netloc}/robots.txt", timeout=10)
        if resp.status_code >= 400:
            return True
        rp.parse(resp.text.splitlines())
        return rp.can_fetch(user_agent, url)
    except httpx.HTTPError:
        return True


def fetch(url: str, user_agent: str) -> dict:
    """Fetch a homepage. Tries https first, then http."""
    candidates = [url]
    if url.startswith("https://"):
        candidates.append("http://" + url.removeprefix("https://"))
    headers = {"User-Agent": user_agent, "Accept-Language": "en-AU,en;q=0.8"}
    error = None
    with httpx.Client(follow_redirects=True, headers=headers, timeout=20) as client:
        for candidate in candidates:
            try:
                if not _robots_allows(candidate, user_agent, client):
                    return {"reachable": True, "robots_blocked": True, "final_url": candidate}
                start = time.monotonic()
                resp = client.get(candidate)
                elapsed_ms = int((time.monotonic() - start) * 1000)
                return {
                    "reachable": resp.status_code < 400,
                    "status": resp.status_code,
                    "final_url": str(resp.url),
                    "load_ms": elapsed_ms,
                    "page_kb": round(len(resp.content) / 1024),
                    "html": resp.text if "html" in resp.headers.get("content-type", "html") else "",
                }
            except httpx.HTTPError as exc:
                error = f"{type(exc).__name__}: {exc}"
    return {"reachable": False, "error": error}


def analyse_html(html: str, final_url: str, load_ms: int | None = None, page_kb: int | None = None) -> dict:
    """Pure function: turn a fetched homepage into audit facts and plain-English issues."""
    soup = BeautifulSoup(html, "html.parser")
    lower = html.lower()
    year_now = date.today().year

    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    meta_desc = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    viewport = soup.find("meta", attrs={"name": re.compile("^viewport$", re.I)})
    generator_tag = soup.find("meta", attrs={"name": re.compile("^generator$", re.I)})
    generator = (generator_tag.get("content") or "").strip() if generator_tag else ""

    for tag in soup(["script", "style", "noscript"]):
        tag.extract()
    text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()

    years = [int(y) for y in COPYRIGHT_RE.findall(html) if int(y) <= year_now]
    copyright_year = max(years) if years else None

    emails = sorted({
        e.lower() for e in EMAIL_RE.findall(html)
        if not e.lower().endswith(IGNORED_EMAIL_DOMAINS) and not e.lower().endswith((".png", ".jpg", ".webp"))
    })
    socials = sorted({
        a["href"] for a in soup.find_all("a", href=True)
        if re.search(r"(facebook|instagram|linkedin|tiktok|youtube)\.com", a["href"], re.I)
    })[:6]

    builder = next((b for b in DIY_BUILDERS if b in generator.lower() or f"{b}.com" in lower), None)
    jquery_old = JQUERY_OLD_RE.search(html)
    outdated = []
    if jquery_old:
        outdated.append(f"jQuery {jquery_old.group(1)}")
    if "<frameset" in lower:
        outdated.append("frames")
    if ".swf" in lower:
        outdated.append("Flash")
    if len(soup.find_all("font")) >= 3:
        outdated.append("<font> tags")
    if len(soup.find_all("table")) >= 4 and not viewport:
        outdated.append("table-based layout")

    facts = {
        "final_url": final_url,
        "https": final_url.startswith("https://"),
        "load_ms": load_ms,
        "page_kb": page_kb,
        "title": title[:150],
        "meta_description": bool(meta_desc and meta_desc.get("content", "").strip()),
        "viewport": bool(viewport),
        "generator": generator[:80],
        "diy_builder": builder,
        "copyright_year": copyright_year,
        "outdated_tech": outdated,
        "has_h1": bool(soup.find("h1")),
        "tel_link": bool(soup.find("a", href=re.compile(r"^tel:", re.I))),
        "has_form": bool(soup.find("form")),
        "emails_found": emails[:5],
        "socials": socials,
        "word_count": len(text.split()),
        "text_excerpt": text[:2500],
    }
    facts["issues"] = issues_from_facts(facts, year_now)
    return facts


def issues_from_facts(f: dict, year_now: int) -> list[str]:
    issues = []
    if not f["viewport"]:
        issues.append("Not mobile-friendly (no responsive viewport)")
    if not f["https"]:
        issues.append("No HTTPS (browsers show 'Not secure')")
    if f["copyright_year"] and f["copyright_year"] <= year_now - 2:
        issues.append(f"Footer says © {f['copyright_year']}, so the site looks unmaintained")
    if f["load_ms"] and f["load_ms"] > 2500:
        issues.append(f"Slow server response ({f['load_ms'] / 1000:.1f}s for the HTML alone)")
    if f["page_kb"] and f["page_kb"] > 3000:
        issues.append(f"Heavy homepage HTML ({f['page_kb']} KB)")
    if not f["title"]:
        issues.append("Missing page title (hurts Google results)")
    if not f["meta_description"]:
        issues.append("Missing meta description (weak Google snippet)")
    if not f["has_h1"]:
        issues.append("No main heading (H1)")
    if f["outdated_tech"]:
        issues.append("Outdated build: " + ", ".join(f["outdated_tech"]))
    if f["diy_builder"]:
        issues.append(f"Built on a DIY builder ({f['diy_builder']})")
    if not f["tel_link"]:
        issues.append("Phone number isn't tap-to-call")
    if not f["has_form"] and not f["emails_found"]:
        issues.append("No contact form or visible email")
    if f["word_count"] < 150:
        issues.append("Very little content on the homepage")
    return issues


def audit_business(biz: dict, user_agent: str) -> dict:
    website = biz.get("website")
    if not website or not is_own_site(website):
        return {"has_website": False, "social_only": bool(website), "social_url": website, "issues": [
            "No website of their own" + (" (only a social/directory page)" if website else "")
        ]}
    fetched = fetch(website, user_agent)
    if fetched.get("robots_blocked"):
        return {"has_website": True, "reachable": True, "robots_blocked": True,
                "final_url": fetched["final_url"], "issues": []}
    if not fetched.get("reachable"):
        reason = fetched.get("error") or f"HTTP {fetched.get('status')}"
        return {"has_website": True, "reachable": False, "error": reason,
                "issues": [f"Website is down or broken ({reason[:80]})"]}
    facts = analyse_html(fetched["html"], fetched["final_url"], fetched["load_ms"], fetched["page_kb"])
    facts.update({"has_website": True, "reachable": True, "status": fetched["status"]})
    # Resolve relative social links for the report.
    facts["socials"] = [urljoin(fetched["final_url"], s) for s in facts["socials"]]
    return facts
