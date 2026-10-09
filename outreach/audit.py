"""Website audit: plain code, no LLM. Fetches one page and checks for common problems."""

from __future__ import annotations

import os
import re
import socket
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


# A normal browser identity: many small-business hosts and firewalls return 403/404 to unknown bots.
# The crawler still identifies you through the From header and obeys robots.txt for its own name.
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
BOT_NAME = "MonolithOutreachBot"
BLOCKED_CODES = {401, 403, 406, 409, 429, 503}
PARKED_MARKERS = (
    "domain is for sale", "buy this domain", "this domain may be for sale", "domain parking",
    "this domain is parked", "parked free", "sedoparking", "hugedomains", "afternic", "dan.com/buy",
    "domain has expired", "this domain name has expired", "renew this domain", "future home of",
    "website coming soon", "site is under construction", "account suspended", "default web site page",
    "welcome to nginx", "apache2 ubuntu default page", "it works!",
)
DNS_ERRORS = ("name or service not known", "nodename nor servname", "no address associated",
              "name resolution", "getaddrinfo failed", "temporary failure in name resolution")


def _robots_allows(url: str, client: httpx.Client) -> bool:
    parsed = urlparse(url)
    rp = robotparser.RobotFileParser()
    try:
        resp = client.get(f"{parsed.scheme}://{parsed.netloc}/robots.txt", timeout=10)
        if resp.status_code >= 400:
            return True
        rp.parse(resp.text.splitlines())
        return rp.can_fetch(BOT_NAME, url)
    except httpx.HTTPError:
        return True


def url_variants(url: str) -> list[str]:
    """The given URL, then the site root, with and without www, https before http."""
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if not host:
        return [url]
    alt = host[4:] if host.startswith("www.") else "www." + host
    out = [url if p.path else url + "/"]
    for scheme in ("https", "http"):
        for h in (host, alt):
            out.append(f"{scheme}://{h}/")
    return list(dict.fromkeys(out))


def resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, 443)
        return True
    except (socket.gaierror, UnicodeError, OSError):
        return False


def is_parked(html: str) -> bool:
    lower = html[:20000].lower()
    return any(m in lower for m in PARKED_MARKERS)


def fetch(url: str, contact: str = "", tries: int = 2) -> dict:
    """Fetch a homepage, trying URL variants and retrying before deciding a site is broken.

    Result keys: ok (bool); on success html/final_url/status/load_ms/page_kb/ssl_error;
    on failure one of blocked, dns_failed, broken, robots_blocked, plus attempts (what was tried).
    """
    headers = {
        "User-Agent": BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-AU,en;q=0.9",
    }
    if "@" in contact:
        headers["From"] = contact
    attempts: list[str] = []
    outcomes: set[str] = set()
    ssl_error = False
    host = (urlparse(url).hostname or "").lower()
    alt = host[4:] if host.startswith("www.") else "www." + host
    if host and not resolves(host) and not resolves(alt):
        return {"ok": False, "dns_failed": True, "attempts": [f"{host}: domain does not resolve"]}
    with httpx.Client(follow_redirects=True, headers=headers, timeout=httpx.Timeout(20, connect=10)) as client:
        if not _robots_allows(url, client):
            return {"ok": False, "robots_blocked": True, "final_url": url, "attempts": ["robots.txt disallows"]}
        for variant in url_variants(url):
            if sum("timed out" in a for a in attempts) >= 3:
                break  # don't spend minutes on a host that never answers
            for attempt in range(tries):
                try:
                    start = time.monotonic()
                    resp = client.get(variant)
                    elapsed_ms = int((time.monotonic() - start) * 1000)
                except httpx.ConnectError as exc:
                    msg = str(exc).lower()
                    if any(e in msg for e in DNS_ERRORS):
                        outcomes.add("dns")
                        attempts.append(f"{variant}: domain does not resolve")
                        break
                    if "certificate" in msg or "ssl" in msg:
                        ssl_error = True
                        outcomes.add("ssl")
                        attempts.append(f"{variant}: SSL certificate error")
                        break
                    outcomes.add("connect")
                    attempts.append(f"{variant}: connection failed ({str(exc)[:60]})")
                except httpx.TimeoutException:
                    outcomes.add("timeout")
                    attempts.append(f"{variant}: timed out")
                    if sum("timed out" in a for a in attempts) >= 3:
                        break
                except httpx.HTTPError as exc:
                    outcomes.add("other")
                    attempts.append(f"{variant}: {type(exc).__name__}")
                    break
                else:
                    ctype = resp.headers.get("content-type", "text/html")
                    if resp.status_code < 400 and "html" in ctype:
                        return {
                            "ok": True, "status": resp.status_code, "final_url": str(resp.url),
                            "load_ms": elapsed_ms, "page_kb": round(len(resp.content) / 1024),
                            "html": resp.text, "ssl_error": ssl_error, "attempts": attempts,
                        }
                    attempts.append(f"{variant}: HTTP {resp.status_code}")
                    if resp.status_code in BLOCKED_CODES or "cf-mitigated" in resp.headers:
                        outcomes.add("blocked")
                    elif resp.status_code >= 500:
                        outcomes.add("server_error")
                        if attempt + 1 < tries:
                            time.sleep(3)
                            continue
                    else:
                        outcomes.add("not_found")
                    break
                if attempt + 1 < tries:
                    time.sleep(2)
    result = {"ok": False, "attempts": attempts}
    if outcomes == {"dns"}:
        result["dns_failed"] = True
    elif outcomes & {"blocked", "timeout", "connect", "other"}:
        # Can't tell whether the site is broken or just refusing automated visits.
        result["blocked"] = True
    else:
        result["broken"] = True  # every variant answered with 404/410/5xx or a bad certificate
    return result


