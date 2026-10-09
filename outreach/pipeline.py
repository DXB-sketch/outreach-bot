"""The stages: discover → audit → score → draft → report. Each stage is safe to re-run."""

from __future__ import annotations

import csv
import json
import sqlite3
import time
from datetime import date
from pathlib import Path

from . import audit as audit_mod
from . import drafts, research as research_mod, websites
from .config import Settings
from .db import load_json, merge_duplicates, now, save_json_row, upsert_business
from .llm import LLM, LLMError
from .score import excluded_reason, pick_channel, score_business
from .sources import csv_import, geocode, is_own_site, osm, places


def row_to_biz(row: sqlite3.Row) -> dict:
    biz = dict(row)
    biz["tags"] = json.loads(biz.pop("tags_json") or "{}")
    return biz


def resolve_centre(s: Settings, area: str, lat: float | None, lon: float | None) -> tuple[float, float]:
    if lat is not None and lon is not None:
        return lat, lon
    return geocode(area, s.user_agent)


def _store(conn, found, label: str) -> int:
    """Save discovered businesses, skipping excluded ones and duplicates."""
    added = excluded = dupes = 0
    for biz in found:
        if excluded_reason(biz):
            excluded += 1
        elif upsert_business(conn, biz):
            added += 1
        else:
            dupes += 1
    conn.commit()
    print(f"  {label}: {added} new, {dupes} duplicates or already known, {excluded} excluded")
    return added


def discover(conn, s: Settings, area: str, radius_km: float, source: str,
             lat: float | None = None, lon: float | None = None,
             queries: list[str] | None = None, pages: int = 1) -> int:
    centre = resolve_centre(s, area, lat, lon)
    print(f"Searching within {radius_km} km of {area} {centre}")
    added = 0
    if source in ("osm", "all"):
        added += _store(conn, osm.discover(*centre, radius_km, s.user_agent), "OpenStreetMap")
    if source in ("places", "all"):
        if not s.google_places_api_key:
            print("  Google Places: skipped (GOOGLE_PLACES_API_KEY not set)")
        else:
            added += _store(conn, places.discover(
                s.google_places_api_key, area, *centre, radius_km, queries, pages), "Google Places")
    return added


def import_csv(conn, s: Settings, path: Path, centre: tuple[float, float] | None) -> int:
    return _store(conn, csv_import.discover(path, centre), f"CSV {path}")


def dedupe(conn) -> int:
    merged = merge_duplicates(conn)
    for kept, removed in merged:
        print(f"  merged #{removed} into #{kept}")
    print(f"Merged {len(merged)} duplicate records")
    return len(merged)


def _active_rows(conn, where: str = "1=1", params: tuple = ()) -> list[dict]:
    rows = conn.execute(
        f"SELECT * FROM businesses WHERE status NOT IN ('bad','skip','won') AND {where} "
        "ORDER BY distance_km IS NULL, distance_km", params).fetchall()
    return [b for b in map(row_to_biz, rows) if not excluded_reason(b)]


def find_websites(conn, s: Settings, limit: int | None = None, refresh: bool = False) -> int:
    """Look for websites the lead sources didn't list. Businesses already searched are skipped."""
    where = "(website IS NULL OR website_status IS NULL OR website_status LIKE 'not_found%')" if refresh \
        else "website_status IS NULL"
    todo = [b for b in _active_rows(conn, where) if not b.get("website") or not is_own_site(b["website"])]
    todo = todo[:limit] if limit else todo
    found = 0
    for i, biz in enumerate(todo, 1):
        result = websites.find_website(biz, s.google_places_api_key, s.brave_api_key, s.sender_email)
        extra = result.get("places") or {}
        for field in ("phone", "rating", "review_count"):
            if extra.get(field) is not None and biz.get(field) is None:
                conn.execute(f"UPDATE businesses SET {field} = ? WHERE id = ?", (extra[field], biz["id"]))
        if result["website"]:
            found += 1
            conn.execute("UPDATE businesses SET website = ?, website_status = ? WHERE id = ?",
                         (result["website"], f"found:{result['method']}", biz["id"]))
            print(f"  [{i}/{len(todo)}] {biz['name']}: {result['website']} (via {result['method']})")
        else:
            conn.execute("UPDATE businesses SET website_status = ? WHERE id = ?",
                         ("not_found:" + ", ".join(result["searched"]), biz["id"]))
            print(f"  [{i}/{len(todo)}] {biz['name']}: none found")
        conn.execute("DELETE FROM audits WHERE business_id = ?", (biz["id"],))  # re-audit with the new info
        conn.commit()
    print(f"Found websites for {found} of {len(todo)} businesses")
    return found


