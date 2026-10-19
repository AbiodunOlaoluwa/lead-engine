"""Measure the parts that make judgement calls, so changes are compared on numbers, not impressions.

1. Matching, end to end: run the pipeline on the generated directory and score its links against the truth.
2. Extraction on every generated profile page, and on hand-written hard cases.
3. Name matching on hand-written traps (short forms, surname-first registers, common surnames).

Writes evals/results.md. Claude is evaluated too when LEAD_ENGINE_MODE=anthropic and a key is set."""
from __future__ import annotations

import itertools
import json
import os
from pathlib import Path

from lead_engine import db
from lead_engine.directory import generate
from lead_engine.extract import Extraction, extract_claude, extract_scripted
from lead_engine.names import fold, given_names_compatible, normalise_phones, parse_person
from lead_engine.parse import profile_text
from lead_engine.pipeline import Run

HERE = Path(__file__).parent
SITE = Path("data/eval-site")


def pr(tp: int, fp: int, fn: int) -> tuple[str, str]:
    p = tp / (tp + fp) if tp + fp else 1.0
    r = tp / (tp + fn) if tp + fn else 1.0
    return f"{p:.0%}", f"{r:.0%}"


# ------------------------------------------------------------------ 1. matching vs truth

def eval_matching(meta: dict) -> tuple[list[str], dict]:
    conn = db.connect(":memory:")
    Run(conn, SITE).all()
    truth = meta["truth"]
    ids = [r["id"] for r in conn.execute("select id from listings")]
    cluster = {r["id"]: r["cluster_id"] for r in conn.execute("select id, cluster_id from listings")}
    review = {(r["a"], r["b"]) for r in conn.execute("select a, b from matches where decision = 'review'")}
    tp = fp = fn = rtp = rfp = surfaced = 0
    wrong_links = []
    for a, b in itertools.combinations(sorted(ids), 2):
        same = truth.get(a) is not None and truth.get(a) == truth.get(b)
        linked = cluster[a] == cluster[b]
        in_review = (a, b) in review
        if linked and same:
            tp += 1
        elif linked and not same:
            fp += 1
            wrong_links.append((a, b))
        elif same:
            fn += 1
        if in_review:
            rtp += same
            rfp += not same
        if same and (linked or in_review):
            surfaced += 1
    p, r = pr(tp, fp, fn)
    total_same = tp + fn
    rp, _ = pr(rtp, rfp, 0)
    metrics = {
        "listings": len(ids),
        "true_contacts": len(set(truth[i] for i in ids)),
        "link_precision": p,
        "wrong_links": fp,
        "link_recall": r,
        "linked_true_pairs": tp,
        "true_pairs": total_same,
        "review_precision": rp,
        "review_true": rtp,
        "review_total": rtp + rfp,
        "surfaced": f"{surfaced / total_same:.0%}",
        "surfaced_n": surfaced,
    }
    rows = [
        "| Measure | Result |",
        "| --- | --- |",
        f"| Listings / true contacts | {len(ids)} / {len(set(truth[i] for i in ids))} |",
        f"| Automatic links: precision (wrong merges are the costly error) | **{p}** ({fp} wrong) |",
        f"| Automatic links: recall | {r} ({tp} of {total_same} true pairs) |",
        f"| Sent to review: share that really were the same | {rp} ({rtp} of {rtp + rfp}) |",
        f"| True duplicates surfaced (linked or sent to review) | **{surfaced / total_same:.0%}** ({surfaced} of {total_same}) |",
    ]
    return rows, {**metrics, "wrong_link_examples": wrong_links[:5]}


# ------------------------------------------------------------------ 2. extraction

def _field_scores(ex: Extraction, want: dict, counts: dict):
    got_name = fold(ex.contact_name or "")
    want_name = fold(want.get("contact_name") or "")
    if want_name:
        wp, gp = parse_person(want_name), parse_person(got_name) if got_name else None
        ok = bool(gp) and gp.surname == wp.surname and (gp.given == wp.given or given_names_compatible(gp.given, wp.given) or not wp.given)
        counts["name_tp" if ok else "name_fn"] += 1
        if got_name and not ok:
            counts["name_fp"] += 1
    elif got_name:
        counts["name_fp"] += 1  # named a person where there is none (e.g. a building)
    for kind in ("phones", "emails"):
        want_set = set(want.get(kind) or [])
        got_set = set(ex.phones if kind == "phones" else ex.emails)
        counts[f"{kind}_tp"] += len(got_set & want_set)
        counts[f"{kind}_fp"] += len(got_set - want_set)
        counts[f"{kind}_fn"] += len(want_set - got_set)
    counts["dropped"] += len(ex.dropped)


