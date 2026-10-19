"""Source -> extract -> enrich -> verify -> match -> score -> review.

Each stage asks the ledger what is already done, does the rest, and records each record as it finishes.
Stop it anywhere and run it again: nothing is fetched, created or looked up twice."""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import db
from .enrich import enrich, verify
from .extract import Extraction, extractor
from .fetch import FileTransport, Fetcher
from .match import Rec, candidate_pairs, compare
from .names import normalise_company, parse_person
from .parse import parse_portal_page, parse_register_page, profile_text
from .score import score


ROLE_MAILBOXES = {"info", "hello", "enquiries", "enquiry", "contact", "admin", "office", "sales", "support"}


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

    def all(self) -> dict:
        self.source()
        self.extract_profiles()
        self.enrich()
        self.verify()
        self.match()
        self.score()
        self.conn.commit()
        return summary(self.conn)

    # ---------------------------------------------------------------- source
    def source(self):
        f = Fetcher(self.conn, FileTransport(self.site, dict(self.flaky)), delay_scale=self.delay_scale)
        f.get("private/admin.html", "portal")  # robots.txt says no: recorded and skipped
        for source, parser in (("portal", parse_portal_page), ("register", parse_register_page)):
            index = f.get(f"{source}/index.html", source) or ""
            for href in re.findall(r'href=["\'](page-\d+\.html)["\']', index):
                path = f"{source}/{href}"
                if db.done(self.conn, path, "parse"):
                    continue
                body = f.get(path, source)
                if body is None:
                    continue
                listings, seen = parser(body, path)
                for item in listings:
                    self._insert(item)
                    if item.get("profile_href"):
                        f.get(f"{source}/{item['profile_href']}", "profile")
                self.conn.execute(
                    "insert or replace into source_counts (page, source, seen, kept) values (?, ?, ?, ?)", (path, source, seen, len(listings))
                )
                if seen != len(listings):
                    db.log(self.conn, "parse", f"{path}: saw {seen} listings but kept {len(listings)}", "error")
                db.mark(self.conn, path, "parse")
                self.conn.commit()
                self.budget.tick()

    def _insert(self, item: dict):
        person = parse_person(item.get("contact_raw") or "", surname_first=item["source"] == "register")
        self.conn.execute(
            """insert into listings (id, source, url, layout, company, company_norm, contact_raw, given, surname, phones, emails,
                 city, for_rent, short_let, for_sale, services, licensed, extracted_by)
               values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'parser') on conflict (id) do nothing""",
            (
                item["id"], item["source"], item["url"], item["layout"], item["company"], normalise_company(item["company"]),
                item.get("contact_raw"), person.given if person else None, person.surname if person else None,
                json.dumps(item["phones"]), json.dumps(item["emails"]), item.get("city"), item.get("for_rent"),
                item.get("short_let"), item.get("for_sale"), item.get("services"), item.get("licensed", 0),
            ),
        )

    # ---------------------------------------------------------------- extract
    def extract_profiles(self):
        run_extractor = extractor()
        pages = self.conn.execute("select url, body from pages where source = 'profile' and status = 'ok' order by url").fetchall()
        for p in pages:
            if db.done(self.conn, p["url"], "extract"):
                continue
            company, text = profile_text(p["body"])
            ex: Extraction = run_extractor(text)
            person = parse_person(ex.contact_name or "")
            lid = "profile-" + Path(p["url"]).stem
            self.conn.execute(
                """insert into listings (id, source, url, layout, company, company_norm, contact_raw, given, surname, phones, emails,
                     services, extracted_by, dropped_fields) values (?, 'profile', ?, 'profile', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   on conflict (id) do nothing""",
                (
                    lid, p["url"], company, normalise_company(company), ex.contact_name, person.given if person else None,
                    person.surname if person else None, json.dumps(ex.phones), json.dumps(ex.emails), ", ".join(ex.services),
                    ex.model, json.dumps(ex.dropped),
                ),
            )
            if ex.usage:
                self.conn.execute(
                    "insert into llm_calls (listing_id, model, input_tokens, output_tokens, cache_read_tokens, cost_micros, at) values (?,?,?,?,?,?,?)",
                    (lid, ex.model, ex.usage["input_tokens"], ex.usage["output_tokens"], ex.usage["cache_read_tokens"], ex.usage["cost_micros"], db.now()),
                )
            if ex.dropped:
                db.log(self.conn, "extract", f"{lid}: dropped {len(ex.dropped)} ungrounded field(s)", "warn")
            db.mark(self.conn, p["url"], "extract")
            self.conn.commit()
            self.budget.tick()

    # ---------------------------------------------------------------- enrich
    def enrich(self):
        rows = self.conn.execute("select id, given, emails, company_norm from listings order by id").fetchall()
        for r in rows:
            if db.done(self.conn, r["id"], "enrich"):
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
                self.conn.execute(
                    "update listings set email = ?, email_source = ?, enrichment_trail = ? where id = ?",
                    (result["email"] if result else None, result["source"] if result else None, json.dumps(trail), r["id"]),
                )
            db.mark(self.conn, r["id"], "enrich")
            self.conn.commit()
            self.budget.tick()

    # ---------------------------------------------------------------- verify
    def verify(self):
        rows = self.conn.execute("select id, email from listings where email is not null order by id").fetchall()
        for r in rows:
            if db.done(self.conn, r["id"], "verify"):
                continue
            domain = r["email"].split("@")[-1]
            if self.conn.execute("select 1 from quarantined_domains where domain = ?", (domain,)).fetchone():
                status = "quarantined"
            else:
                status = verify(r["email"])
                if status == "catch_all":
                    # Accepts everything, so "valid" means nothing here. Flag it and stop trusting the domain.
                    self.conn.execute(
                        "insert or ignore into quarantined_domains (domain, reason, at) values (?, 'catch-all: accepts any address', ?)",
                        (domain, db.now()),
                    )
            self.conn.execute("update listings set email_status = ? where id = ?", (status, r["id"]))
            db.mark(self.conn, r["id"], "verify")
            self.conn.commit()
            self.budget.tick()

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
        for a, b in candidate_pairs(recs):
            s, reasons, decision = compare(a, b)
            if decision == "different":
                continue
            self.conn.execute(
                "insert into matches (a, b, score, reasons, decision) values (?, ?, ?, ?, ?) on conflict (a, b) do nothing",
                (a.id, b.id, s, json.dumps(reasons), decision),
            )
        # Shared identifiers form clusters (one contact, several listings). Fuzzy matches wait for a person.
        parent = {r.id: r.id for r in recs}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for m in self.conn.execute("select a, b from matches where decision = 'linked'"):
            parent[find(m["a"])] = find(m["b"])
        for r in recs:
            self.conn.execute("update listings set cluster_id = ? where id = ?", (find(r.id), r.id))
        review_ids = {x for m in self.conn.execute("select a, b from matches where decision = 'review'") for x in (m["a"], m["b"])}
        for lid in review_ids:
            self.conn.execute("update listings set review_status = 'needs_review' where id = ? and review_status = 'ready'", (lid,))
        self.conn.commit()

    # ---------------------------------------------------------------- score
    def score(self):
        # Score the contact, not the listing: "licensed" comes from the register, the rental book from the portal.
        clusters: dict[str, list[dict]] = {}
        for r in self.conn.execute("select * from listings").fetchall():
            clusters.setdefault(r["cluster_id"] or r["id"], []).append(dict(r))
        for members in clusters.values():
            portal = [m for m in members if m["source"] == "portal"] or members
            combined = {
                "company": portal[0]["company"],
                "licensed": any(m["licensed"] for m in members),
                "for_rent": max((m["for_rent"] or 0) for m in members),
                "short_let": max((m["short_let"] or 0) for m in members),
                "for_sale": max((m["for_sale"] or 0) for m in members),
                "services": " ".join(m["services"] or "" for m in members),
            }
            points, tier, parts = score(combined)
            for m in members:
                self.conn.execute(
                    "update listings set score = ?, tier = ?, score_breakdown = ? where id = ?", (points, tier, json.dumps(parts), m["id"])
                )
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
        "review_pairs": one("select count(*) from matches where decision = 'review'"),
        "tier1": one("select count(distinct cluster_id) from listings where tier = 1"),
        "dropped_fields": one("select coalesce(sum(json_array_length(dropped_fields)), 0) from listings"),
        "llm_cost_micros": one("select coalesce(sum(cost_micros), 0) from llm_calls"),
    }
