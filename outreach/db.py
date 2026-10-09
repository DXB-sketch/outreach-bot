"""SQLite storage. One database file holds every stage of the pipeline."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .dedupe import find_duplicate, is_duplicate

SCHEMA = """
CREATE TABLE IF NOT EXISTS businesses (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL,
    source_id     TEXT NOT NULL,
    name          TEXT NOT NULL,
    category      TEXT,
    address       TEXT,
    lat           REAL,
    lon           REAL,
    distance_km   REAL,
    website       TEXT,
    email         TEXT,
    phone         TEXT,
    rating        REAL,
    review_count  INTEGER,
    tags_json     TEXT,
    dedupe_key    TEXT,
    website_status TEXT,  -- source | found:<method> | not_found
    status        TEXT NOT NULL DEFAULT 'new',  -- new | good | bad | contacted | won | skip
    note          TEXT,
    discovered_at TEXT NOT NULL,
    UNIQUE (source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_businesses_dedupe ON businesses (dedupe_key);

CREATE TABLE IF NOT EXISTS audits (
    business_id INTEGER PRIMARY KEY REFERENCES businesses (id),
    audited_at  TEXT NOT NULL,
    data_json   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scores (
    business_id  INTEGER PRIMARY KEY REFERENCES businesses (id),
    scored_at    TEXT NOT NULL,
    score        INTEGER NOT NULL,
    data_json    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drafts (
    business_id INTEGER PRIMARY KEY REFERENCES businesses (id),
    created_at  TEXT NOT NULL,
    channel     TEXT NOT NULL,
    path        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_usage (
    id                INTEGER PRIMARY KEY,
    at                TEXT NOT NULL,
    model             TEXT NOT NULL,
    purpose           TEXT NOT NULL,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(businesses)")}
    if "website_status" not in cols:
        conn.execute("ALTER TABLE businesses ADD COLUMN website_status TEXT")
        conn.execute("UPDATE businesses SET website_status = 'source' WHERE website IS NOT NULL")
        conn.commit()


DEDUPE_COLS = "id, source, source_id, name, lat, lon, website, phone"


def upsert_business(conn: sqlite3.Connection, biz: dict) -> bool:
    """Insert a business unless it, or a duplicate of it, is already stored.

    Returns True when a new row was inserted.
    """
    exists = conn.execute(
        "SELECT id FROM businesses WHERE source = ? AND source_id = ?", (biz["source"], biz["source_id"])
    ).fetchone()
    if exists:
        _merge_missing(conn, exists["id"], biz)
        return False
    candidates = [dict(r) for r in conn.execute(f"SELECT {DEDUPE_COLS} FROM businesses")]
    dup = find_duplicate(candidates, biz)
    if dup:
        _merge_missing(conn, dup["id"], biz)
        return False
    conn.execute(
        """
        INSERT INTO businesses (source, source_id, name, category, address, lat, lon,
            distance_km, website, website_status, email, phone, rating, review_count, tags_json,
            dedupe_key, discovered_at)
        VALUES (:source, :source_id, :name, :category, :address, :lat, :lon,
            :distance_km, :website, :website_status, :email, :phone, :rating, :review_count, :tags_json,
            :dedupe_key, :discovered_at)
        """,
        {
            "category": None, "address": None, "lat": None, "lon": None,
            "distance_km": None, "website": None, "email": None, "phone": None,
            "rating": None, "review_count": None, "dedupe_key": None,
            **biz,
            "website_status": "source" if biz.get("website") else None,
            "tags_json": json.dumps(biz.get("tags", {})),
            "discovered_at": now(),
        },
    )
    return True


MERGE_FIELDS = ("website", "email", "phone", "address", "rating", "review_count", "lat", "lon", "distance_km")


def _merge_missing(conn: sqlite3.Connection, business_id: int, biz: dict) -> None:
    """Fill empty fields on an existing row with data from another record of the same business."""
    for field in MERGE_FIELDS:
        if biz.get(field) is not None:
            conn.execute(
                f"UPDATE businesses SET {field} = ? WHERE id = ? AND {field} IS NULL",
                (biz[field], business_id),
            )
    if biz.get("website"):
        conn.execute(
            "UPDATE businesses SET website_status = 'source' WHERE id = ? AND website_status IS NULL", (business_id,)
        )
    # Prefer review data from the source that has it (Google Places).
    if biz.get("review_count") is not None:
        conn.execute(
            "UPDATE businesses SET rating = ?, review_count = ? WHERE id = ? AND COALESCE(review_count, -1) < ?",
            (biz.get("rating"), biz["review_count"], business_id, biz["review_count"]),
        )


def merge_duplicates(conn: sqlite3.Connection) -> list[tuple[int, int]]:
    """Merge duplicates already in the database into the oldest record. Returns (kept, removed) pairs."""
    rows = [dict(r) for r in conn.execute("SELECT * FROM businesses ORDER BY id")]
    kept: list[dict] = []
    merged = []
    for row in rows:
        target = next((k for k in kept if is_duplicate(k, row)), None)
        if not target:
            kept.append(row)
            continue
        _merge_missing(conn, target["id"], row)
        if row["status"] != "new" and target["status"] == "new":
            conn.execute("UPDATE businesses SET status = ?, note = COALESCE(note, ?) WHERE id = ?",
                         (row["status"], row["note"], target["id"]))
            target["status"] = row["status"]
        for field in MERGE_FIELDS:
            if target.get(field) is None:
                target[field] = row.get(field)
        for table in ("audits", "scores", "drafts"):
            conn.execute(f"DELETE FROM {table} WHERE business_id = ?", (row["id"],))
        conn.execute("DELETE FROM businesses WHERE id = ?", (row["id"],))
        merged.append((target["id"], row["id"]))
    if merged:
        # Merged rows may have gained a website or contact details, so re-audit and re-score them.
        ids = sorted({k for k, _ in merged})
        marks = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM audits WHERE business_id IN ({marks})", ids)
        conn.execute(f"DELETE FROM scores WHERE business_id IN ({marks})", ids)
    conn.commit()
    return merged


def save_json_row(conn: sqlite3.Connection, table: str, business_id: int, data: dict, **cols) -> None:
    stamp_col = {"audits": "audited_at", "scores": "scored_at"}[table]
    names = ["business_id", stamp_col, "data_json", *cols]
    values = [business_id, now(), json.dumps(data), *cols.values()]
    conn.execute(
        f"INSERT OR REPLACE INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
        values,
    )


def load_json(row: sqlite3.Row | None) -> dict | None:
    return json.loads(row["data_json"]) if row else None
