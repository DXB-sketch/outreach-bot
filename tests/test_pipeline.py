from datetime import date
from pathlib import Path

import pytest

from outreach import audit as audit_mod
from outreach import pipeline
from outreach.config import Settings
from outreach.db import connect
from outreach.llm import LLM, parse_json
from outreach.score import category_tier, pick_channel, score_business
from outreach.sources import dedupe_key, is_own_site, normalise_website
from outreach.sources.osm import to_business

OLD_SITE = """<html><head><title>Bob's Plumbing</title>
<script src="/js/jquery-1.8.3.min.js"></script></head>
<body><table><tr><td><font>Welcome</font><font>to</font><font>Bob's</font></td></tr></table>
<p>Call us on 07 5555 0000. Email bob@bobsplumbing.com.au</p>
<p>&copy; 2016 Bob's Plumbing</p></body></html>"""

MODERN_SITE = f"""<!doctype html><html><head><title>Acme Dental | Caboolture</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="Family dentist in Caboolture."></head>
<body><h1>Acme Dental</h1><a href="tel:0755550000">Call</a><form></form>
<p>{'Friendly family dentistry. ' * 60}</p><footer>© {date.today().year} Acme Dental</footer></body></html>"""


def settings(tmp_path: Path, **kw) -> Settings:
    base = dict(
        data_dir=tmp_path, llm_base_url="", llm_api_key="", llm_fast_models=[], llm_strong_models=[],
        llm_min_interval=0, google_places_api_key="", sender_name="Dex", sender_business="Monolith Web Studio",
        sender_email="", sender_phone="0400 000 000", sender_website="https://example.com",
        sender_location="Wamuran, QLD", threshold=60,
    )
    base.update(kw)
    return Settings(**base)


def test_analyse_old_site_finds_problems():
    facts = audit_mod.analyse_html(OLD_SITE, "http://bobsplumbing.com.au/", load_ms=3200, page_kb=10)
    assert not facts["viewport"] and not facts["https"]
    assert facts["copyright_year"] == 2016
    assert "jQuery 1.8.3" in facts["outdated_tech"]
    assert facts["emails_found"] == ["bob@bobsplumbing.com.au"]
    joined = " ".join(facts["issues"])
    assert "mobile" in joined and "HTTPS" in joined and "2016" in joined


def test_analyse_modern_site_is_clean():
    facts = audit_mod.analyse_html(MODERN_SITE, "https://acmedental.com.au/", load_ms=400, page_kb=20)
    assert facts["issues"] == []


def test_scores_rank_sensibly():
    plumber = {"name": "Bob's Plumbing", "category": "craft=plumber", "distance_km": 8,
               "phone": "07 5555 0000", "address": "1 Main St, Wamuran", "tags": {}}
    old = audit_mod.analyse_html(OLD_SITE, "http://bobsplumbing.com.au/", 3200, 10)
    old["has_website"] = old["reachable"] = True
    modern = audit_mod.analyse_html(MODERN_SITE, "https://x.com.au/", 400, 20)
    modern["has_website"] = modern["reachable"] = True
    no_site = {"has_website": False, "issues": ["No website of their own"]}

    s_old = score_business(plumber, old)
    s_none = score_business(plumber, no_site)
    s_modern = score_business(plumber, modern)
    assert s_old.score >= 60 and s_none.score >= 60
    assert s_modern.score < 50
    assert 0 <= s_old.score <= 100 and s_old.need <= 45 and s_old.value <= 35 and s_old.fit <= 20


def test_chains_and_excluded_categories_are_disqualified():
    chain = {"name": "Big Burger", "category": "amenity=fast_food", "tags": {"brand": "Big Burger"}}
    assert score_business(chain, None).disqualified
    assert category_tier("shop=supermarket") == "excluded"
    assert score_business({"name": "X", "category": "amenity=fuel", "tags": {}}, None).score == 0


def test_channel_choice():
    biz = {"distance_km": 5, "address": "1 Main St", "phone": "1", "email": None}
    assert pick_channel(biz, {"has_website": False}) == "visit"
    assert pick_channel(biz, {"has_website": True, "emails_found": ["a@b.com"]}) == "email"
    assert pick_channel({**biz, "address": None}, {"has_website": True}) == "phone"


