"""Everything the web app does, as plain functions: background jobs with progress, and the queries behind each screen.
The CLI and the web app share the same pipeline; nothing here changes how a run works."""
from __future__ import annotations

import csv
import io
import json
import shutil
import sqlite3
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from . import db
from . import icp as icp_mod
from . import sources as src
from .directory import generate
from .names import parse_person
from .pipeline import STAGES, Budget, Interrupted, Run, summary

DATA = Path("data")
SITE = DATA / "site"
DB_PATH = DATA / "lead-engine.db"
EVAL_RESULTS = DATA / "eval-results.json"


# ---------------------------------------------------------------- jobs

def _blank_stages() -> dict:
    return {s: {"done": 0, "total": 0, "state": "pending", "warn": 0, "error": 0, "last": ""} for s in STAGES}


@dataclass
class Job:
    kind: str = "none"
    state: str = "idle"  # idle | running | done | interrupted | failed
    stages: dict = field(default_factory=_blank_stages)
    current: str | None = None
    phase: str = ""
    message: str = ""
    result: dict | None = None
    events: list = field(default_factory=list)
    started: float = 0.0
    finished: float = 0.0


class Jobs:
    """One job at a time, on a background thread. Every change bumps a version and wakes anyone waiting,
    so the UI is pushed the engine's real state the moment it changes: it never guesses and never lags."""

    def __init__(self):
        self.cond = threading.Condition()
        self.version = 0
        self.job = Job()

    def _bump(self):
        self.version += 1
        self.cond.notify_all()

    def _snap(self) -> dict:
        j = self.job
        return {
            "version": self.version, "kind": j.kind, "state": j.state, "stages": json.loads(json.dumps(j.stages)),
            "current": j.current, "phase": j.phase, "message": j.message, "result": j.result, "events": j.events[-40:],
            "seconds": round((j.finished or time.time()) - j.started, 1) if j.started else 0,
        }

    def snapshot(self) -> dict:
        with self.cond:
            return self._snap()

    def wait(self, last_version: int, timeout: float) -> tuple[int, dict]:
        """Block until something changes (or timeout, as a heartbeat), then return the new state."""
        with self.cond:
            self.cond.wait_for(lambda: self.version != last_version, timeout=timeout)
            return self.version, self._snap()

    def busy(self) -> bool:
        return self.job.state == "running"

    def start(self, kind: str, fn) -> bool:
        with self.cond:
            if self.job.state == "running":
                return False
            self.job = Job(kind=kind, state="running", started=time.time())
            self._bump()

        def target():
            db.set_listener(self.event)
            try:
                fn(self)
            except Interrupted as e:
                with self.cond:
                    stage = self.job.current
                    if stage:
                        st = self.job.stages[stage]
                        st["state"] = "stopped"
                        st["error"] += 1
                        st["last"] = f"crashed here, at step {st['done']} of {st['total']}"
                    self.job.events.append({"stage": stage or "-", "level": "error", "message": f"Simulated crash in {stage} ({e}). Resume picks up from here."})
                    self.job.state, self.job.finished = "interrupted", time.time()
                    self.job.message = f"Crashed in {stage}, at step {self.job.stages[stage]['done']} of {self.job.stages[stage]['total']}. Press Resume to carry on from exactly there." if stage else "Crashed."
                    self._bump()
            except Exception as e:  # shown in the UI rather than lost in a thread
                traceback.print_exc()
                with self.cond:
                    if self.job.current:
                        self.job.stages[self.job.current]["state"] = "stopped"
                    self.job.state, self.job.finished, self.job.message = "failed", time.time(), f"{type(e).__name__}: {e}"
                    self._bump()
            finally:
                db.set_listener(None)

        threading.Thread(target=target, daemon=True).start()
        return True

    # Called by the engine as it works.
    def step(self, stage: str, i: int, total: int):
        with self.cond:
            self.job.current = stage
            st = self.job.stages[stage]
            st["done"], st["total"] = i, total
            if st["state"] == "pending":
                st["state"] = "running"
            self._bump()

    def stage(self, stage: str, state: str):
        with self.cond:
            st = self.job.stages[stage]
            st["state"] = state
            if state == "running":
                self.job.current = stage
            if state == "done" and st["total"] == 0:
                st["done"] = st["total"] = 0  # nothing to do in this stage (e.g. all done on an earlier run)
            self._bump()

    def event(self, stage: str, level: str, message: str):
        if level not in ("warn", "error"):
            return
        with self.cond:
            key = stage if stage in self.job.stages else {"fetch": "source", "parse": "source"}.get(stage)
            if key:
                self.job.stages[key][level] += 1
                self.job.stages[key]["last"] = message
            self.job.events.append({"stage": stage, "level": level, "message": message})
            self.job.events = self.job.events[-60:]
            self._bump()

    def keep_others(self, stage: str):
        """For a one-stage job (a rescore): every other stage keeps what the last run stored, and says so."""
        with self.cond:
            for name, st in self.job.stages.items():
                if name != stage:
                    st["state"] = "kept"
            self._bump()

    def set_phase(self, text: str):
        with self.cond:
            self.job.phase = text
            self._bump()

    def reset_stages(self):
        with self.cond:
            self.job.stages = _blank_stages()
            self.job.current = None
            self._bump()

    def done(self, message: str = "", result: dict | None = None):
        with self.cond:
            self.job.state, self.job.message, self.job.finished = "done", message, time.time()
            if result is not None:
                self.job.result = result
            self._bump()


