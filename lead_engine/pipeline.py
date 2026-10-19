"""Source -> extract -> enrich -> verify -> match -> score -> review.

Each stage asks the ledger what is already done, does the rest, and records each record as it finishes.
Stop it anywhere and run it again: nothing is fetched, created or looked up twice."""
from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import db
from . import icp as icp_mod
from . import sources as src
from .enrich import enrich, verify
from .extract import Extraction, extractor
from .fetch import FileTransport, Fetcher
from .match import Rec, candidate_pairs, compare
from .names import normalise_company, parse_person
from .parse import parse_portal_page, parse_register_page, profile_text
from .score import fits, score


ROLE_MAILBOXES = {"info", "hello", "enquiries", "enquiry", "contact", "admin", "office", "sales", "support"}


STAGES = ("source", "extract", "enrich", "verify", "match", "score")


class Interrupted(Exception):
    """Raised by the --stop-after drill to simulate a crash mid-run."""


@dataclass
class Budget:
    stop_after: int | None = None
    used: int = 0

    def tick(self):
        self.used += 1
        if self.stop_after is not None and self.used >= self.stop_after:
            raise Interrupted(f"stopped after {self.used} steps")


@dataclass
class Run:
    conn: sqlite3.Connection
    site: Path
    budget: Budget = field(default_factory=Budget)
    delay_scale: float = 0.0
    flaky: dict[str, int] = field(default_factory=lambda: {"portal/page-3.html": 1})
    on_step: Callable[[str, int, int], None] | None = None  # (stage, done, total), for progress displays
    on_stage: Callable[[str, str], None] | None = None  # (stage, "running" | "done"), reported by the engine itself
    pace_ms: int = 0  # slows each unit of work so a demo can be watched
    score_pace_ms: int = 0  # a rescore on its own is instant; this lets the demo show it happen
    icp: dict | None = None  # None: the latest ICP saved in this database
    icp_version: int | None = None
    source_config: dict | None = None  # None: this database's source catalog settings

    def _sources(self) -> dict:
        if self.source_config is None:
            self.source_config = src.config(self.conn)
        return self.source_config

    def _icp(self) -> tuple[int | None, dict]:
        if self.icp is None:
            self.icp_version, self.icp = icp_mod.current(self.conn)
        return self.icp_version, self.icp

    def _progress(self, stage: str, i: int, total: int, worked: bool = True):
        if self.on_step:
            self.on_step(stage, i, total)
        if worked:
            if self.pace_ms:
                time.sleep(self.pace_ms / 1000)
            self.budget.tick()

    def all(self) -> dict:
        steps = {"source": self.source, "extract": self.extract_profiles, "enrich": self.enrich,
                 "verify": self.verify, "match": self.match, "score": self.score}
        for name in STAGES:
            if self.on_stage:
                self.on_stage(name, "running")
            steps[name]()
            self.conn.commit()
            if self.on_stage:
                self.on_stage(name, "done")
        return summary(self.conn)

    def only(self, name: str) -> dict:
        """Run one stage on what earlier runs stored, e.g. re-score after the ICP changes, with nothing re-fetched."""
        steps = {"match": self.match, "score": self.score}
        if self.on_stage:
            self.on_stage(name, "running")
        steps[name]()
        self.conn.commit()
        if self.on_stage:
            self.on_stage(name, "done")
        return summary(self.conn)

    # ---------------------------------------------------------------- source
    def source(self):
        cfg = self._sources()
        on = src.effective(cfg)
        for key in [s["key"] for s in src.CATALOG if s["key"] not in on]:
            removed = src.purge(self.conn, key)
            if removed:
                db.log(self.conn, "source", f"{src.BY_KEY[key]['name']} is off: set aside its {removed} listings for this run", "warn", key)
        self.conn.commit()
        delays = {k: v["crawl_delay"] for k, v in cfg.items() if v["crawl_delay"]}
        f = Fetcher(self.conn, FileTransport(self.site, dict(self.flaky)), delay_scale=self.delay_scale, source_delays=delays)
        if "portal" in on:
            f.get("private/admin.html", "portal")  # robots.txt says no: recorded and skipped
        plan = []
        for source, parser in (("portal", parse_portal_page), ("register", parse_register_page)):
            if source not in on:
                continue
            index = f.get(f"{source}/index.html", source) or ""
            hrefs = re.findall(r'href=["\'](page-\d+\.html)["\']', index)
            cap = cfg[source]["max_pages"]
            if cap and len(hrefs) > cap:
                db.log(self.conn, "source", f"{source}: max pages is {cap}, so {len(hrefs) - cap} of {len(hrefs)} pages are left unread", "warn", source)
                hrefs = hrefs[:cap]
            plan += [(source, parser, f"{source}/{href}") for href in hrefs]
        profile_cap = cfg["profile"]["max_pages"] if "profile" in on else 0
        profiles = lambda: self.conn.execute("select count(*) from pages where source = 'profile' and status = 'ok'").fetchone()[0]  # noqa: E731
        capped = False
        for i, (source, parser, path) in enumerate(plan, 1):
            if db.done(self.conn, path, "parse"):
                self._progress("source", i, len(plan), worked=False)
                continue
            body = f.get(path, source)
            if body is None:
                self._progress("source", i, len(plan), worked=False)
                continue
            listings, seen = parser(body, path)
            for item in listings:
                self._insert(item)
                if item.get("profile_href") and "profile" in on:
                    target = f"{source}/{item['profile_href']}"
                    if profile_cap and profiles() >= profile_cap and not self.conn.execute("select 1 from pages where url = ?", (target,)).fetchone():
                        if not capped:
                            db.log(self.conn, "source", f"profile: max pages is {profile_cap}, so later profile pages are left unread", "warn", "profile")
                            capped = True
                        continue
                    f.get(target, "profile")
            self.conn.execute(
                "insert or replace into source_counts (page, source, seen, kept) values (?, ?, ?, ?)", (path, source, seen, len(listings))
            )
            if seen != len(listings):
                db.log(self.conn, "parse", f"{path}: saw {seen} listings but kept {len(listings)}", "error", source)
            db.mark(self.conn, path, "parse")
            self.conn.commit()
            self._progress("source", i, len(plan))

    def _insert(self, item: dict):
        person = parse_person(item.get("contact_raw") or "", surname_first=item["source"] == "register")
        role = {"portal": "agent", "register": "member"}.get(item["source"])
        self.conn.execute(
            """insert into listings (id, source, url, layout, company, company_norm, contact_raw, given, surname, phones, emails,
                 city, for_rent, short_let, for_sale, services, licensed, role, extracted_by)
               values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'parser') on conflict (id) do nothing""",
            (
                item["id"], item["source"], item["url"], item["layout"], item["company"], normalise_company(item["company"]),
                item.get("contact_raw"), person.given if person else None, person.surname if person else None,
                json.dumps(item["phones"]), json.dumps(item["emails"]), item.get("city"), item.get("for_rent"),
                item.get("short_let"), item.get("for_sale"), item.get("services"), item.get("licensed", 0), role,
            ),
        )

    # ---------------------------------------------------------------- extract
    def extract_profiles(self):
        run_extractor = extractor()
        if "profile" not in src.effective(self._sources()):
            return  # switched off: its pages were set aside by the source stage
        pages = self.conn.execute("select url, body from pages where source = 'profile' and status = 'ok' order by url").fetchall()
        for i, p in enumerate(pages, 1):
            if db.done(self.conn, p["url"], "extract"):
                self._progress("extract", i, len(pages), worked=False)
                continue
            company, text = profile_text(p["body"])
            ex: Extraction = run_extractor(text)
            person = parse_person(ex.contact_name or "")
            lid = "profile-" + Path(p["url"]).stem
            self.conn.execute(
                """insert into listings (id, source, url, layout, company, company_norm, contact_raw, given, surname, phones, emails,
                     services, role, extracted_by, dropped_fields) values (?, 'profile', ?, 'profile', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   on conflict (id) do nothing""",
                (
                    lid, p["url"], company, normalise_company(company), ex.contact_name, person.given if person else None,
                    person.surname if person else None, json.dumps(ex.phones), json.dumps(ex.emails), ", ".join(ex.services),
                    ex.contact_role, ex.model, json.dumps(ex.dropped),
                ),
            )
            if ex.usage:
                self.conn.execute(
                    "insert into llm_calls (listing_id, model, input_tokens, output_tokens, cache_read_tokens, cost_micros, at) values (?,?,?,?,?,?,?)",
                    (lid, ex.model, ex.usage["input_tokens"], ex.usage["output_tokens"], ex.usage["cache_read_tokens"], ex.usage["cost_micros"], db.now()),
                )
            if ex.dropped:
                db.log(self.conn, "extract", f"{lid}: dropped {len(ex.dropped)} ungrounded field(s)", "warn", "profile")
            db.mark(self.conn, p["url"], "extract")
            self.conn.commit()
            self._progress("extract", i, len(pages))

    # ---------------------------------------------------------------- enrich
    def enrich(self):
        rows = self.conn.execute("select id, given, emails, company_norm from listings order by id").fetchall()
        for i, r in enumerate(rows, 1):
            if db.done(self.conn, r["id"], "enrich"):
                self._progress("enrich", i, len(rows), worked=False)
                continue
            emails = json.loads(r["emails"])
            if emails:
                self.conn.execute(
                    "update listings set email = ?, email_source = 'listing', enrichment_trail = ? where id = ?",
                    (emails[0], json.dumps([{"provider": "-", "result": "listed on the page; no lookup needed"}]), r["id"]),
                )
            else:
                domain = (r["company_norm"].replace(" ", "") + ".example") if r["company_norm"] else None
                result, trail = enrich(r["id"], r["given"], domain)
                for t in trail:
                    if t["result"] == "timeout":
                        db.log(self.conn, "enrich", f"{t['provider']} timed out on {r['id']}; retried", "warn")
                    elif t["result"].startswith("rate limited"):
                        db.log(self.conn, "enrich", f"{t['provider']} rate-limited us on {r['id']}; backed off and retried", "warn")
                self.conn.execute(
                    "update listings set email = ?, email_source = ?, enrichment_trail = ? where id = ?",
                    (result["email"] if result else None, result["source"] if result else None, json.dumps(trail), r["id"]),
                )
            db.mark(self.conn, r["id"], "enrich")
            self.conn.commit()
            self._progress("enrich", i, len(rows))

    # ---------------------------------------------------------------- verify
    def verify(self):
        rows = self.conn.execute("select id, email from listings where email is not null order by id").fetchall()
        for i, r in enumerate(rows, 1):
            if db.done(self.conn, r["id"], "verify"):
                self._progress("verify", i, len(rows), worked=False)
                continue
            domain = r["email"].split("@")[-1]
            if self.conn.execute("select 1 from quarantined_domains where domain = ?", (domain,)).fetchone():
                status = "quarantined"
            else:
                status = verify(r["email"])
                if status == "catch_all":
                    db.log(self.conn, "verify", f"{domain} accepts any address (catch-all): flagged, domain quarantined", "warn")
                    # Accepts everything, so "valid" means nothing here. Flag it and stop trusting the domain.
                    self.conn.execute(
                        "insert or ignore into quarantined_domains (domain, reason, at) values (?, 'catch-all: accepts any address', ?)",
                        (domain, db.now()),
                    )
            self.conn.execute("update listings set email_status = ? where id = ?", (status, r["id"]))
            db.mark(self.conn, r["id"], "verify")
            self.conn.commit()
            self._progress("verify", i, len(rows))

    # ---------------------------------------------------------------- match
    def match(self):
        rows = self.conn.execute("select * from listings order by id").fetchall()
        # Role addresses (info@, hello@) belong to a firm, not a person, and providers guess them, so they never link people.
        def personal(email: str | None, status: str | None) -> list[str]:
            if not email or status != "deliverable" or email.split("@")[0] in ROLE_MAILBOXES:
                return []
            return [email]

        recs = [
            Rec(r["id"], r["company_norm"], r["given"] or "", r["surname"] or "", json.loads(r["phones"]),
                personal(r["email"], r["email_status"]), r["city"] or "")
            for r in rows
        ]
        pairs = candidate_pairs(recs)
        for i, (a, b) in enumerate(pairs, 1):
            s, reasons, decision = compare(a, b)
            if decision != "different":
                self.conn.execute(
                    "insert into matches (a, b, score, reasons, decision) values (?, ?, ?, ?, ?) on conflict (a, b) do nothing",
                    (a.id, b.id, s, json.dumps(reasons), decision),
                )
            self._progress("match", i, len(pairs), worked=False)  # reported after the work, so the bar never runs ahead
        # Shared identifiers form clusters (one contact, several listings). Fuzzy matches wait for a person.
        parent = {r.id: r.id for r in recs}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for m in self.conn.execute("select a, b from matches where decision = 'linked'"):
            parent[find(m["a"])] = find(m["b"])
        # A person's "same" decision on a review pair joins the clusters too, and survives a re-run.
        for m in self.conn.execute("select a, b from review_decisions where decision = 'merge'"):
            if m["a"] in parent and m["b"] in parent:
                parent[find(m["a"])] = find(m["b"])
        for r in recs:
            self.conn.execute("update listings set cluster_id = ? where id = ?", (find(r.id), r.id))
        pending = self.conn.execute(
            """select m.a, m.b from matches m left join review_decisions d on d.a = m.a and d.b = m.b
               where m.decision = 'review' and d.a is null"""
        ).fetchall()
        review_ids = {x for m in pending for x in (m["a"], m["b"])}
        self.conn.execute("update listings set review_status = 'ready'")
        for lid in review_ids:
            self.conn.execute("update listings set review_status = 'needs_review' where id = ?", (lid,))
        self.conn.commit()

    # ---------------------------------------------------------------- score
    def score(self):
        # Score the contact, not the listing: "licensed" comes from the register, the rental book from the portal.
        version, icp = self._icp()
        clusters: dict[str, list[dict]] = {}
        for r in self.conn.execute("select * from listings").fetchall():
            clusters.setdefault(r["cluster_id"] or r["id"], []).append(dict(r))
        for i, members in enumerate(clusters.values(), 1):
            if self.score_pace_ms:
                time.sleep(self.score_pace_ms / 1000)
            portal = [m for m in members if m["source"] == "portal"] or members
            combined = {
                "company": portal[0]["company"],
                "licensed": any(m["licensed"] for m in members),
                "for_rent": max((m["for_rent"] or 0) for m in members),
                "short_let": max((m["short_let"] or 0) for m in members),
                "for_sale": max((m["for_sale"] or 0) for m in members),
                "services": " ".join(m["services"] or "" for m in members),
                "cities": sorted({m["city"] for m in members if m["city"]}),
                "roles": sorted({m["role"] for m in members if m["role"]}),
                "has_email": any(m["email_status"] == "deliverable" for m in members),
                "has_phone": any(json.loads(m["phones"]) for m in members),
            }
            points, tier, parts = score(combined, icp)
            fit = int(fits(parts))
            for m in members:
                self.conn.execute(
                    "update listings set score = ?, tier = ?, score_breakdown = ?, icp_fit = ?, icp_version = ? where id = ?",
                    (points, tier, json.dumps(parts), fit, version, m["id"]),
                )
            self._progress("score", i, len(clusters), worked=False)  # after the work, so the bar never runs ahead
        self.conn.commit()