def render(url: str, timeout_ms: int = 20000) -> str | None:
    """Load a page in headless Chromium so JavaScript-built sites (Wix, Squarespace, React) can be read.

    Optional: needs `pip install playwright` and `playwright install chromium`. Returns None if unavailable.
    """
    if os.environ.get("RENDER_JS", "auto").lower() in ("0", "off", "false", "no"):
        return None
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    try:
        with sync_playwright() as pw:
            exe = os.environ.get("PLAYWRIGHT_CHROMIUM_PATH")
            browser = pw.chromium.launch(**({"executable_path": exe} if exe else {}))
            try:
                page = browser.new_page(user_agent=BROWSER_UA, locale="en-AU")
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                return page.content()
            finally:
                browser.close()
    except Exception:
        return None


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


def _no_website_audit(biz: dict) -> dict:
    website = biz.get("website")
    status = biz.get("website_status") or ""
    if website:  # only a social or directory page
        return {"has_website": False, "social_only": True, "social_url": website,
                "issues": ["No website of their own, only a social/directory page"]}
    checked = status.removeprefix("not_found:") if status.startswith("not_found") else ""
    return {"has_website": False, "website_search": checked or "not searched",
            "issues": ["No website found" + (f" (searched: {checked})" if checked else
                                             " (not searched yet, so run find-websites)")]}


def audit_business(biz: dict, contact: str = "") -> dict:
    website = biz.get("website")
    if not website or not is_own_site(website):
        return _no_website_audit(biz)

    fetched = fetch(website, contact)
    rendered_html = None
    if not fetched["ok"] and fetched.get("blocked"):
        # Firewalls often block plain HTTP clients but let a real browser through.
        rendered_html = render(website)
        if rendered_html:
            fetched = {"ok": True, "status": 200, "final_url": website, "load_ms": None,
                       "page_kb": round(len(rendered_html) / 1024), "html": rendered_html,
                       "ssl_error": False, "attempts": fetched["attempts"]}

    base = {"has_website": True, "attempts": fetched.get("attempts", [])[-6:]}
    if fetched.get("robots_blocked"):
        return {**base, "reachable": None, "check_status": "robots", "final_url": website,
                "issues": [], "note": "Site asks robots not to crawl it. Check it yourself."}
    if fetched.get("blocked"):
        return {**base, "reachable": None, "check_status": "blocked", "final_url": website, "issues": [],
                "note": "Site refused or timed out on automated checks. Check it yourself."}
    if fetched.get("dns_failed"):
        return {**base, "reachable": False, "check_status": "dns_failed", "final_url": website,
                "issues": [f"Website domain no longer works ({urlparse(website).hostname} doesn't resolve)"]}
    if fetched.get("broken"):
        return {**base, "reachable": False, "check_status": "broken", "final_url": website,
                "issues": [f"Website returns errors on every address tried ({fetched['attempts'][-1] if fetched['attempts'] else 'no response'})"]}

    html = fetched["html"]
    facts = analyse_html(html, fetched["final_url"], fetched["load_ms"], fetched["page_kb"])
    # JavaScript-built pages look empty to a plain HTTP fetch. Re-read them in a browser before judging.
    if not rendered_html and (facts["word_count"] < 150 or not facts["has_h1"]):
        rendered_html = render(fetched["final_url"])
        if rendered_html:
            rendered = analyse_html(rendered_html, fetched["final_url"], fetched["load_ms"], fetched["page_kb"])
            if rendered["word_count"] >= facts["word_count"]:
                facts = rendered
                facts["rendered_js"] = True
    if is_parked(html if not facts.get("rendered_js") else rendered_html):
        return {**base, "reachable": False, "check_status": "parked", "final_url": fetched["final_url"],
                "issues": ["Domain shows a parked, expired or placeholder page instead of a website"]}
    if fetched.get("ssl_error") and not facts["https"]:
        facts["issues"].insert(0, "HTTPS is broken (certificate error), so only the insecure http:// version loads")
    facts.update({**base, "reachable": True, "check_status": "ok", "status": fetched["status"]})
    facts["socials"] = [urljoin(fetched["final_url"], s) for s in facts["socials"]]
    return facts