JOBS = Jobs()


def ensure_site():
    if not (SITE / "truth.json").exists():
        generate(SITE)


def run_blocked() -> str | None:
    return src.blocked(src.config(conn()))


def _tiers(c: sqlite3.Connection) -> dict:
    rows = c.execute("select cluster_id, min(tier) as tier, min(icp_fit) as fit from listings group by cluster_id").fetchall()
    return {
        "tier1": sum(1 for r in rows if r["tier"] == 1),
        "tier2": sum(1 for r in rows if r["tier"] == 2),
        "tier3": sum(1 for r in rows if r["tier"] == 3),
        "outside": sum(1 for r in rows if r["fit"] == 0),
    }


def rescore_job(pace_ms: int):
    """Re-score every contact with the current ICP. Only the score stage runs: nothing is fetched or looked up."""

    def fn(jobs: Jobs):
        c = db.connect(DB_PATH)
        version, _ = icp_mod.current(c)
        before = _tiers(c)
        jobs.keep_others("score")
        jobs.set_phase(f"Scoring with ICP version {version}. Every other stage keeps what the last run stored.")
        result = Run(c, SITE, on_step=jobs.step, on_stage=jobs.stage, score_pace_ms=pace_ms).only("score")
        after = _tiers(c)
        result = result | {"rescore": {"version": version, "before": before, "after": after}}
        jobs.set_phase("")
        jobs.done(
            f"Rescored with ICP version {version}. Tier 1: {before['tier1']} to {after['tier1']}. "
            f"Outside the ICP: {before['outside']} to {after['outside']}. Nothing was fetched or looked up.",
            result,
        )

    return fn


def run_job(stop_after: int | None, pace_ms: int, fresh: bool):
    def fn(jobs: Jobs):
        ensure_site()
        if fresh:
            reset_db()
        conn = db.connect(DB_PATH)
        jobs.set_phase("Crash simulation: will stop after %d steps" % stop_after if stop_after else "")
        result = Run(conn, SITE, Budget(stop_after), on_step=jobs.step, on_stage=jobs.stage, pace_ms=pace_ms).all()
        jobs.done("Finished. Every stage reported done and every record is in the ledger.", result)

    return fn


def drill_job(jobs: Jobs):
    """Crash a run three times, resume it, and compare with a clean run, on a scratch database."""
    ensure_site()
    main = conn()
    version, icp = icp_mod.current(main)
    settings = {"icp": icp, "icp_version": version, "source_config": src.config(main)}  # the drill uses the same targeting
    scratch = DATA / "drill.db"
    for p in (scratch, Path(str(scratch) + "-wal"), Path(str(scratch) + "-shm")):
        p.unlink(missing_ok=True)
    conn = db.connect(scratch)
    stops = []
    plan = (12, 45, 90)
    for n, stop_after in enumerate(plan, 1):
        jobs.reset_stages()
        jobs.set_phase(f"Attempt {n} of {len(plan) + 1}: will crash after {stop_after} steps")
        try:
            Run(conn, SITE, Budget(stop_after), on_step=jobs.step, on_stage=jobs.stage, pace_ms=8, **settings).all()
            break
        except Interrupted:
            stops.append(stop_after)
            jobs.event(jobs.job.current or "source", "error", f"Attempt {n}: crashed after {stop_after} steps, as planned")
            conn.commit()
    jobs.reset_stages()
    jobs.set_phase(f"Attempt {len(plan) + 1} of {len(plan) + 1}: resume and finish")
    final = Run(conn, SITE, on_step=jobs.step, on_stage=jobs.stage, pace_ms=8, **settings).all()
    jobs.set_phase("Comparing with a run that never stopped")
    clean = Run(db.connect(":memory:"), SITE, **settings).all()
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    checks = {
        "listings": one("select count(*) from listings"),
        "distinct_listing_ids": one("select count(distinct id) from listings"),
        "pages_fetched_twice": one("select count(*) from (select url from pages group by url having count(*) > 1)"),
        "enrichment_lookups_repeated": one("select count(*) from (select key from ledger where stage = 'enrich' group by key having count(*) > 1)"),
    }
    differences = {k: [final[k], clean[k]] for k in final if final[k] != clean[k]}
    jobs.done(
        "Same result as an uninterrupted run." if not differences else "Different from a clean run.",
        {"stopped_at": stops, "checks": checks, "differences": differences, "summary": final},
    )


