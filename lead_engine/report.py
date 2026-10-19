"""A static HTML report of the latest run: what was fetched, what each page showed versus kept, one contact's
full trail, the review queue, and the scores with their reasons. No server needed; open the file."""
from __future__ import annotations

import json
import sqlite3
from html import escape as e
from pathlib import Path

from . import icp as icp_mod
from .pipeline import summary

CSS = """
:root{--bg:#fff;--tint:#f6f7f4;--sunk:#eff1ec;--ink:#0c1014;--ink2:#2b3139;--muted:#646b75;--line:#e6e8e2;--green:#0a7d4f;--gb:#1bb26c;--gt:#e8f4ee;
--amber:#a8680a;--at:#fcf1df;--red:#bb3a2a;--rt:#fbe9e6;--slate:#3d5a80;--st:#e8eef6}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 -apple-system,"Segoe UI",Inter,Roboto,sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 60px}h1,h2,h3{letter-spacing:-.02em;margin:0;text-wrap:balance}h1{font-size:30px;font-weight:800}
h2{font-size:21px;font-weight:750;margin:34px 0 12px}h3{font-size:15px}.mono{font-family:ui-monospace,Menlo,monospace;font-size:12.5px}.muted{color:var(--muted)}
header{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap;padding-bottom:16px;border-bottom:1px solid var(--line)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-top:18px}
.tile{padding:12px 14px;border-radius:12px;box-shadow:0 0 0 1px var(--line);background:#fff}.tile b{display:block;font-size:24px;letter-spacing:-.03em}.tile span{font-size:12.5px;color:var(--muted)}
.tile.ok{background:var(--gt)}.tile.held{background:var(--at)}.tile.bad{background:var(--rt)}
table{width:100%;border-collapse:collapse;font-size:13.5px;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 0 0 1px var(--line)}
th,td{text-align:left;padding:8px 10px;border-top:1px solid var(--line);vertical-align:top}th{background:var(--tint);font-weight:600;font-size:12px;color:var(--muted);border-top:0}
.scroll{overflow-x:auto}.chip{display:inline-block;font:500 11.5px/1 ui-monospace,Menlo,monospace;padding:4px 7px;border-radius:999px;white-space:nowrap}
.c-ok{background:var(--gt);color:var(--green)}.c-held{background:var(--at);color:var(--amber)}.c-bad{background:var(--rt);color:var(--red)}.c-info{background:var(--st);color:var(--slate)}
.trail{display:grid;gap:10px}.card{padding:14px 16px;border-radius:14px;box-shadow:0 0 0 1px var(--line);background:#fff;display:grid;gap:6px}
.card .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.steps{margin:4px 0 0;padding-left:18px;color:var(--ink2);font-size:13.5px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}@media(max-width:800px){.grid2{grid-template-columns:1fr}}
.pts{font-variant-numeric:tabular-nums;font-weight:600}.neg{color:var(--red)}.note{font-size:13px;color:var(--muted);margin-top:6px}
"""

STATUS_CHIP = {"deliverable": "c-ok", "catch_all": "c-bad", "quarantined": "c-bad", "invalid": "c-bad", "unknown": "c-held", None: "c-info"}


def chip(text: str, cls: str) -> str:
    return f'<span class="chip {cls}">{e(text)}</span>'


