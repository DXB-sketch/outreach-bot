"""Tests for website detection, duplicate merging and client exclusions."""

import httpx
import pytest

from outreach import audit as audit_mod
from outreach import websites
from outreach.db import connect, merge_duplicates, upsert_business
from outreach.dedupe import is_duplicate, name_similarity, phone_digits
from outreach.score import excluded_reason, score_business

SITE = "<html><head><title>Bob's Plumbing Wamuran</title><meta name=viewport content=x></head>" \
       "<body><h1>Bob's Plumbing</h1><p>Call 07 5496 1234</p></body></html>"


@pytest.fixture
def mock_http(monkeypatch):
    """Route every httpx.Client request through a handler you give it."""
    real_client = httpx.Client
    monkeypatch.setenv("RENDER_JS", "off")
    monkeypatch.setattr(audit_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(audit_mod, "resolves", lambda host: True)

    def install(handler):
        monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    return install


def test_deep_link_404_falls_back_to_homepage(mock_http):
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        if req.url.path == "/old-page.html":
            return httpx.Response(404, text="not found")
        return httpx.Response(200, text=SITE, headers={"content-type": "text/html"})
    mock_http(handler)
    result = audit_mod.fetch("https://bobsplumbing.com.au/old-page.html")
    assert result["ok"] and result["final_url"] == "https://bobsplumbing.com.au/"


def test_bot_block_is_not_reported_as_down(mock_http):
    mock_http(lambda req: httpx.Response(403, text="Forbidden"))
    result = audit_mod.audit_business({"website": "https://bobsplumbing.com.au"})
    assert result["check_status"] == "blocked" and result["reachable"] is None and result["issues"] == []
    assert score_business({"name": "Bob's Plumbing", "category": "craft=plumber", "tags": {}}, result).need == 8


def test_server_errors_retried_then_broken(mock_http):
    calls = []

    def handler(req):
        calls.append(str(req.url))
        return httpx.Response(404) if req.url.path == "/robots.txt" else httpx.Response(500)
    mock_http(handler)
    result = audit_mod.audit_business({"website": "https://bobsplumbing.com.au"})
    assert result["check_status"] == "broken" and result["reachable"] is False
    assert len([c for c in calls if "robots" not in c]) == 8  # 4 address variants, each tried twice


def test_dead_domain(mock_http, monkeypatch):
    def handler(req):
        raise AssertionError("no request should be made for a domain that doesn't resolve")
    mock_http(handler)
    monkeypatch.setattr(audit_mod, "resolves", lambda host: False)
    result = audit_mod.audit_business({"website": "https://gone.com.au"})
    assert result["check_status"] == "dns_failed" and "doesn't resolve" in result["issues"][0]


def test_dns_error_during_fetch(mock_http):
    def handler(req):
        raise httpx.ConnectError("[Errno -2] Name or service not known")
    mock_http(handler)
    assert audit_mod.audit_business({"website": "https://gone.com.au"})["check_status"] == "dns_failed"


def test_parked_domain(mock_http):
    mock_http(lambda req: httpx.Response(200, text="<html><body>This domain is for sale! Buy this domain.</body></html>",
                                         headers={"content-type": "text/html"}))
    assert audit_mod.audit_business({"website": "https://bobs.com.au"})["check_status"] == "parked"


def test_url_variants():
    v = audit_mod.url_variants("https://www.bobs.com.au/contact")
    assert v[0] == "https://www.bobs.com.au/contact"
    assert {"https://www.bobs.com.au/", "https://bobs.com.au/", "http://bobs.com.au/"} <= set(v)


def test_page_matches_by_name_or_phone():
    biz = {"name": "Bob's Plumbing Pty Ltd", "phone": "(07) 5496 1234"}
    assert websites.page_matches(SITE, biz)
    assert websites.page_matches("<p>Call us 0754961234</p>", {"name": "Totally Different", "phone": "07 5496 1234"})
    assert not websites.page_matches("<h1>Jim's Electrical</h1>", biz)
    assert not websites.page_matches("<p>Bob's Plumbing. This domain is for sale</p>", biz)


def test_candidate_domains():
    c = websites.candidate_domains("Bob's Plumbing Pty Ltd", "Wamuran")
    assert c[0] == "bobsplumbing.com.au" and "bobs-plumbing.com.au" in c and "bobsplumbingwamuran.com.au" in c
    assert "bunnings.com.au" in websites.candidate_domains("Bunnings Warehouse")


def test_find_website_by_domain_guess(monkeypatch):
    monkeypatch.setattr(websites, "_resolves", lambda host: host == "bobsplumbing.com.au")
    monkeypatch.setattr(audit_mod, "fetch", lambda url, contact="", tries=2: {
        "ok": True, "html": SITE, "final_url": url})
    result = websites.find_website({"name": "Bob's Plumbing", "address": "1 Main St, Wamuran"})
    assert result["website"] == "https://bobsplumbing.com.au/" and result["method"] == "domain guess"


def test_find_website_from_email_domain(monkeypatch):
    monkeypatch.setattr(websites, "_resolves", lambda host: True)
    monkeypatch.setattr(audit_mod, "fetch", lambda url, contact="", tries=2: {
        "ok": True, "html": "<h1>Welcome</h1>", "final_url": url})
    result = websites.find_website({"name": "X", "email": "info@xtrades.com.au"})
    assert result["method"] == "email domain"
    assert websites.find_website.__defaults__  # gmail etc. are never treated as a website:
    assert websites.from_email({"name": "X", "email": "bob@gmail.com"}, "") is None


def test_no_website_is_low_need_until_searched():
    biz = {"name": "Bob's Plumbing", "category": "craft=plumber", "tags": {}}
    unsearched = audit_mod._no_website_audit({"name": "Bob's", "website_status": None})
    searched = audit_mod._no_website_audit({"name": "Bob's", "website_status": "not_found:email domain, domain guesses"})
    assert score_business(biz, unsearched).need == 12
    assert score_business(biz, searched).need == 28


def test_duplicate_detection():
    a = {"name": "Bob's Plumbing", "lat": -27.0400, "lon": 152.8700}
    assert is_duplicate(a, {"name": "Bob's Plumbing Services Pty Ltd", "lat": -27.0405, "lon": 152.8702})
    assert is_duplicate(a, {"name": "Bobs Plumbing", "lat": -27.0401, "lon": 152.8701})
    assert is_duplicate({"name": "A", "phone": "(07) 5496 1234"}, {"name": "B", "phone": "+61 7 5496 1234"})
    assert is_duplicate({"name": "A", "website": "https://www.bobs.com.au/x"}, {"name": "B", "website": "http://bobs.com.au"})
    assert not is_duplicate(a, {"name": "Jim's Electrical", "lat": -27.0400, "lon": 152.8700})
    assert not is_duplicate(a, {"name": "Bob's Plumbing", "lat": -27.30, "lon": 152.87})  # far away
    assert not is_duplicate({"name": "A", "website": "https://facebook.com/a"}, {"name": "B", "website": "https://facebook.com/b"})
    assert phone_digits("+61 7 5496 1234") == phone_digits("07 5496 1234")
    assert name_similarity("Smith Landscaping", "Smith Landscaping Caboolture") >= 0.9


def test_osm_node_and_way_merge_on_insert(tmp_path):
    conn = connect(tmp_path / "t.db")
    base = {"source": "osm", "name": "Bob's Plumbing", "lat": -27.04, "lon": 152.87, "tags": {}}
    assert upsert_business(conn, {**base, "source_id": "node/1"})
    assert not upsert_business(conn, {**base, "source_id": "way/2", "phone": "0400 000 000"})
    assert not upsert_business(conn, {**base, "source": "places", "source_id": "p1", "name": "Bobs Plumbing Pty Ltd",
                                      "review_count": 40, "rating": 4.8})
    rows = conn.execute("SELECT * FROM businesses").fetchall()
    assert len(rows) == 1 and rows[0]["phone"] == "0400 000 000" and rows[0]["review_count"] == 40


def test_merge_existing_duplicates(tmp_path):
    conn = connect(tmp_path / "t.db")
    for i, name in enumerate(["Bob's Plumbing", "Bobs Plumbing", "Jim's Electrical"]):
        conn.execute("INSERT INTO businesses (source, source_id, name, lat, lon, discovered_at, phone) "
                     "VALUES ('osm', ?, ?, -27.04, 152.87, 'x', ?)", (str(i), name, "0400 000 000" if i == 1 else None))
    conn.execute("INSERT INTO scores (business_id, scored_at, score, data_json) VALUES (2, 'x', 70, '{}')")
    merged = merge_duplicates(conn)
    assert merged == [(1, 2)]
    names = [r["name"] for r in conn.execute("SELECT name FROM businesses ORDER BY id")]
    assert names == ["Bob's Plumbing", "Jim's Electrical"]
    assert conn.execute("SELECT phone FROM businesses WHERE id = 1").fetchone()[0] == "0400 000 000"
    assert conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 0


@pytest.mark.parametrize("name,category,excluded", [
    ("Caboolture Hospital", "amenity=hospital", True),
    ("Smith & Co Lawyers", "office=lawyer", True),
    ("Jones Legal", "places=consultant", True),
    ("Wamuran Motor Inn", "tourism=motel", True),
    ("Glasshouse Lodge", "places=hotel", True),
    ("Royal Hotel", "amenity=pub", True),
    ("Caboolture State School", "amenity=school", True),
    ("Bob's Lawn Mowing", "craft=gardener", False),
    ("Kilcoy Driving School", "amenity=driving_school", False),
    ("Joe's Plumbing", "craft=plumber", False),
    ("Sunny Caravan Park", "places=rv_park", False),
])
def test_exclusions(name, category, excluded):
    assert bool(excluded_reason({"name": name, "category": category, "tags": {}})) is excluded


def test_extra_exclusions_from_env(monkeypatch):
    monkeypatch.setenv("EXCLUDE_KEYWORDS", "real estate")
    assert excluded_reason({"name": "Ray's", "category": "places=real_estate_agency", "tags": {}})