def eval_job(jobs: Jobs):
    from evals.run_evals import compute

    result = compute(progress=jobs.set_phase)
    DATA.mkdir(exist_ok=True)
    EVAL_RESULTS.write_text(json.dumps(result, indent=1))
    jobs.done("Evals finished.", {k: v for k, v in result.items() if k != "markdown"})


def generate_job(size: int, seed: int):
    def fn(jobs: Jobs):
        jobs.set_phase(f"Generating {size} fictional firms (seed {seed})")
        if SITE.exists():
            shutil.rmtree(SITE)
        meta = generate(SITE, n=size, seed=seed)
        reset_db()
        jobs.done(f"New directory: {len(meta['firms'])} firms, {len(meta['truth'])} listings. Run the pipeline to process it.")

    return fn


RESULT_TABLES = ("pages", "listings", "matches", "ledger", "source_counts", "quarantined_domains", "llm_calls", "events", "review_decisions")


def reset_db():
    """Clear the results. The ICP versions and the source settings are yours, so they stay."""
    if not DB_PATH.exists():
        return
    c = db.connect(DB_PATH)
    for table in RESULT_TABLES:
        c.execute(f"delete from {table}")
    c.commit()
    c.close()


# ---------------------------------------------------------------- queries

def conn() -> sqlite3.Connection:
    return db.connect(DB_PATH)


def site_info() -> dict:
    meta = json.loads((SITE / "truth.json").read_text()) if (SITE / "truth.json").exists() else {"firms": [], "truth": {}}
    return {"firms": len(meta["firms"]), "listings": len(meta["truth"])}


def display_name(members: list[dict]) -> str:
    for source in ("portal", "profile", "register"):
        for m in members:
            if m["source"] == source and m["contact_raw"]:
                p = parse_person(m["contact_raw"], surname_first=source == "register")
                return p.display if p else m["contact_raw"]
    return "(no name)"


def _clusters(c: sqlite3.Connection) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in c.execute("select * from listings order by id"):
        out.setdefault(r["cluster_id"] or r["id"], []).append(dict(r))
    return out


def contacts(c: sqlite3.Connection, q: str = "") -> list[dict]:
    rows = []
    for cid, ms in _clusters(c).items():
        firm = next((m["company"] for m in ms if m["source"] == "portal"), ms[0]["company"])
        email = next((m for m in ms if m["email_status"] == "deliverable"), None) or next((m for m in ms if m["email"]), None)
        row = {
            "id": cid,
            "name": display_name(ms),
            "firm": firm,
            "city": next((m["city"] for m in ms if m["city"]), ""),
            "tier": ms[0]["tier"],
            "score": ms[0]["score"],
            "email": email["email"] if email else None,
            "email_status": email["email_status"] if email else None,
            "phones": sorted({p for m in ms for p in json.loads(m["phones"])}),
            "sources": sorted({m["source"] for m in ms}),
            "listings": len(ms),
            "needs_review": any(m["review_status"] == "needs_review" for m in ms),
            "outside_icp": any(m["icp_fit"] == 0 for m in ms),
        }
        if q and q.lower() not in f"{row['name']} {row['firm']} {row['city']}".lower():
            continue
        rows.append(row)
    return sorted(rows, key=lambda r: (r["outside_icp"], -(r["score"] or 0), r["firm"]))  # inside the ICP first


