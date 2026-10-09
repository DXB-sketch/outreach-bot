"""The stages: discover → audit → score → draft → report. Each stage is safe to re-run."""

from __future__ import annotations

import csv
import json
import sqlite3
import time
from datetime import date
from pathlib import Path

from . import audit as audit_mod
from . import drafts
from .config import Settings
from .db import load_json, save_json_row, upsert_business
from .llm import LLM, LLMError
from .score import pick_channel, score_business
from .sources import csv_import, geocode, osm, places


def row_to_biz(row: sqlite3.Row) -> dict:
    biz = dict(row)
    biz["tags"] = json.loads(biz.pop("tags_json") or "{}")
    return biz


def resolve_centre(s: Settings, area: str, lat: float | None, lon: float | None) -> tuple[float, float]:
    if lat is not None and lon is not None:
        return lat, lon
    return geocode(area, s.user_agent)


def discover(conn, s: Settings, area: str, radius_km: float, source: str,
             lat: float | None = None, lon: float | None = None,
             queries: list[str] | None = None, pages: int = 1) -> int:
    centre = resolve_centre(s, area, lat, lon)
    print(f"Searching within {radius_km} km of {area} {centre}")
    added = 0
    if source in ("osm", "all"):
        n = sum(upsert_business(conn, b) for b in osm.discover(*centre, radius_km, s.user_agent))
        print(f"  OpenStreetMap: {n} new")
        added += n
    if source in ("places", "all"):
        if not s.google_places_api_key:
            print("  Google Places: skipped (GOOGLE_PLACES_API_KEY not set)")
        else:
            n = sum(upsert_business(conn, b) for b in places.discover(
                s.google_places_api_key, area, *centre, radius_km, queries, pages))
            print(f"  Google Places: {n} new")
            added += n
    conn.commit()
    return added


def import_csv(conn, s: Settings, path: Path, centre: tuple[float, float] | None) -> int:
    n = sum(upsert_business(conn, b) for b in csv_import.discover(path, centre))
    conn.commit()
    print(f"Imported {n} new businesses from {path}")
    return n


def audit(conn, s: Settings, limit: int | None = None, refresh: bool = False, delay: float = 1.0) -> int:
    sql = "SELECT b.* FROM businesses b LEFT JOIN audits a ON a.business_id = b.id WHERE b.status NOT IN ('bad','skip','won')"
    if not refresh:
        sql += " AND a.business_id IS NULL"
    sql += " ORDER BY b.distance_km IS NULL, b.distance_km"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()
    for i, row in enumerate(rows, 1):
        biz = row_to_biz(row)
        result = audit_mod.audit_business(biz, s.user_agent)
        save_json_row(conn, "audits", biz["id"], result)
        conn.commit()
        print(f"  [{i}/{len(rows)}] {biz['name']}: {len(result.get('issues', []))} issues")
        if biz.get("website"):
            time.sleep(delay)
    return len(rows)


def score(conn, s: Settings, use_llm: bool = True) -> int:
    llm = LLM(s, conn) if (use_llm and s.llm_enabled) else None
    rows = conn.execute("SELECT * FROM businesses").fetchall()
    reviewed = 0
    for row in rows:
        biz = row_to_biz(row)
        aud = load_json(conn.execute("SELECT data_json FROM audits WHERE business_id = ?", (biz["id"],)).fetchone())
        result = score_business(biz, aud).as_dict()
        previous = load_json(conn.execute("SELECT data_json FROM scores WHERE business_id = ?", (biz["id"],)).fetchone())

        # Only spend LLM tokens on leads that are close to (or over) the threshold.
        llm_review = (previous or {}).get("llm")
        if llm and not llm_review and not result["disqualified"] and result["score"] >= s.threshold - 10:
            try:
                llm_review = drafts.review(llm, biz, aud)
                reviewed += 1
            except (LLMError, ValueError) as exc:
                print(f"  LLM review failed for {biz['name']}: {exc}")
        if llm_review and not result["disqualified"]:
            try:
                adj = max(-10, min(10, int(llm_review.get("adjustment", 0))))
            except (TypeError, ValueError):
                adj = 0
            result["llm"] = llm_review
            result["score"] = max(0, min(100, result["score"] + adj))
            if adj:
                result["reasons"].append(f"llm: {llm_review.get('adjustment_reason', '')} ({adj:+d})")
        result["channel"] = pick_channel(biz, aud)
        save_json_row(conn, "scores", biz["id"], result, score=result["score"])
        conn.commit()
    print(f"Scored {len(rows)} businesses ({reviewed} new LLM reviews)")
    return len(rows)