def audit(conn, s: Settings, limit: int | None = None, refresh: bool = False, delay: float = 1.0) -> int:
    where = "1=1" if refresh else "id NOT IN (SELECT business_id FROM audits)"
    rows = _active_rows(conn, where)
    rows = rows[:limit] if limit else rows
    for i, biz in enumerate(rows, 1):
        result = audit_mod.audit_business(biz, s.sender_email)
        save_json_row(conn, "audits", biz["id"], result)
        conn.commit()
        state = result.get("check_status") or ("no website" if not result.get("has_website") else "")
        print(f"  [{i}/{len(rows)}] {biz['name']}: {state}, {len(result.get('issues', []))} issues")
        if biz.get("website"):
            time.sleep(delay)
    return len(rows)


def score(conn, s: Settings, use_llm: bool = True) -> int:
    llm = LLM(s, conn) if (use_llm and s.llm_enabled) else None
    rows = conn.execute("SELECT * FROM businesses").fetchall()
    reviewed = 0
    for row in rows:
        biz = row_to_biz(row)
        audit_row = conn.execute("SELECT data_json, audited_at FROM audits WHERE business_id = ?", (biz["id"],)).fetchone()
        aud = load_json(audit_row)
        result = score_business(biz, aud).as_dict()
        result["audited_at"] = audit_row["audited_at"] if audit_row else None
        previous = load_json(conn.execute("SELECT data_json FROM scores WHERE business_id = ?", (biz["id"],)).fetchone())

        # Only spend LLM tokens on leads that are close to (or over) the threshold, and only
        # reuse an earlier review if it was based on the same audit.
        llm_review = (previous or {}).get("llm") if (previous or {}).get("audited_at") == result["audited_at"] else None
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


def _slug(name: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:50]


def load_research(conn, business_id: int) -> dict | None:
    return load_json(conn.execute("SELECT data_json FROM research WHERE business_id = ?", (business_id,)).fetchone())


def research(conn, s: Settings, top: int = 10, ids: list[int] | None = None, use_llm: bool = True,
             refresh: bool = False) -> int:
    """Deep research on the best leads (and any marked 'good'). Skips leads already researched since their last audit."""
    llm = LLM(s, conn) if (use_llm and s.llm_enabled) else None
    if ids:
        targets = []
        for business_id in ids:
            row = conn.execute("SELECT * FROM businesses WHERE id = ?", (business_id,)).fetchone()
            if row:
                aud = load_json(conn.execute("SELECT data_json FROM audits WHERE business_id = ?", (business_id,)).fetchone())
                targets.append((row_to_biz(row), aud))
    else:
        current = shortlist(conn, s.threshold)
        good = [t for t in current if t[0]["status"] == "good"]
        targets = [(b, a) for b, _, a in good + [t for t in current if t[0]["status"] != "good"][:top]]
    done = 0
    for biz, aud in targets:
        row = conn.execute(
            """SELECT r.researched_at, a.audited_at FROM research r LEFT JOIN audits a ON a.business_id = r.business_id
               WHERE r.business_id = ?""", (biz["id"],)).fetchone()
        if row and not refresh and (row["audited_at"] is None or row["researched_at"] >= row["audited_at"]):
            continue
        print(f"  Researching #{biz['id']} {biz['name']} ...")
        result = research_mod.research_business(llm, biz, aud, s.sender_email)
        save_json_row(conn, "research", biz["id"], result)
        conn.commit()
        out = s.data_dir / "research"
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{biz['id']:05d}-{_slug(biz['name'])}.md"
        path.write_text(research_mod.profile_markdown(biz, result["profile"], result["crawl"]))
        print(f"    {path} ({len(result['crawl'].get('pages', []))} pages, {result['profile'].get('generated_by', 'LLM')})")
        done += 1
    print(f"Researched {done} businesses")
    return done


def _researched_after_draft(conn, business_id: int) -> bool:
    row = conn.execute("""SELECT r.researched_at > d.created_at FROM research r JOIN drafts d ON d.business_id = r.business_id
                          WHERE r.business_id = ?""", (business_id,)).fetchone()
    return bool(row and row[0])


def draft(conn, s: Settings, top: int = 10, use_llm: bool = True, redo: bool = False) -> int:
    llm = LLM(s, conn) if (use_llm and s.llm_enabled) else None
    current = shortlist(conn, s.threshold)
    _archive_stale_drafts(conn, s, {b["id"] for b, _, _ in current})
    done = {r[0] for r in conn.execute("SELECT business_id FROM drafts")}
    count = 0
    for biz, sc, aud in current:
        if count >= top:
            break
        if biz["id"] in done and not redo and not _researched_after_draft(conn, biz["id"]):
            continue
        path = drafts.write_draft(llm, biz, aud, sc, sc.get("channel", "email"), s,
                                  (load_research(conn, biz["id"]) or {}).get("profile"))
        conn.execute("INSERT OR REPLACE INTO drafts (business_id, created_at, channel, path) VALUES (?, ?, ?, ?)",
                     (biz["id"], now(), sc.get("channel", "email"), str(path)))
        conn.commit()
        count += 1
        print(f"  Draft: {path}")
    print(f"Wrote {count} new drafts")
    return count