def contact_detail(c: sqlite3.Connection, cid: str) -> dict | None:
    ms = _clusters(c).get(cid)
    if not ms:
        return None
    ids = [m["id"] for m in ms]
    marks = ",".join("?" * len(ids))
    links = [dict(r) | {"reasons": json.loads(r["reasons"])} for r in c.execute(f"select * from matches where a in ({marks}) or b in ({marks})", ids * 2)]
    listings = []
    for m in sorted(ms, key=lambda m: ("register", "portal", "profile").index(m["source"])):
        p = parse_person(m["contact_raw"] or "", surname_first=m["source"] == "register")
        listings.append(
            {
                "id": m["id"], "source": m["source"], "layout": m["layout"], "url": m["url"], "company": m["company"],
                "company_norm": m["company_norm"], "contact_raw": m["contact_raw"], "read_as": p.display if p else None,
                "phones": json.loads(m["phones"]), "email": m["email"], "email_status": m["email_status"],
                "email_source": m["email_source"], "trail": json.loads(m["enrichment_trail"] or "[]"),
                "extracted_by": m["extracted_by"], "dropped": json.loads(m["dropped_fields"] or "[]"),
            }
        )
    return {
        "id": cid, "name": display_name(ms), "score": ms[0]["score"], "tier": ms[0]["tier"],
        "breakdown": json.loads(ms[0]["score_breakdown"] or "[]"), "listings": listings, "links": links,
        "icp_version": ms[0]["icp_version"], "outside_icp": ms[0]["icp_fit"] == 0,
    }


def _listing_card(c: sqlite3.Connection, lid: str) -> dict:
    m = dict(c.execute("select * from listings where id = ?", (lid,)).fetchone())
    p = parse_person(m["contact_raw"] or "", surname_first=m["source"] == "register")
    return {
        "id": lid, "source": m["source"], "name": m["contact_raw"], "read_as": p.display if p else None, "company": m["company"],
        "city": m["city"], "phones": json.loads(m["phones"]), "email": m["email"],
    }


def review_queue(c: sqlite3.Connection) -> dict:
    pending = c.execute(
        """select m.* from matches m left join review_decisions d on d.a = m.a and d.b = m.b
           where m.decision = 'review' and d.a is null order by m.score desc"""
    ).fetchall()
    decided = c.execute(
        "select d.*, m.score, m.reasons from review_decisions d join matches m on m.a = d.a and m.b = d.b order by d.at desc"
    ).fetchall()
    shape = lambda r: {"a": _listing_card(c, r["a"]), "b": _listing_card(c, r["b"]), "score": r["score"], "reasons": json.loads(r["reasons"])}  # noqa: E731
    return {
        "pending": [shape(r) for r in pending],
        "decided": [shape(r) | {"decision": r["decision"], "at": r["at"]} for r in decided],
    }


def decide(c: sqlite3.Connection, a: str, b: str, decision: str) -> dict:
    a, b = sorted((a, b))
    if decision not in ("merge", "keep_apart"):
        raise ValueError("decision must be merge or keep_apart")
    if not c.execute("select 1 from matches where a = ? and b = ? and decision = 'review'", (a, b)).fetchone():
        raise LookupError("no such pair in the review queue")
    c.execute("insert or replace into review_decisions (a, b, decision, at) values (?, ?, ?, ?)", (a, b, decision, db.now()))
    db.log(c, "review", f"{'Merged' if decision == 'merge' else 'Kept apart'}: {a} and {b}")
    run = Run(c, SITE)
    run.match()  # re-cluster with the decision applied, then re-score the affected contacts
    run.score()
    return summary(c)


def export_csv(c: sqlite3.Connection) -> tuple[str, int, int, int]:
    """One row per contact. Contacts still waiting for review are held back, the way a CRM import should be, and so
    are contacts outside the ICP. Returns (csv, exported, held for review, outside the ICP)."""
    rows = contacts(c)
    ready = [r for r in rows if not r["needs_review"] and not r["outside_icp"]]
    held = sum(1 for r in rows if r["needs_review"])
    outside = sum(1 for r in rows if r["outside_icp"] and not r["needs_review"])
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["name", "firm", "city", "phones", "email", "email_status", "tier", "score", "sources", "listings"])
    for r in ready:
        w.writerow([r["name"], r["firm"], r["city"], " ".join(r["phones"]), r["email"] or "", r["email_status"] or "", r["tier"], r["score"], " ".join(r["sources"]), r["listings"]])
    return buf.getvalue(), len(ready), held, outside


def pages(c: sqlite3.Connection) -> list[dict]:
    return [
        dict(r)
        for r in c.execute(
            """select p.url, p.source, p.status, p.attempts, s.seen, s.kept from pages p
               left join source_counts s on s.page = p.url order by p.source, p.url"""
        )
    ]


def events(c: sqlite3.Connection, limit: int = 40) -> list[dict]:
    return [dict(r) for r in c.execute("select * from events order by id desc limit ?", (limit,))]