def eval_extraction(meta: dict, cases: list[dict], runners: dict, structured: list | None = None) -> list[str]:
    firms = {f["id"]: f for f in meta["firms"]}
    generated = []
    for lid, fid in meta["truth"].items():
        if not lid.startswith("profile-"):
            continue
        f = firms[fid]
        _, text = profile_text((SITE / "portal" / "profile" / f"{lid.removeprefix('profile-')}.html").read_text())
        generated.append(
            {
                "text": text,
                "contact_name": f"{'ESV ' if f['licensed'] else ''}{f['first']} {f['middle'][0]}. {f['surname']}",
                "phones": normalise_phones(f["phones"][-1]),
                "emails": [f["email"] or f"enquiries@{f['domain']}"],
            }
        )
    rows = ["| Extractor | Set | Name P / R | Phones P / R | Emails P / R | Ungrounded fields dropped | Cost |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for name, run in runners.items():
        for set_name, items in (("generated profiles", generated), ("hard cases", cases)):
            c = {k: 0 for k in ("name_tp", "name_fp", "name_fn", "phones_tp", "phones_fp", "phones_fn", "emails_tp", "emails_fp", "emails_fn", "dropped")}
            cost = 0
            for item in items:
                ex = run(item["text"])
                cost += (ex.usage or {}).get("cost_micros", 0)
                _field_scores(ex, item, c)
            cells = [" / ".join(pr(c[f"{k}_tp"], c[f"{k}_fp"], c[f"{k}_fn"])) for k in ("name", "phones", "emails")]
            if structured is not None:
                structured.append({"extractor": name, "set": set_name, "n": len(items), "name": cells[0], "phones": cells[1], "emails": cells[2], "dropped": c["dropped"], "cost": f"${cost / 1e6:.4f}"})
            rows.append(f"| {name} | {set_name} ({len(items)}) | {cells[0]} | {cells[1]} | {cells[2]} | {c['dropped']} | ${cost / 1e6:.4f} |")
    return rows


# ------------------------------------------------------------------ 3. names

def eval_names(pairs: list[dict], structured: dict | None = None) -> tuple[list[str], list[str]]:
    tp = fp = fn = tn = 0
    misses = []
    for p in pairs:
        a = parse_person(p["a"], surname_first=p.get("a_surname_first", False))
        b = parse_person(p["b"], surname_first=p.get("b_surname_first", False))
        predicted = bool(a and b and a.surname == b.surname and given_names_compatible(a.given, b.given))
        if predicted and p["same"]:
            tp += 1
        elif predicted:
            fp += 1
            misses.append(f"- Wrongly matched **{p['a']}** and **{p['b']}**: {p['why']}")
        elif p["same"]:
            fn += 1
            misses.append(f"- Missed **{p['a']}** = **{p['b']}**: {p['why']}")
        else:
            tn += 1
    prec, rec = pr(tp, fp, fn)
    if structured is not None:
        structured.update({"pairs": len(pairs), "precision": prec, "recall": rec, "kept_apart": tn, "different_total": tn + fp, "misses": misses})
    rows = ["| Pairs | Precision | Recall | Correctly kept apart |", "| --- | --- | --- | --- |", f"| {len(pairs)} | {prec} | {rec} | {tn} of {tn + fp} |"]
    return rows, misses


def compute(progress=lambda text: None) -> dict:
    """Run every eval; write evals/results.md and return the numbers for the web app."""
    progress("Generating a fresh directory for the evals")
    meta = generate(SITE)
    cases = [json.loads(line) for line in (HERE / "extraction_cases.jsonl").read_text().splitlines() if line.strip()]
    pairs = [json.loads(line) for line in (HERE / "name_pairs.jsonl").read_text().splitlines() if line.strip()]
    runners = {"regex baseline": extract_scripted}
    if os.environ.get("LEAD_ENGINE_MODE") == "anthropic" and os.environ.get("ANTHROPIC_API_KEY"):
        runners[os.environ.get("LEAD_ENGINE_MODEL", "claude-opus-5")] = extract_claude

    progress("Running the whole pipeline and checking every pair of listings against the truth file")
    match_rows, match_info = eval_matching(meta)
    progress("Scoring extraction on generated profiles and hand-written hard cases")
    extraction: list = []
    extract_rows = eval_extraction(meta, cases, runners, extraction)
    names: dict = {}
    progress("Checking the name-matching traps")
    name_rows, misses = eval_names(pairs, names)

    out = [
        "# Eval results",
        "",
        "Generated by `python -m lead_engine eval`. All data is fictional.",
        "",
        "## Matching, end to end",
        "",
        "The whole pipeline runs on the generated directory, and every pair of listings is checked against the truth file.",
        "",
        *match_rows,
        "",
        "## Extraction",
        "",
        "Precision / recall per field. A field only counts if its quote is really on the page; ungrounded fields are dropped before scoring.",
        "",
        *extract_rows,
        "",
        "## Name matching traps",
        "",
        *name_rows,
        "",
        *(misses or ["No misses."]),
        "",
    ]
    (HERE / "results.md").write_text("\n".join(out))
    return {"matching": match_info, "extraction": extraction, "names": names, "markdown": "\n".join(out), "claude": len(runners) > 1}


def main() -> int:
    result = compute()
    print(result["markdown"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
