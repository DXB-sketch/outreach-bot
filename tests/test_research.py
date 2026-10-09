"""Research stage, LLM connection test and drafts that use research."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from outreach import audit as audit_mod
from outreach import pipeline, research, websites
from outreach.db import connect

from test_pipeline import settings

HOME = """<html><head><title>Bob's Plumbing | Wamuran</title>
<meta name="theme-color" content="#1a5fb4">
<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;700&display=swap" rel="stylesheet">
<style>.btn{background:#e66100;color:#ffffff} .x{color:#333333}</style>
<script type="application/ld+json">{"@type": "Plumber", "name": "Bob's Plumbing", "telephone": "07 5496 1234",
 "areaServed": ["Wamuran", "Caboolture"], "openingHours": "Mo-Fr 07:00-17:00"}</script></head>
<body><img src="/img/bobs-logo.png" alt="Bob's Plumbing logo">
<nav><a href="/about-us">About</a><a href="/services">Our Services</a><a href="/contact">Contact</a>
<a href="/testimonials">Reviews</a><a href="https://facebook.com/bobsplumbing">fb</a></nav>
<h1>Bob's Plumbing</h1><img src="/img/van.jpg" width="800" alt="Our van">
<p>Local plumber serving Wamuran and Caboolture for blocked drains, hot water and gas fitting.</p></body></html>"""
PAGES = {
    "/about-us": "<h1>About Bob</h1><p>Family owned plumbing business based in Wamuran. " + "Friendly local service. " * 20 + "</p>",
    "/services": "<h1>Services</h1><h2>Blocked drains</h2><h2>Hot water systems</h2><h2>Gas fitting</h2><p>" + "We fix it. " * 30 + "</p>",
    "/contact": "<h1>Contact</h1><p>Call 07 5496 1234 or email bob@bobsplumbing.com.au. " + "Open weekdays. " * 20 + "</p>",
    "/testimonials": "<h1>Reviews</h1><p>Bob turned up on time and fixed our hot water the same day. Highly recommend. - Sarah K</p>"
                     + "<p>" + "Great work. " * 30 + "</p>",
}


@pytest.fixture
def fake_site(monkeypatch):
    real_client = httpx.Client
    monkeypatch.setenv("RENDER_JS", "off")
    monkeypatch.setattr(audit_mod, "resolves", lambda host: True)

    passthrough = httpx.HTTPTransport()

    def handler(req):
        if "bobsplumbing" not in req.url.host:
            return passthrough.handle_request(req)  # e.g. the fake LLM router
        path = req.url.path
        if path == "/robots.txt":
            return httpx.Response(404)
        html = HOME if path in ("", "/") else PAGES.get(path)
        if html is None:
            return httpx.Response(404)
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Response(404))  # stylesheets
    monkeypatch.setattr(research.time, "sleep", lambda s: None)


def test_crawl_collects_pages_assets_and_facts(fake_site):
    crawled = research.crawl("https://bobsplumbing.com.au/")
    kinds = {p["kind"] for p in crawled["pages"]}
    assert kinds == {"home", "about", "services", "contact", "reviews"}
    assets = crawled["assets"]
    assert assets["logo"] == "https://bobsplumbing.com.au/img/bobs-logo.png"
    assert assets["images"][0]["src"] == "https://bobsplumbing.com.au/img/van.jpg"
    assert assets["colours"][:2] == ["#1a5fb4", "#e66100"]  # theme colour first, greys dropped
    assert "Montserrat" in assets["fonts"]
    facts = crawled["facts"]
    assert "07 5496 1234" in facts["phones"] and "bob@bobsplumbing.com.au" in facts["emails"]
    assert facts["schema_org"][0]["@type"] == "Plumber"
    services = next(p for p in crawled["pages"] if p["kind"] == "services")
    assert "Hot water systems" in services["headings"]


def test_invented_quotes_are_removed():
    source = "Bob turned up on time and fixed our hot water the same day. Highly recommend."
    kept = research.verify_quotes([
        {"quote": "Bob turned up on time and fixed our hot water the same day.", "by": "Sarah K"},
        {"quote": "Best plumber in Queensland, five stars!", "by": "Made Up"},
        {"quote": "Great", "by": "too short"},
    ], source)
    assert kept == [{"quote": "Bob turned up on time and fixed our hot water the same day.", "by": "Sarah K"}]


def test_template_guess():
    assert research.guess_template({"category": "craft=plumber"}) == "trades"
    assert research.guess_template({"category": "amenity=dentist"}) == "health"
    assert research.guess_template({"category": "amenity=cafe"}) == "hospitality"
    assert research.guess_template({"category": "office=accountant"}) == "professional"
    assert research.guess_template({"category": "shop=hairdresser"}) == "lifestyle"


class FakeRouter(BaseHTTPRequestHandler):
    """Minimal FreeLLMAPI stand-in: /v1/models and /v1/chat/completions."""
    calls: list = []

    def log_message(self, *args):
        pass

    def _send(self, code, body, headers=None):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer good-key":
            return self._send(401, {"error": "bad key"})
        self._send(200, {"data": [{"id": "gemini-3.8-flash"}, {"id": "glm-5.3"}]})

    def do_POST(self):
        if self.headers.get("Authorization") != "Bearer good-key":
            return self._send(401, {"error": "bad key"})
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeRouter.calls.append(body["model"])
        prompt = body["messages"][1]["content"]
        if "capital_of_queensland" in prompt:
            content = '{"ok": true, "capital_of_queensland": "Brisbane"}'
        elif "Website content" in prompt:
            content = json.dumps({
                "business_name": "Bob's Plumbing", "what_they_do": "Local plumber in Wamuran.",
                "services": [{"name": "Hot water", "description": "Hot water systems."}],
                "service_areas": ["Wamuran", "Caboolture"], "selling_points": ["Family owned"],
                "customer_quotes": [{"quote": "Bob turned up on time and fixed our hot water the same day.", "by": "Sarah K"},
                                    {"quote": "Absolutely the best plumber in all of Australia.", "by": "Invented"}],
                "template": "trades", "pitch_angles": ["Show the same-day service"],
                "mockup_headline": "Wamuran's local plumber", "mockup_subheading": "x", "mockup_cta": "Get a free quote",
            })
        elif "Research:" in prompt:
            assert "Hot water" in prompt  # research reaches the draft prompt
            content = '{"subject": "Bob\'s Plumbing website", "email": "Hi Bob, ...", "phone_script": "Hi ..."}'
        else:
            content = '{"what_they_do": "Plumbing", "summary": "Good fit.", "adjustment": 0, "adjustment_reason": "", "red_flags": []}'
        self._send(200, {"model": body["model"], "choices": [{"message": {"content": "```json\n" + content + "\n```"}}],
                         "usage": {"prompt_tokens": 100, "completion_tokens": 20}},
                   {"x-routed-via": "google/gemini-3.8-flash" if body["model"] == "auto:fast" else "zhipu/glm-5.3"})


@pytest.fixture
def router():
    server = HTTPServer(("127.0.0.1", 0), FakeRouter)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    FakeRouter.calls = []
    yield f"http://127.0.0.1:{server.server_port}/v1"
    server.shutdown()


def llm_settings(tmp_path, base_url, key="good-key"):
    return settings(tmp_path, llm_base_url=base_url, llm_api_key=key,
                    llm_fast_models=["auto:fast"], llm_strong_models=["auto:smart"])


def test_llm_test_reports_success(tmp_path, router, capsys):
    s = llm_settings(tmp_path, router)
    conn = connect(s.db_path)
    assert pipeline.llm_test(conn, s)
    out = capsys.readouterr().out
    assert "Models ready to serve right now: 2" in out and "via google/gemini-3.8-flash" in out and "'Brisbane'" in out
    pipeline.usage(conn)
    assert "zhipu/glm-5.3" in capsys.readouterr().out


def test_llm_test_bad_key_and_no_router(tmp_path, router, capsys):
    s = llm_settings(tmp_path, router, key="wrong")
    assert not pipeline.llm_test(connect(s.db_path), s)
    assert "rejected the key" in capsys.readouterr().out
    s = llm_settings(tmp_path, "http://127.0.0.1:9/v1")
    assert not pipeline.llm_test(connect(s.db_path), s)
    assert "Can't reach the router" in capsys.readouterr().out


def test_research_and_draft_end_to_end(tmp_path, router, fake_site, monkeypatch):
    s = llm_settings(tmp_path, router)
    s.llm_min_interval = 0
    conn = connect(s.db_path)
    csv_path = tmp_path / "l.csv"
    csv_path.write_text("name,category,address,website,phone,lat,lon\n"
                        "Bob's Plumbing,plumber,1 Main St Wamuran,bobsplumbing.com.au,07 5496 1234,-27.04,152.87\n")
    pipeline.import_csv(conn, s, csv_path, (-27.04, 152.87))
    pipeline.audit(conn, s, delay=0)
    # Make sure it is shortlisted regardless of the clean test site.
    s.threshold = 0
    pipeline.score(conn, s)
    assert pipeline.research(conn, s) == 1
    assert pipeline.research(conn, s) == 0  # already researched since the last audit

    stored = pipeline.load_research(conn, 1)
    profile = stored["profile"]
    assert [q["by"] for q in profile["customer_quotes"]] == ["Sarah K"]  # invented quote removed
    assert profile["generated_by"] == "LLM (zhipu/glm-5.3)"
    assert stored["crawl"]["assets"]["logo"].endswith("bobs-logo.png")
    md = next((s.data_dir / "research").glob("*.md")).read_text()
    assert "Wamuran's local plumber" in md and "Absolutely the best" not in md

    pipeline.draft(conn, s)
    draft = next(s.drafts_dir.glob("*.md")).read_text()
    assert "Draft written by:** LLM" in draft and "research" in draft.lower()
    assert "auto:smart" in FakeRouter.calls and "auto:fast" in FakeRouter.calls


def test_research_without_llm_is_facts_only(tmp_path, fake_site):
    s = settings(tmp_path, threshold=0)
    conn = connect(s.db_path)
    csv_path = tmp_path / "l.csv"
    csv_path.write_text("name,category,website,phone\nBob's Plumbing,plumber,bobsplumbing.com.au,07 5496 1234\n")
    pipeline.import_csv(conn, s, csv_path, None)
    pipeline.audit(conn, s, delay=0)
    pipeline.score(conn, s, use_llm=False)
    pipeline.research(conn, s, use_llm=False)
    profile = pipeline.load_research(conn, 1)["profile"]
    assert profile["generated_by"] == "facts only" and profile["template"] == "trades"
    assert any(x["name"] == "Hot water systems" for x in profile["services"])