def summary(conn: sqlite3.Connection) -> dict:
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "pages_fetched": one("select count(*) from pages where status = 'ok'"),
        "pages_disallowed": one("select count(*) from pages where status = 'disallowed'"),
        "listings": one("select count(*) from listings"),
        "by_source": {r[0]: r[1] for r in conn.execute("select source, count(*) from listings group by source")},
        "parse_gaps": one("select count(*) from source_counts where seen != kept"),
        "with_email": one("select count(*) from listings where email is not null"),
        "deliverable": one("select count(*) from listings where email_status = 'deliverable'"),
        "catch_all_or_quarantined": one("select count(*) from listings where email_status in ('catch_all', 'quarantined')"),
        "contacts": one("select count(distinct cluster_id) from listings"),
        "linked_pairs": one("select count(*) from matches where decision = 'linked'"),
        "review_pairs": one(
            "select count(*) from matches m left join review_decisions d on d.a = m.a and d.b = m.b where m.decision = 'review' and d.a is null"
        ),
        "tier1": one("select count(distinct cluster_id) from listings where tier = 1"),
        "outside_icp": one("select count(distinct cluster_id) from listings where icp_fit = 0"),
        "dropped_fields": one("select coalesce(sum(json_array_length(dropped_fields)), 0) from listings"),
        "llm_cost_micros": one("select coalesce(sum(cost_micros), 0) from llm_calls"),
    }
