"""SQLite storage. One database file holds every stage of the pipeline."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

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
    return conn


def upsert_business(conn: sqlite3.Connection, biz: dict) -> bool:
    """Insert a business unless it (or a duplicate from another source) exists.

    Returns True when a new row was inserted.
    """
    if biz.get("dedupe_key"):
        dup = conn.execute(
            "SELECT id, source FROM businesses WHERE dedupe_key = ? AND source != ?",
            (biz["dedupe_key"], biz["source"]),
        ).fetchone()
        if dup:
            _merge_missing(conn, dup["id"], biz)
            return False
    cur = conn.execute(
        """
        INSERT INTO businesses (source, source_id, name, category, address, lat, lon,
            distance_km, website, email, phone, rating, review_count, tags_json,
            dedupe_key, discovered_at)
        VALUES (:source, :source_id, :name, :category, :address, :lat, :lon,
            :distance_km, :website, :email, :phone, :rating, :review_count, :tags_json,
            :dedupe_key, :discovered_at)
        ON CONFLICT (source, source_id) DO NOTHING
        """,
        {
            "category": None, "address": None, "lat": None, "lon": None,
            "distance_km": None, "website": None, "email": None, "phone": None,
            "rating": None, "review_count": None, "dedupe_key": None,
            **biz,
            "tags_json": json.dumps(biz.get("tags", {})),
            "discovered_at": now(),
        },
    )
    return cur.rowcount > 0


def _merge_missing(conn: sqlite3.Connection, business_id: int, biz: dict) -> None:
    """Fill empty fields on an existing row with data from another source."""
    for field in ("website", "email", "phone", "address", "rating", "review_count"):
        if biz.get(field) is not None:
            conn.execute(
                f"UPDATE businesses SET {field} = ? WHERE id = ? AND {field} IS NULL",
                (biz[field], business_id),
            )


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