def state() -> dict:
    c = conn()
    s = summary(c)
    ready = sum(1 for r in contacts(c) if not r["needs_review"] and not r["outside_icp"]) if s["listings"] else 0
    version, icp = icp_mod.current(c)
    scored_with = c.execute("select max(icp_version) from listings").fetchone()[0]
    return {
        "summary": s,
        "ready_to_export": ready,
        "site": site_info(),
        "job": JOBS.snapshot(),
        "mode": "claude" if icp_mod.claude_on() else "regex baseline",
        "has_run": s["listings"] > 0,
        "evals": json.loads(EVAL_RESULTS.read_text()) if EVAL_RESULTS.exists() else None,
        "icp": {"version": version, "description": icp["description"], "scored_with": scored_with},
        "blocked": src.blocked(src.config(c)),
    }


# ---------------------------------------------------------------- targeting: ICP and sources

def _coverage(c: sqlite3.Connection, icp: dict) -> dict:
    return src.coverage(src.config(c), icp_mod.needs(icp), icp.get("unsupported"))


def icp_state(c: sqlite3.Connection) -> dict:
    version, icp = icp_mod.current(c)
    scored_with = c.execute("select max(icp_version) from listings").fetchone()[0]
    return {
        "version": version,
        "icp": icp,
        "versions": icp_mod.versions(c),
        "coverage": _coverage(c, icp),
        "scored_with": scored_with,
        "parser": "claude" if icp_mod.claude_on() else "baseline",
        "examples": icp_mod.EXAMPLES,
        "labels": icp_mod.LABELS,
        "order": list(icp_mod.ORDER),
        "vocab": {"cities": list(icp_mod.CITIES), "firm_types": list(icp_mod.FIRM_TYPES), "roles": list(icp_mod.ROLES), "channels": list(icp_mod.CHANNELS)},
    }


def parse_icp(c: sqlite3.Connection, description: str) -> dict:
    """Read a description into a draft ICP. Nothing is saved until you press Save."""
    draft, usage = icp_mod.parse(description)
    if usage:
        c.execute(
            "insert into llm_calls (listing_id, model, input_tokens, output_tokens, cache_read_tokens, cost_micros, at) values (?,?,?,?,?,?,?)",
            ("icp", draft["parser"], usage["input_tokens"], usage["output_tokens"], usage["cache_read_tokens"], usage["cost_micros"], db.now()),
        )
        c.commit()
    return {"icp": draft, "coverage": _coverage(c, draft), "cost_micros": (usage or {}).get("cost_micros", 0)}


def save_icp(c: sqlite3.Connection, data: dict) -> dict:
    version = icp_mod.save(c, data)
    db.log(c, "icp", f"Saved ICP version {version}")
    c.commit()
    return icp_state(c)


def coverage_for(c: sqlite3.Connection, data: dict) -> dict:
    return _coverage(c, icp_mod.normalise(data))


def sources_state(c: sqlite3.Connection) -> dict:
    cfg = src.config(c)
    rb = src.robots(SITE)
    on = src.effective(cfg)
    hl = src.health(c)
    _, icp = icp_mod.current(c)
    cov = _coverage(c, icp)
    needed = {cap for row in cov["rows"] for cap in row["caps"]}
    items = []
    for s in src.CATALOG:
        k = s["key"]
        items.append(
            {
                **{x: s[x] for x in ("key", "name", "what", "index")},
                "requires": s.get("requires"),
                "enabled": cfg[k]["enabled"],
                "reading": k in on,
                "crawl_delay": cfg[k]["crawl_delay"],
                "max_pages": cfg[k]["max_pages"],
                "robots_allowed": rb["allowed"][k],
                "capabilities": [
                    {"key": cap, "label": src.CAPABILITIES[cap], "needed": cap in needed, "partial": s["partial"].get(cap)} for cap in s["provides"]
                ],
                "health": hl[k],
            }
        )
    return {
        "sources": items,
        "robots": {"crawl_delay": rb["crawl_delay"], "disallowed": rb["disallowed"], "text": rb["text"]},
        "coverage": cov,
        "icp_version": icp_mod.current(c)[0],
    }


def update_source(c: sqlite3.Connection, key: str, body: dict) -> dict:
    clear = tuple(k for k in ("crawl_delay", "max_pages") if k in body and body[k] is None)
    res = src.update(c, key, SITE, enabled=body.get("enabled"), crawl_delay=body.get("crawl_delay"), max_pages=body.get("max_pages"), clear=clear)
    if "enabled" in body:
        name = src.BY_KEY[key]["name"]
        db.log(c, "source", f"{name} switched {'on' if body['enabled'] else 'off'}. The next run {'reads it' if body['enabled'] else 'sets it aside'}.", source=key)
        c.commit()
    return sources_state(c) | {"note": res["note"]}