def write_report(conn: sqlite3.Connection, site: Path, out: Path) -> Path:
    s = summary(conn)
    rows = [dict(r) for r in conn.execute("select * from listings")]
    by_cluster: dict[str, list[dict]] = {}
    for r in rows:
        by_cluster.setdefault(r["cluster_id"], []).append(r)

    tiles = [
        (s["pages_fetched"], "pages fetched", ""),
        (s["pages_disallowed"], "page skipped (robots.txt)", "held" if s["pages_disallowed"] else ""),
        (s["listings"], "listings from 3 sources", ""),
        (s["parse_gaps"], "pages where we kept fewer than we saw", "ok" if s["parse_gaps"] == 0 else "bad"),
        (s["deliverable"], "deliverable emails", "ok"),
        (s["catch_all_or_quarantined"], "catch-all addresses flagged", "bad"),
        (s["contacts"], "contacts after matching", ""),
        (s["review_pairs"], "pairs waiting for review", "held"),
        (s["tier1"], "tier 1 contacts", "ok"),
        (s["outside_icp"], "outside the ICP", ""),
    ]
    html_tiles = "".join(f'<div class="tile {c}"><b>{v}</b><span>{e(l)}</span></div>' for v, l, c in tiles)

    counts = conn.execute("select * from source_counts order by source, page").fetchall()
    count_rows = "".join(
        f"<tr><td class='mono'>{e(c['page'])}</td><td>{c['source']}</td><td>{c['seen']}</td><td>{c['kept']}</td><td>{chip('all kept', 'c-ok') if c['seen'] == c['kept'] else chip('gap', 'c-bad')}</td></tr>"
        for c in counts
    )

    # The richest contact: most sources, then an enrichment lookup in its trail.
    def richness(members):
        return (len({m["source"] for m in members}), len(members), any(m["email_source"] not in (None, "listing") for m in members))

    best = max(by_cluster.values(), key=richness)
    trail_cards = []
    for m in sorted(best, key=lambda m: ("register", "portal", "profile").index(m["source"])):
        trail = json.loads(m["enrichment_trail"] or "[]")
        steps = "".join(
            f"<li><b>{e(t.get('provider', ''))}</b>: {e(t.get('result', ''))}{(' &rarr; ' + e(t['email'])) if t.get('email') else ''}</li>" for t in trail
        )
        dropped = json.loads(m["dropped_fields"] or "[]")
        trail_cards.append(
            f"""<div class="card"><div class="row"><h3>{e(m['source'].title())}</h3>{chip(m['layout'], 'c-info')}<span class="mono muted">{e(m['id'])}</span></div>
<div>Name as written: <b>{e(m['contact_raw'] or '(none)')}</b> &rarr; read as given name <b>{e((m['given'] or '').title())}</b>, surname <b>{e((m['surname'] or '').title())}</b>{' (register: surname first)' if m['source'] == 'register' else ''}</div>
<div>Firm: {e(m['company'])} &rarr; <span class="mono">{e(m['company_norm'])}</span></div>
<div>Phones: <span class="mono">{e(', '.join(json.loads(m['phones'])) or '(none)')}</span></div>
<div>Email: <span class="mono">{e(m['email'] or '(none)')}</span> {chip(m['email_status'] or 'not checked', STATUS_CHIP.get(m['email_status'], 'c-info'))} {f"<span class='muted'>via {e(m['email_source'])}</span>" if m['email_source'] else ''}</div>
{f'<ol class="steps">{steps}</ol>' if trail else ''}{f"<div class='note'>Extracted by {e(m['extracted_by'])}; {len(dropped)} ungrounded field(s) dropped.</div>" if m['source'] == 'profile' else ''}</div>"""
        )
    links = conn.execute(
        "select * from matches where a in ({0}) or b in ({0})".format(",".join("?" * len(best))), [m["id"] for m in best] * 2
    ).fetchall()
    link_rows = "".join(
        f"<tr><td class='mono'>{e(l['a'])}</td><td class='mono'>{e(l['b'])}</td><td>{chip(l['decision'], 'c-ok' if l['decision'] == 'linked' else 'c-held')}</td><td>{e('; '.join(json.loads(l['reasons'])))}</td></tr>"
        for l in links
    )
    pts = best[0]
    def mark(p: dict) -> str:
        return "&#10005;" if p.get("kind") == "outside" else "&#10003;" if p.get("kind") == "must" else f"{'+' if p['points'] > 0 else ''}{p['points']}"

    breakdown = "".join(
        f"<tr><td class='pts {'neg' if p['points'] < 0 or p.get('kind') == 'outside' else ''}'>{mark(p)}</td><td>{e(p['why'])}"
        f"{''.join(f'<div class=note>ICP: &ldquo;{e(x)}&rdquo;</div>' for x in p.get('evidence') or [])}</td></tr>"
        for p in json.loads(pts["score_breakdown"] or "[]")
    )
    version, icp = icp_mod.current(conn)

    review = conn.execute("select * from matches where decision = 'review' order by score desc").fetchall()
    names = {r["id"]: r for r in rows}
    review_rows = "".join(
        f"<tr><td>{e(names[r['a']]['contact_raw'] or '')}<div class='mono muted'>{e(names[r['a']]['company'])}</div></td>"
        f"<td>{e(names[r['b']]['contact_raw'] or '')}<div class='mono muted'>{e(names[r['b']]['company'])}</div></td>"
        f"<td class='pts'>{r['score']:.2f}</td><td>{e('; '.join(json.loads(r['reasons'])))}</td></tr>"
        for r in review[:12]
    )

    top = sorted(by_cluster.values(), key=lambda ms: (ms[0]["icp_fit"] == 0, -(ms[0]["score"] or 0)))[:10]
    top_rows = "".join(
        f"<tr><td>{e(next((m['company'] for m in ms if m['source'] == 'portal'), ms[0]['company']))}</td><td>{len(ms)}</td>"
        f"<td class='pts'>{ms[0]['score']}</td><td>{chip('tier ' + str(ms[0]['tier']), 'c-ok' if ms[0]['tier'] == 1 else 'c-held' if ms[0]['tier'] == 2 else 'c-info')}</td>"
        f"<td>{e('; '.join(p['why'] for p in json.loads(ms[0]['score_breakdown'] or '[]')))}</td></tr>"
        for ms in top
    )
    events = conn.execute("select * from events where level in ('warn', 'error') order by id limit 12").fetchall()
    event_rows = "".join(f"<tr><td>{chip(ev['level'], 'c-held' if ev['level'] == 'warn' else 'c-bad')}</td><td class='mono'>{e(ev['stage'])}</td><td>{e(ev['message'])}</td></tr>" for ev in events)
    quarantined = conn.execute("select * from quarantined_domains order by domain").fetchall()

    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lead Engine run report</title><style>{CSS}</style></head><body><div class="wrap">