def shortlist(conn, threshold: int, limit: int | None = None) -> list[tuple[dict, dict, dict | None]]:
    sql = """SELECT b.*, s.data_json AS score_json, a.data_json AS audit_json
             FROM businesses b JOIN scores s ON s.business_id = b.id
             LEFT JOIN audits a ON a.business_id = b.id
             WHERE s.score >= ? AND b.status IN ('new', 'good')
             ORDER BY s.score DESC"""
    if limit:
        sql += f" LIMIT {int(limit)}"
    out = []
    for row in conn.execute(sql, (threshold,)).fetchall():
        d = dict(row)
        sc = json.loads(d.pop("score_json"))
        aud = json.loads(d.pop("audit_json")) if d.get("audit_json") else None
        d.pop("audit_json", None)
        d["tags"] = json.loads(d.pop("tags_json") or "{}")
        out.append((d, sc, aud))
    return out


def draft(conn, s: Settings, top: int = 10, use_llm: bool = True, redo: bool = False) -> int:
    llm = LLM(s, conn) if (use_llm and s.llm_enabled) else None
    done = {r[0] for r in conn.execute("SELECT business_id FROM drafts")}
    count = 0
    for biz, sc, aud in shortlist(conn, s.threshold):
        if count >= top:
            break
        if biz["id"] in done and not redo:
            continue
        path = drafts.write_draft(llm, biz, aud, sc, sc.get("channel", "email"), s)
        conn.execute("INSERT OR REPLACE INTO drafts (business_id, created_at, channel, path) VALUES (?, datetime('now'), ?, ?)",
                     (biz["id"], sc.get("channel", "email"), str(path)))
        conn.commit()
        count += 1
        print(f"  Draft: {path}")
    print(f"Wrote {count} new drafts")
    return count


def report(conn, s: Settings) -> Path:
    today = date.today().isoformat()
    s.reports_dir.mkdir(parents=True, exist_ok=True)
    rows = shortlist(conn, s.threshold)
    near = shortlist(conn, s.threshold - 10)
    near = [r for r in near if r[1]["score"] < s.threshold][:10]
    drafts_by_id = {r["business_id"]: r["path"] for r in conn.execute("SELECT business_id, path FROM drafts")}
    totals = conn.execute("""SELECT COUNT(*) AS n,
        SUM(CASE WHEN a.business_id IS NOT NULL THEN 1 ELSE 0 END) AS audited
        FROM businesses b LEFT JOIN audits a ON a.business_id = b.id""").fetchone()
    tokens = conn.execute("""SELECT COALESCE(SUM(prompt_tokens),0) + COALESCE(SUM(completion_tokens),0)
        FROM llm_usage WHERE at >= date('now', 'start of month')""").fetchone()[0]

    def line(biz, sc, aud):
        issues = "; ".join((aud or {}).get("issues", [])[:3]) or "-"
        d = drafts_by_id.get(biz["id"])
        return (f"| {sc['score']} | {biz['id']} | {biz['name']} | {(biz.get('category') or '').split('=')[-1]} | "
                f"{biz.get('distance_km') if biz.get('distance_km') is not None else '?'} km | {sc.get('channel', '?')} | "
                f"{issues} | {Path(d).name if d else '-'} |")

    header = "| Score | ID | Business | Type | Dist | Channel | Top issues | Draft |\n|---|---|---|---|---|---|---|---|"
    md = [f"# Lead report {today}", "",
          f"{totals['n']} businesses found · {totals['audited'] or 0} audited · "
          f"{len(rows)} at or above {s.threshold} · LLM tokens this month: {tokens:,}", "",
          "Nothing has been sent. Review each draft, then mark the lead with "
          "`outreach mark <ID> contacted|good|bad|skip|won`.", "",
          f"## Shortlist (score ≥ {s.threshold})", "", header, *[line(*r) for r in rows], ""]
    if near:
        md += ["## Near misses", "", header, *[line(*r) for r in near], ""]
    path = s.reports_dir / f"{today}.md"
    path.write_text("\n".join(md))

    with (s.reports_dir / f"{today}.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["score", "id", "name", "category", "distance_km", "channel", "phone", "email", "website", "address", "issues", "draft"])
        for biz, sc, aud in rows:
            w.writerow([sc["score"], biz["id"], biz["name"], biz.get("category"), biz.get("distance_km"),
                        sc.get("channel"), biz.get("phone"), biz.get("email") or ", ".join((aud or {}).get("emails_found", [])),
                        biz.get("website"), biz.get("address"), "; ".join((aud or {}).get("issues", [])),
                        drafts_by_id.get(biz["id"], "")])
    print(f"Report: {path}")
    return path