def _archive_stale_drafts(conn, s: Settings, shortlisted: set[int]) -> None:
    """Move drafts for leads that dropped off the shortlist (excluded, merged, re-scored) to drafts/archive/."""
    contacted = {r[0] for r in conn.execute("SELECT id FROM businesses WHERE status IN ('contacted','won')")}
    for business_id, path in conn.execute("SELECT business_id, path FROM drafts").fetchall():
        if business_id in shortlisted or business_id in contacted:
            continue
        src = Path(path)
        if src.exists():
            dest = s.drafts_dir / "archive" / src.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            src.replace(dest)
        conn.execute("DELETE FROM drafts WHERE business_id = ?", (business_id,))
    # Drafts of businesses removed by dedupe have no database row any more.
    known = {Path(r[0]).name for r in conn.execute("SELECT path FROM drafts")}
    if s.drafts_dir.exists():
        for f in s.drafts_dir.glob("*.md"):
            if f.name not in known:
                (s.drafts_dir / "archive").mkdir(parents=True, exist_ok=True)
                f.replace(s.drafts_dir / "archive" / f.name)
    conn.commit()


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


def llm_test(conn, s: Settings) -> bool:
    """Check the LLM connection end to end. Prints what works and what to fix."""
    import httpx

    print(f"Endpoint: {s.llm_base_url}")
    if not s.llm_api_key:
        print("LLM_API_KEY is not set. Add your FreeLLMAPI unified key (router dashboard → Keys) to .env.")
        return False
    headers = {"Authorization": f"Bearer {s.llm_api_key}"}
    try:
        resp = httpx.get(f"{s.llm_base_url}/models", params={"execution_status": "ready"}, headers=headers, timeout=30)
    except httpx.HTTPError as exc:
        print(f"Can't reach the router: {exc}\nIs FreeLLMAPI running, and is LLM_BASE_URL right?")
        return False
    if resp.status_code in (401, 403):
        print(f"The router rejected the key (HTTP {resp.status_code}). Check LLM_API_KEY.")
        return False
    if resp.status_code < 400:
        ids = [m.get("id") for m in resp.json().get("data", []) if m.get("id")]
        print(f"Models ready to serve right now: {len(ids)}")
        for model_id in ids[:20]:
            print(f"  {model_id}")
        if len(ids) > 20:
            print(f"  ... and {len(ids) - 20} more")
    else:
        print(f"Model list unavailable (HTTP {resp.status_code}); trying a chat call anyway.")

    llm = LLM(s, conn)
    ok = True
    for tier in ("fast", "strong"):
        models = s.llm_fast_models if tier == "fast" else s.llm_strong_models
        start = time.monotonic()
        try:
            reply = llm.chat_json(tier, "test", "You are a test endpoint.",
                                  'Return {"ok": true, "capital_of_queensland": "<answer>"}', max_tokens=300)
            print(f"{tier:6} ({', '.join(models)}): OK in {time.monotonic() - start:.1f}s via {llm.last_served_by}, "
                  f"answered {reply.get('capital_of_queensland')!r}")
        except (LLMError, ValueError) as exc:
            ok = False
            print(f"{tier:6} ({', '.join(models)}): FAILED, {exc}")
    conn.commit()

    top = shortlist(conn, s.threshold, limit=1) or shortlist(conn, 0, limit=1)
    if ok and top:
        biz, _, aud = top[0]
        print(f"\nSample lead review for #{biz['id']} {biz['name']} (not saved):")
        try:
            print(json.dumps(drafts.review(llm, biz, aud), indent=2))
        except (LLMError, ValueError) as exc:
            print(f"  failed: {exc}")
        conn.commit()
    return ok


def usage(conn) -> None:
    rows = conn.execute("""SELECT purpose, model, COUNT(*) AS calls,
            COALESCE(SUM(prompt_tokens), 0) AS prompt, COALESCE(SUM(completion_tokens), 0) AS completion
        FROM llm_usage WHERE at >= date('now', 'start of month')
        GROUP BY purpose, model ORDER BY prompt + completion DESC""").fetchall()
    total = sum(r["prompt"] + r["completion"] for r in rows)
    print(f"LLM tokens this month: {total:,}")
    for r in rows:
        print(f"  {r['purpose']:<9} {r['model'][:45]:<45} {r['calls']:>5} calls {r['prompt'] + r['completion']:>12,} tokens")
