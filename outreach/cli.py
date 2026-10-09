"""Command line: `python -m outreach <command>` (or `outreach <command>` once installed)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import pipeline
from .config import get_settings
from .db import connect, load_json

STATUSES = ("new", "good", "bad", "contacted", "won", "skip")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="outreach", description="Find, audit, score and draft outreach to local businesses.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def area_args(sp):
        sp.add_argument("--area", default="Wamuran QLD", help="place name to search around")
        sp.add_argument("--lat", type=float)
        sp.add_argument("--lon", type=float)
        sp.add_argument("--radius-km", type=float, default=25)

    d = sub.add_parser("discover", help="find businesses")
    area_args(d)
    d.add_argument("--source", choices=("osm", "places", "all"), default="all")
    d.add_argument("--query", action="append", help="Places search term (repeatable); default is a built-in list")
    d.add_argument("--pages", type=int, default=1, help="Places result pages per query (20 each)")

    i = sub.add_parser("import-csv", help="import leads from a CSV")
    i.add_argument("path", type=Path)
    i.add_argument("--lat", type=float)
    i.add_argument("--lon", type=float)

    sub.add_parser("dedupe", help="merge duplicate businesses already in the database")

    fw = sub.add_parser("find-websites", help="look for websites the lead sources didn't list")
    fw.add_argument("--limit", type=int)
    fw.add_argument("--refresh", action="store_true", help="search again for businesses where none was found")

    a = sub.add_parser("audit", help="audit websites")
    a.add_argument("--limit", type=int)
    a.add_argument("--refresh", action="store_true", help="re-audit already audited businesses")

    s = sub.add_parser("score", help="score every business")
    s.add_argument("--no-llm", action="store_true")

    rs = sub.add_parser("research", help="deep research on the top leads: client profile, brand assets, mockup copy")
    rs.add_argument("--top", type=int, default=10)
    rs.add_argument("--id", type=int, action="append", dest="ids", help="research a specific business (repeatable)")
    rs.add_argument("--no-llm", action="store_true")
    rs.add_argument("--refresh", action="store_true", help="redo research that already exists")

    sub.add_parser("llm-test", help="check the LLM connection and show which models are available")
    sub.add_parser("usage", help="LLM tokens used this month")

    dr = sub.add_parser("draft", help="write outreach drafts for the top leads")
    dr.add_argument("--top", type=int, default=10)
    dr.add_argument("--no-llm", action="store_true")
    dr.add_argument("--redo", action="store_true", help="rewrite existing drafts")

    sub.add_parser("report", help="write today's shortlist report")

    r = sub.add_parser("run", help="discover → dedupe → find websites → audit → score → research → draft → report")
    area_args(r)
    r.add_argument("--source", choices=("osm", "places", "all"), default="all")
    r.add_argument("--skip-discover", action="store_true")
    r.add_argument("--refresh", action="store_true",
                   help="redo website searches, audits and drafts for businesses already processed")
    r.add_argument("--audit-limit", type=int, default=150)
    r.add_argument("--top", type=int, default=10)
    r.add_argument("--no-llm", action="store_true")

    ls = sub.add_parser("list", help="list scored businesses")
    ls.add_argument("--min-score", type=int, default=0)
    ls.add_argument("--limit", type=int, default=30)

    sh = sub.add_parser("show", help="show everything known about one business")
    sh.add_argument("id", type=int)

    m = sub.add_parser("mark", help="record what happened with a lead")
    m.add_argument("id", type=int)
    m.add_argument("status", choices=STATUSES)
    m.add_argument("--note")

    args = p.parse_args(argv)
    settings = get_settings()
    conn = connect(settings.db_path)

    if args.cmd == "discover":
        pipeline.discover(conn, settings, args.area, args.radius_km, args.source, args.lat, args.lon, args.query, args.pages)
    elif args.cmd == "import-csv":
        centre = (args.lat, args.lon) if args.lat is not None and args.lon is not None else None
        pipeline.import_csv(conn, settings, args.path, centre)
    elif args.cmd == "dedupe":
        pipeline.dedupe(conn)
    elif args.cmd == "find-websites":
        pipeline.find_websites(conn, settings, args.limit, args.refresh)
    elif args.cmd == "audit":
        pipeline.audit(conn, settings, args.limit, args.refresh)
    elif args.cmd == "score":
        pipeline.score(conn, settings, not args.no_llm)
    elif args.cmd == "research":
        pipeline.research(conn, settings, args.top, args.ids, not args.no_llm, args.refresh)
    elif args.cmd == "llm-test":
        raise SystemExit(0 if pipeline.llm_test(conn, settings) else 1)
    elif args.cmd == "usage":
        pipeline.usage(conn)
    elif args.cmd == "draft":
        pipeline.draft(conn, settings, args.top, not args.no_llm, args.redo)
    elif args.cmd == "report":
        pipeline.report(conn, settings)
    elif args.cmd == "run":
        if not args.skip_discover:
            pipeline.discover(conn, settings, args.area, args.radius_km, args.source, args.lat, args.lon)
        pipeline.dedupe(conn)
        pipeline.find_websites(conn, settings, args.audit_limit, args.refresh)
        pipeline.audit(conn, settings, args.audit_limit, args.refresh)
        pipeline.score(conn, settings, not args.no_llm)
        pipeline.research(conn, settings, args.top, use_llm=not args.no_llm, refresh=args.refresh)
        pipeline.draft(conn, settings, args.top, not args.no_llm, args.refresh)
        pipeline.report(conn, settings)
    elif args.cmd == "list":
        rows = conn.execute("""SELECT b.id, b.name, b.category, b.distance_km, b.status, s.score
            FROM businesses b JOIN scores s ON s.business_id = b.id
            WHERE s.score >= ? ORDER BY s.score DESC LIMIT ?""", (args.min_score, args.limit)).fetchall()
        for r in rows:
            print(f"{r['score']:>3}  #{r['id']:<5} {r['name'][:40]:<40} {(r['category'] or '')[:28]:<28} "
                  f"{r['distance_km'] if r['distance_km'] is not None else '?':>5} km  {r['status']}")
    elif args.cmd == "show":
        row = conn.execute("SELECT * FROM businesses WHERE id = ?", (args.id,)).fetchone()
        if not row:
            raise SystemExit(f"No business #{args.id}")
        out = dict(row)
        out["tags"] = json.loads(out.pop("tags_json") or "{}")
        out["audit"] = load_json(conn.execute("SELECT data_json FROM audits WHERE business_id = ?", (args.id,)).fetchone())
        if out["audit"]:
            out["audit"].pop("text_excerpt", None)
        out["score"] = load_json(conn.execute("SELECT data_json FROM scores WHERE business_id = ?", (args.id,)).fetchone())
        research = load_json(conn.execute("SELECT data_json FROM research WHERE business_id = ?", (args.id,)).fetchone())
        out["research_profile"] = (research or {}).get("profile")
        print(json.dumps(out, indent=2, default=str))
    elif args.cmd == "mark":
        cur = conn.execute("UPDATE businesses SET status = ?, note = COALESCE(?, note) WHERE id = ?",
                           (args.status, args.note, args.id))
        conn.commit()
        print(f"#{args.id} marked {args.status}" if cur.rowcount else f"No business #{args.id}")


if __name__ == "__main__":
    main()
