"""The source catalog: every directory the engine can read, what each can provide, the rules it is crawled under,
and how it behaved on the last run.

There is no open-web scraping. Every field has to be grounded in a page the engine can show you, robots.txt and a
site's terms are checked per source, and a known layout is what makes de-duplication and quality checks possible.
Adding a source means adding a parser to this catalog."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from urllib.robotparser import RobotFileParser

from . import db

CAPABILITIES = {
    "phone": "phone numbers",
    "email": "email addresses",
    "listing_counts": "listing counts (rent, short-let, sale)",
    "location": "city",
    "services": "services offered",
    "register": "register membership",
    "role": "who runs the firm",
    "agent": "the listing agent",
    "name": "contact names",
}
CATALOG = [
    {
        "key": "portal",
        "name": "Property portal",
        "what": "Listing cards in two layouts, standard and premium. Some firms run two accounts on it.",
        "index": "portal/index.html",
        "provides": ["phone", "email", "listing_counts", "location", "services", "agent", "name"],
        "partial": {"email": "only on some cards"},
    },
    {
        "key": "register",
        "name": "Professional register",
        "what": "Licensed members, written surname first, with phones that lost their leading zero. No emails.",
        "index": "register/index.html",
        "provides": ["register", "phone", "location", "name"],
        "partial": {},
    },
    {
        "key": "profile",
        "name": "Firm profile pages",
        "what": "Free-text About pages, read by the extractor. Found through the portal's View profile links.",
        "index": "portal/profile/",
        "provides": ["email", "phone", "role", "services", "name"],
        "partial": {"email": "written as [at] and (dot)"},
        "requires": "portal",
    },
]
BY_KEY = {s["key"]: s for s in CATALOG}
CONTACT_CAPS = ("phone", "email")


def robots(site: Path) -> dict:
    path = site / "robots.txt"
    text = path.read_text() if path.exists() else ""
    rp = RobotFileParser()
    rp.parse(text.splitlines())
    return {
        "text": text,
        "crawl_delay": float(rp.crawl_delay("lead-engine") or 0),
        "disallowed": re.findall(r"(?im)^disallow:\s*(\S+)", text),
        "allowed": {s["key"]: rp.can_fetch("lead-engine", "/" + s["index"]) for s in CATALOG},
    }


def config(conn: sqlite3.Connection) -> dict:
    rows = {r["key"]: dict(r) for r in conn.execute("select * from source_config")}
    out = {}
    for s in CATALOG:
        r = rows.get(s["key"], {})
        out[s["key"]] = {
            "enabled": bool(r.get("enabled", 1)),
            "crawl_delay": r.get("crawl_delay"),
            "max_pages": r.get("max_pages"),
        }
    return out


def effective(cfg: dict) -> set[str]:
    """The sources a run will actually read: switched on, and with whatever they're found through also on."""
    on = {k for k, v in cfg.items() if v["enabled"]}
    return {k for k in on if not BY_KEY[k].get("requires") or BY_KEY[k]["requires"] in on}


def update(conn: sqlite3.Connection, key: str, site: Path, enabled=None, crawl_delay=None, max_pages=None, clear: tuple = ()) -> dict:
    """Change one source's switch or rules. The crawl delay never goes below what robots.txt asks for."""
    if key not in BY_KEY:
        raise LookupError(f"no source called {key}")
    cur = config(conn)[key]
    note = ""
    if enabled is not None:
        cur["enabled"] = bool(enabled)
    if "crawl_delay" in clear:
        cur["crawl_delay"] = None
    elif crawl_delay is not None:
        floor = robots(site)["crawl_delay"]
        value = max(0.0, min(60.0, float(crawl_delay)))
        if value < floor:
            note = f"robots.txt asks for at least {floor:g} s between requests, so the delay stays at {floor:g} s."
            value = floor
        cur["crawl_delay"] = value
    if "max_pages" in clear:
        cur["max_pages"] = None
    elif max_pages is not None:
        cur["max_pages"] = max(1, min(10000, int(max_pages)))
    conn.execute(
        "insert or replace into source_config (key, enabled, crawl_delay, max_pages, updated_at) values (?, ?, ?, ?, ?)",
        (key, int(cur["enabled"]), cur["crawl_delay"], cur["max_pages"], db.now()),
    )
    conn.commit()
    return {"config": cur, "note": note}