<header><div><p class="mono muted">Lead Engine &middot; run report</p><h1>From three messy directories to {s['contacts']} checked contacts</h1></div>
<span class="mono muted">Fictional data. Regenerate with python -m lead_engine generate</span></header>
<div class="tiles">{html_tiles}</div>

<h2>One contact, end to end</h2>
<p class="muted">The same firm appears in {len(best)} listings across {len({m['source'] for m in best})} sources. This is how each was read, enriched, checked and joined.</p>
<div class="trail">{''.join(trail_cards)}</div>
<div class="scroll" style="margin-top:12px"><table><tr><th>Listing</th><th>Listing</th><th>Decision</th><th>Why</th></tr>{link_rows}</table></div>
<div class="grid2" style="margin-top:12px"><div class="scroll"><table><tr><th>Points</th><th>Why this contact scores {pts['score']} (tier {pts['tier']})</th></tr>{breakdown}</table></div>
<div class="card"><h3>What makes this trail trustworthy</h3><ul class="steps"><li>Phone numbers normalised before comparing: 0800…, 800… and +234… are the same number.</li>
<li>The register's SURNAME-FIRST names are read in the right order before matching.</li><li>Shared phone or personal email links listings automatically. Similar names only go to review.</li>
<li>No provider found an email? The field stays empty. Nothing is guessed.</li></ul></div></div>

<h2>Waiting for a person</h2>
<p class="muted">Likely the same person by name, but no shared phone or email. A wrong merge is worse than a duplicate, so these wait.</p>
<div class="scroll"><table><tr><th>Listing A</th><th>Listing B</th><th>Score</th><th>Why</th></tr>{review_rows}</table></div>

<h2>Top contacts</h2>
<p class="muted">Scored with ICP version {version}: &ldquo;{e(icp['description'])}&rdquo;</p>
<div class="scroll"><table><tr><th>Firm</th><th>Listings</th><th>Points</th><th>Tier</th><th>Breakdown</th></tr>{top_rows}</table></div>

<h2>Every page: listings shown versus kept</h2>
<p class="muted">A parser that skips a layout shows up here as a gap instead of silently losing half a page.</p>
<div class="scroll"><table><tr><th>Page</th><th>Source</th><th>Shown</th><th>Kept</th><th></th></tr>{count_rows}</table></div>

<div class="grid2"><div><h2>Warnings from this run</h2><div class="scroll"><table><tr><th></th><th>Stage</th><th>What happened</th></tr>{event_rows}</table></div></div>
<div><h2>Quarantined domains</h2><p class="muted">Catch-all mail servers accept any address, so "deliverable" means nothing there. Addresses on these domains are flagged, not trusted.</p>
<div class="scroll"><table><tr><th>Domain</th><th>Why</th></tr>{''.join(f"<tr><td class='mono'>{e(q['domain'])}</td><td>{e(q['reason'])}</td></tr>" for q in quarantined)}</table></div></div></div>
</div></body></html>"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page)
    return out
