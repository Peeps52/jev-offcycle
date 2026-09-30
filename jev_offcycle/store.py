"""A local store, so you pay Jev once per listing rather than once per run.

Without this, every run re-scores everything and you buy the same answers
again. With it, each morning's run costs only what is genuinely new, and
"what changed since yesterday" becomes a question you can ask.

Also tracks disappearance: a listing that stops appearing on its board has
almost certainly closed, and should leave your shortlist rather than sit there
looking live. Idea taken from jobleft (MIT, Blueturboguy07/jobleft), which
removes closed postings from its feed.

SQLite, stdlib, one file at ~/.jev-offcycle/listings.db.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB = Path.home() / ".jev-offcycle" / "listings.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
  id            TEXT PRIMARY KEY,
  title         TEXT NOT NULL,
  organisation  TEXT NOT NULL,
  location      TEXT,
  url           TEXT,
  source        TEXT,
  payload       TEXT NOT NULL,
  content_hash  TEXT NOT NULL,
  first_seen    TEXT NOT NULL,
  last_seen     TEXT NOT NULL,
  closed_at     TEXT
);
CREATE TABLE IF NOT EXISTS verdicts (
  listing_id    TEXT NOT NULL,
  content_hash  TEXT NOT NULL,
  verdict       TEXT NOT NULL,
  score         REAL NOT NULL,
  candidacy     REAL,
  reasons       TEXT,
  signals       TEXT,
  cost_usd      REAL,
  scored_at     TEXT NOT NULL,
  PRIMARY KEY (listing_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_last_seen ON listings(last_seen);
"""


def listing_id(l) -> str:
    """Stable across runs. URL when there is one -- it is what the employer
    considers the identity of the posting -- otherwise org+title."""
    basis = l.url.strip() if l.url else f"{l.organisation}|{l.title}"
    return hashlib.sha1(basis.encode()).hexdigest()[:16]


# Bump when the question set or scoring changes. Verdicts are cached per
# content hash, so without this a listing scored under the old VC-only
# questions would be served its stale verdict forever.
SCORING_VERSION = "2026-09-30.functions2"


def content_hash(l) -> str:
    """Changes when the POSTING changes -- or when the scoring does. A
    reworded description is a different thing to score; an unchanged one
    under unchanged questions is not worth paying for twice."""
    blob = f"{SCORING_VERSION}|{l.title}|{l.location}|{l.duration}|{l.start_date}|{l.description}"
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


def connect(path: Path = DB) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sync(con: sqlite3.Connection, listings: list, seen_sources: set[str]) -> dict:
    """Record this crawl. Returns counts of new, changed and unchanged.

    Anything previously seen from a source we just crawled successfully, but
    absent this time, is marked closed. Sources that failed are excluded from
    that judgement -- a board that 404'd today has not closed every job it
    ever had, and treating a network blip as mass closure would be worse than
    useless.
    """
    now = _now()
    counts = {"new": 0, "changed": 0, "unchanged": 0, "closed": 0}
    present: set[str] = set()

    for l in listings:
        lid, ch = listing_id(l), content_hash(l)
        present.add(lid)
        row = con.execute("SELECT content_hash FROM listings WHERE id=?", (lid,)).fetchone()
        payload = json.dumps({
            "title": l.title, "organisation": l.organisation, "location": l.location,
            "description": l.description, "duration": l.duration,
            "start_date": l.start_date, "url": l.url, "source": l.source})
        if row is None:
            con.execute(
                "INSERT INTO listings (id,title,organisation,location,url,source,"
                "payload,content_hash,first_seen,last_seen) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (lid, l.title, l.organisation, l.location, l.url, l.source,
                 payload, ch, now, now))
            counts["new"] += 1
        else:
            con.execute(
                "UPDATE listings SET last_seen=?, closed_at=NULL, payload=?, "
                "content_hash=?, title=?, location=? WHERE id=?",
                (now, payload, ch, l.title, l.location, lid))
            counts["changed" if row["content_hash"] != ch else "unchanged"] += 1

    if seen_sources:
        marks = ",".join("?" * len(seen_sources))
        rows = con.execute(
            f"SELECT id FROM listings WHERE source IN ({marks}) AND closed_at IS NULL",
            tuple(seen_sources)).fetchall()
        gone = [r["id"] for r in rows if r["id"] not in present]
        if gone:
            con.executemany("UPDATE listings SET closed_at=? WHERE id=?",
                            [(now, g) for g in gone])
            counts["closed"] = len(gone)
    con.commit()
    return counts


def needs_scoring(con: sqlite3.Connection, listings: list) -> list:
    """Only what has never been scored at its current content hash.

    This is where the money is saved: an unchanged listing already has a
    verdict, and buying it again tells you nothing.
    """
    out = []
    for l in listings:
        lid, ch = listing_id(l), content_hash(l)
        hit = con.execute(
            "SELECT 1 FROM verdicts WHERE listing_id=? AND content_hash=?",
            (lid, ch)).fetchone()
        if not hit:
            out.append(l)
    return out


def save_verdict(con: sqlite3.Connection, result) -> None:
    con.execute(
        "INSERT OR REPLACE INTO verdicts (listing_id,content_hash,verdict,score,"
        "candidacy,reasons,signals,cost_usd,scored_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (listing_id(result.listing), content_hash(result.listing), result.verdict,
         result.score, result.candidacy, json.dumps(result.reasons),
         json.dumps(result.signals), result.cost_usd, _now()))
    con.commit()


def load_verdicts(con: sqlite3.Connection, include_closed: bool = False) -> list[dict]:
    """Every current verdict, newest scoring first."""
    sql = ("SELECT l.payload, l.first_seen, l.closed_at, v.* FROM verdicts v "
           "JOIN listings l ON l.id = v.listing_id AND l.content_hash = v.content_hash")
    if not include_closed:
        sql += " WHERE l.closed_at IS NULL"
    sql += " ORDER BY v.score DESC"
    out = []
    for r in con.execute(sql).fetchall():
        d = dict(r)
        d["listing"] = json.loads(d.pop("payload"))
        d["reasons"] = json.loads(d["reasons"] or "[]")
        d["signals"] = json.loads(d["signals"] or "{}")
        out.append(d)
    return out
