"""Import leads from a CSV you put together by hand (directories, networking, referrals).

Columns (header row required; only `name` is mandatory):
name, category, address, website, email, phone, rating, review_count, lat, lon, note
"""

from __future__ import annotations

import csv
import hashlib
from collections.abc import Iterator
from pathlib import Path

from . import dedupe_key, haversine_km, normalise_website


def _num(value: str | None, cast):
    try:
        return cast(value) if value not in (None, "") else None
    except ValueError:
        return None


def discover(path: Path, centre: tuple[float, float] | None = None) -> Iterator[dict]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            name = row.get("name")
            if not name:
                continue
            lat, lon = _num(row.get("lat"), float), _num(row.get("lon"), float)
            website = normalise_website(row.get("website"))
            distance = None
            if centre and lat is not None and lon is not None:
                distance = round(haversine_km(centre[0], centre[1], lat, lon), 1)
            yield {
                "source": "csv",
                "source_id": hashlib.sha1(f"{name}|{row.get('address', '')}".encode()).hexdigest()[:16],
                "name": name,
                "category": f"csv={row['category']}" if row.get("category") else None,
                "address": row.get("address") or None,
                "lat": lat,
                "lon": lon,
                "distance_km": distance,
                "website": website,
                "email": row.get("email") or None,
                "phone": row.get("phone") or None,
                "rating": _num(row.get("rating"), float),
                "review_count": _num(row.get("review_count"), int),
                "tags": {"note": row.get("note", "")},
                "dedupe_key": dedupe_key(name, website, lat, lon),
            }