def test_source_helpers():
    assert normalise_website("bobs.com.au") == "https://bobs.com.au"
    assert not is_own_site("https://www.facebook.com/bobs")
    assert dedupe_key("Bob's", "https://www.bobs.com.au/x", None, None) == "web:bobs.com.au"
    biz = to_business({"type": "node", "id": 1, "lat": -27.0, "lon": 152.9,
                       "tags": {"name": "Bob's", "craft": "plumber", "website": "bobs.com.au"}}, (-27.0, 152.9))
    assert biz["category"] == "craft=plumber" and biz["distance_km"] == 0


def test_parse_json_handles_noise():
    assert parse_json('Sure!\n```json\n{"a": "x \\"}\\" y", "b": {"c": 1}}\n```') == {"a": 'x "}" y', "b": {"c": 1}}
    assert parse_json("<think>{nope}</think>{\"ok\": true}") == {"ok": True}


def test_end_to_end_with_csv(tmp_path, monkeypatch):
    csv_path = tmp_path / "leads.csv"
    csv_path.write_text(
        "name,category,address,website,phone,lat,lon\n"
        "Bob's Plumbing,plumber,1 Main St Wamuran,bobsplumbing.com.au,07 5555 0000,-27.04,152.87\n"
        "Acme Dental,dentist,2 King St Caboolture,acmedental.com.au,07 5555 1111,-27.07,152.95\n"
        "Wamuran Landscaping,landscaper,3 Rd Wamuran,,0400 111 222,-27.03,152.86\n"
    )
    pages = {"bobsplumbing": (OLD_SITE, "http://bobsplumbing.com.au/"),
             "acmedental": (MODERN_SITE, "https://acmedental.com.au/")}

    def fake_fetch(url, ua):
        html, final = next(v for k, v in pages.items() if k in url)
        return {"reachable": True, "status": 200, "final_url": final, "load_ms": 500, "page_kb": 10, "html": html}

    monkeypatch.setattr(audit_mod, "fetch", fake_fetch)
    s = settings(tmp_path)
    conn = connect(s.db_path)
    assert pipeline.import_csv(conn, s, csv_path, (-27.04, 152.87)) == 3
    assert pipeline.import_csv(conn, s, csv_path, (-27.04, 152.87)) == 0  # idempotent
    pipeline.audit(conn, s, delay=0)
    pipeline.score(conn, s, use_llm=False)
    names = [b["name"] for b, _, _ in pipeline.shortlist(conn, s.threshold)]
    assert "Bob's Plumbing" in names and "Wamuran Landscaping" in names and "Acme Dental" not in names

    assert pipeline.draft(conn, s, top=5, use_llm=False) == 2
    draft_text = next(s.drafts_dir.glob("*bob-s-plumbing.md")).read_text()
    assert "Nothing has been sent" in draft_text and "no thanks" in draft_text
    report = pipeline.report(conn, s).read_text()
    assert "Bob's Plumbing" in report and "Acme Dental" not in report.split("## Near misses")[0]


def test_llm_review_and_draft_used_when_configured(tmp_path, monkeypatch):
    s = settings(tmp_path, llm_base_url="http://llm.invalid/v1", llm_api_key="k",
                 llm_fast_models=["fast"], llm_strong_models=["strong"])
    conn = connect(s.db_path)
    csv_path = tmp_path / "l.csv"
    csv_path.write_text("name,category,address,phone,lat,lon\nTiny Tiles,tiler,1 St,0400,-27.04,152.87\n")
    pipeline.import_csv(conn, s, csv_path, (-27.04, 152.87))
    pipeline.audit(conn, s, delay=0)

    calls = []

    def fake_chat(self, tier, purpose, system, user, max_tokens=800):
        calls.append(tier)
        if purpose == "review":
            return '{"what_they_do": "Tiling", "summary": "Good fit.", "adjustment": 5, "adjustment_reason": "busy trade", "red_flags": []}'
        return '{"subject": "Tiny Tiles website", "email": "Hi there, ...", "phone_script": "Hi ..."}'

    monkeypatch.setattr(LLM, "chat", fake_chat)
    pipeline.score(conn, s)
    pipeline.score(conn, s)  # review is cached, not repeated
    assert calls == ["fast"]
    pipeline.draft(conn, s)
    assert calls == ["fast", "strong"]
    text = next(s.drafts_dir.glob("*.md")).read_text()
    assert "Draft written by:** LLM" in text and "busy trade (+5)" in text
