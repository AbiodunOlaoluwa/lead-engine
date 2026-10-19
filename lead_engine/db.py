"""SQLite storage. Unique keys and the ledger are what make a stopped run safe to restart:
every stage records what it finished, and every insert is keyed, so a rerun repeats nothing."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
create table if not exists pages (
  url text primary key,
  source text not null,
  status text not null,          -- ok | failed | disallowed
  attempts int not null default 0,
  body text,
  fetched_at text
);
create table if not exists listings (
  id text primary key,           -- stable: source + listing id, so a re-parse can't duplicate
  source text not null,
  url text not null,
  layout text not null,          -- standard | premium | register | profile
  company text not null,
  company_norm text not null,
  contact_raw text,
  given text, surname text,
  phones text not null default '[]',
  emails text not null default '[]',
  city text,
  for_rent int, short_let int, for_sale int,
  services text,
  licensed int not null default 0,
  extracted_by text,             -- parser | scripted | claude
  dropped_fields text not null default '[]',
  email text, email_source text, email_status text,
  enrichment_trail text not null default '[]',
  score int, tier int, score_breakdown text,
  cluster_id text,
  review_status text not null default 'ready',  -- ready | needs_review | excluded
  role text,                     -- agent (portal contact) | member (register) | principal (named as running the firm)
  icp_fit int,                   -- 1 inside the ICP, 0 fails one of its must-haves
  icp_version int                -- the ICP version that scored it
);
create table if not exists matches (
  a text not null, b text not null,
  score real not null,
  reasons text not null,
  decision text not null,        -- linked (same identifiers) | review (likely the same person) | different
  primary key (a, b)
);
create table if not exists ledger (
  key text not null,
  stage text not null,
  status text not null,
  at text not null,
  primary key (key, stage)
);
create table if not exists source_counts (
  page text primary key, source text not null, seen int not null, kept int not null
);
create table if not exists quarantined_domains (domain text primary key, reason text not null, at text not null);
create table if not exists llm_calls (
  id integer primary key, listing_id text, model text, input_tokens int, output_tokens int,
  cache_read_tokens int, cost_micros int, at text
);
create table if not exists events (id integer primary key, stage text, level text, message text, at text, source text);
create table if not exists review_decisions (
  a text not null, b text not null,
  decision text not null,        -- merge | keep_apart
  at text not null,
  primary key (a, b)
);
create table if not exists icp_versions (
  version integer primary key autoincrement,
  description text not null,
  parser text not null,
  icp text not null,             -- JSON: the criteria, their evidence and points
  created_at text not null
);
create table if not exists source_config (
  key text primary key,
  enabled int not null default 1,
  crawl_delay real,              -- seconds; never below what robots.txt asks for
  max_pages int,                 -- null: no limit
  updated_at text
);
"""

# Columns added after the first release, so an existing database upgrades in place.
MIGRATIONS = {"listings": ("role text", "icp_fit int", "icp_version int"), "events": ("source text",)}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    if str(path) != ":memory:":
        conn.execute("pragma journal_mode = wal")  # the web app reads while a run writes
    conn.executescript(SCHEMA)
    for table, columns in MIGRATIONS.items():
        have = {r[1] for r in conn.execute(f"pragma table_info({table})")}
        for col in columns:
            if col.split()[0] not in have:
                conn.execute(f"alter table {table} add column {col}")
    return conn


def done(conn: sqlite3.Connection, key: str, stage: str) -> bool:
    return conn.execute("select 1 from ledger where key = ? and stage = ?", (key, stage)).fetchone() is not None


def mark(conn: sqlite3.Connection, key: str, stage: str, status: str = "done") -> None:
    conn.execute("insert or replace into ledger (key, stage, status, at) values (?, ?, ?, ?)", (key, stage, status, now()))


_listener = None


def set_listener(fn) -> None:
    """Called with (stage, level, message) for every logged event; the web app streams these live."""
    global _listener
    _listener = fn


def log(conn: sqlite3.Connection, stage: str, message: str, level: str = "info", source: str | None = None) -> None:
    """source names the catalog source an event belongs to, so the Sources tab can show each one's last problem."""
    conn.execute("insert into events (stage, level, message, at, source) values (?, ?, ?, ?, ?)", (stage, level, message, now(), source))
    if _listener:
        _listener(stage, level, message)


def jload(v: str | None, default=None):
    return json.loads(v) if v else default