def purge(conn: sqlite3.Connection, key: str) -> int:
    """Set a switched-off source aside: its listings, pages and ledger marks go, so the next run neither uses nor
    fetches it. Switching it back on fetches it again from scratch."""
    ids = [r[0] for r in conn.execute("select id from listings where source = ?", (key,))]
    urls = [r[0] for r in conn.execute("select url from pages where source = ?", (key,))]
    if not ids and not urls:
        return 0
    for chunk in (ids[i : i + 400] for i in range(0, len(ids), 400)):
        marks = ",".join("?" * len(chunk))
        conn.execute(f"delete from matches where a in ({marks}) or b in ({marks})", chunk * 2)
        conn.execute(f"delete from ledger where key in ({marks})", chunk)
    for chunk in (urls[i : i + 400] for i in range(0, len(urls), 400)):
        marks = ",".join("?" * len(chunk))
        conn.execute(f"delete from ledger where key in ({marks})", chunk)
    conn.execute("delete from source_counts where source = ?", (key,))
    conn.execute("delete from listings where source = ?", (key,))
    conn.execute("delete from pages where source = ?", (key,))
    return len(ids)


def health(conn: sqlite3.Connection) -> dict:
    """How each source behaved: pages fetched, failed, retried; listings shown versus kept; its latest problem."""
    out = {}
    for s in CATALOG:
        key = s["key"]
        pages = conn.execute("select status, attempts from pages where source = ?", (key,)).fetchall()
        fetched = sum(1 for p in pages if p["status"] == "ok")
        if key == "profile":
            shown, kept = fetched, conn.execute("select count(*) from listings where source = 'profile'").fetchone()[0]
        else:
            row = conn.execute("select coalesce(sum(seen), 0), coalesce(sum(kept), 0) from source_counts where source = ?", (key,)).fetchone()
            shown, kept = row[0], row[1]
        last = conn.execute(
            "select level, message, at from events where source = ? and level in ('warn', 'error') order by id desc limit 1", (key,)
        ).fetchone()
        out[key] = {
            "fetched": fetched,
            "failed": sum(1 for p in pages if p["status"] == "failed"),
            "disallowed": sum(1 for p in pages if p["status"] == "disallowed"),
            "retried": sum(1 for p in pages if (p["attempts"] or 0) > 1),
            "shown": shown,
            "kept": kept,
            "listings": conn.execute("select count(*) from listings where source = ?", (key,)).fetchone()[0],
            "layouts": {r[0]: r[1] for r in conn.execute("select layout, count(*) from listings where source = ? group by layout", (key,))},
            "last_problem": dict(last) if last else None,
        }
    return out


def _names(keys: list[str]) -> str:
    names = [BY_KEY[k]["name"] for k in keys]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def coverage(cfg: dict, needs: list[dict], unsupported: list[dict] | None = None) -> dict:
    """For each ICP criterion: which switched-on sources can provide it. Gaps warn; they never block a run."""
    on = effective(cfg)
    rows, warnings = [], []
    for need in needs:
        providers = [s["key"] for s in CATALOG if set(need["caps"]) & set(s["provides"])]
        live = [k for k in providers if k in on]
        status = "covered" if live else "gap" if providers else "unsupported"
        rows.append({**need, "providers": providers, "live": live, "status": status,
                     "cap_label": " or ".join(CAPABILITIES[c] for c in need["caps"])})
        if status == "gap":
            off = [k for k in providers if k not in on]
            verb = "is" if len(off) == 1 else "are"
            fix = "Turn it on" if len(off) == 1 else "Turn one on"
            why = ""
            if any(BY_KEY[k].get("requires") and cfg[BY_KEY[k]["requires"]]["enabled"] is False for k in off):
                why = " (profile pages are found through the portal, so they need it on too)"
            warnings.append(f"Your ICP needs {rows[-1]['cap_label']} for \"{need['label']}\", but {_names(off)} {verb} off{why}. {fix} or drop the criterion.")
    for u in unsupported or []:
        warnings.append(f"Nothing the engine reads gives {u['what']} (\"{u['phrase']}\"), so scoring ignores it.")
    if cfg["profile"]["enabled"] and not cfg["portal"]["enabled"]:
        warnings.append("Firm profile pages are found through the portal's View profile links, so they are not read while the Property portal is off.")
    suggestions = sorted(
        (
            {
                "key": s["key"],
                "name": s["name"],
                "enabled": s["key"] in on,
                "covers": [n["label"] for n in needs if set(n["caps"]) & set(s["provides"])],
            }
            for s in CATALOG
        ),
        key=lambda x: (-len(x["covers"]), x["key"]),
    )
    return {"rows": rows, "warnings": warnings, "suggestions": suggestions, "of": len(needs), "blocked": blocked(cfg)}


def blocked(cfg: dict) -> str | None:
    """The one gap that stops a run: if no source that is on gives a phone or an email, nobody found could be contacted."""
    on = effective(cfg)
    if any(set(CONTACT_CAPS) & set(BY_KEY[k]["provides"]) for k in on):
        return None
    return "No source that is on gives a phone number or an email, so nobody it found could be contacted. Turn on the Property portal or the Professional register."
