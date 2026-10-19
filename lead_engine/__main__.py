"""python -m lead_engine <command>

  generate          build the fictional directory in data/site
  run [--stop-after N] [--fresh]
                    run the pipeline (resumes from the ledger unless --fresh)
  drill             stop a run part-way, resume it, and prove nothing was duplicated
  report            write out/report.html for the latest run
  eval              run the evals in evals/ and write evals/results.md
  serve             open the demo app at http://127.0.0.1:8040
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from . import db
from .directory import generate
from .pipeline import Budget, Interrupted, Run, summary

DATA = Path("data")
SITE = DATA / "site"
DB_PATH = DATA / "lead-engine.db"


def ensure_site():
    if not (SITE / "truth.json").exists():
        generate(SITE)


def cmd_run(stop_after: int | None, fresh: bool) -> int:
    ensure_site()
    if fresh and DB_PATH.exists():
        DB_PATH.unlink()
    conn = db.connect(DB_PATH)
    try:
        result = Run(conn, SITE, Budget(stop_after)).all()
    except Interrupted as e:
        conn.commit()
        print(f"Interrupted ({e}). Run again to resume.")
        return 1
    print(json.dumps(result, indent=2))
    return 0


def duplicates(conn) -> dict:
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "listings": one("select count(*) from listings"),
        "distinct_listing_ids": one("select count(distinct id) from listings"),
        "pages_fetched_twice": one("select count(*) from (select url from pages group by url having count(*) > 1)"),
        "ledger_rows": one("select count(*) from ledger"),
        "enrichment_lookups_repeated": one("select count(*) from (select key from ledger where stage = 'enrich' group by key having count(*) > 1)"),
    }


def cmd_drill() -> int:
    """Crash a run part-way, resume it, and compare with a clean run."""
    ensure_site()
    tmp = DATA / "drill.db"
    for p in (tmp,):
        if p.exists():
            p.unlink()
    conn = db.connect(tmp)
    stops = 0
    for stop_after in (12, 45, 90):
        try:
            Run(conn, SITE, Budget(stop_after)).all()
            break
        except Interrupted:
            stops += 1
            conn.commit()
    final = Run(conn, SITE).all()
    clean_conn = db.connect(":memory:")
    clean = Run(clean_conn, SITE).all()
    same = {k: (final[k], clean[k]) for k in final if final[k] != clean[k]}
    print(f"Stopped {stops} times and resumed. Duplicate check: {json.dumps(duplicates(conn))}")
    print("Same result as an uninterrupted run." if not same else f"DIFFERENT from a clean run: {same}")
    return 0 if not same else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="lead_engine", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("generate")
    r = sub.add_parser("run")
    r.add_argument("--stop-after", type=int)
    r.add_argument("--fresh", action="store_true")
    sub.add_parser("drill")
    sub.add_parser("report")
    sub.add_parser("eval")
    sv = sub.add_parser("serve")
    sv.add_argument("--port", type=int, default=8040)
    args = ap.parse_args(argv)

    if args.cmd == "generate":
        if SITE.exists():
            shutil.rmtree(SITE)
        meta = generate(SITE)
        print(f"Generated {len(meta['firms'])} firms and {len(meta['truth'])} listings in {SITE}/")
        return 0
    if args.cmd == "run":
        return cmd_run(args.stop_after, args.fresh)
    if args.cmd == "drill":
        return cmd_drill()
    if args.cmd == "report":
        from .report import write_report

        path = write_report(db.connect(DB_PATH), SITE, Path("out/report.html"))
        print(f"Wrote {path}")
        return 0
    if args.cmd == "serve":
        import uvicorn

        print(f"Lead Engine is running at http://127.0.0.1:{args.port}")
        uvicorn.run("lead_engine.web:app", host="127.0.0.1", port=args.port, log_level="warning")
        return 0
    if args.cmd == "eval":
        from evals.run_evals import main as run_evals

        return run_evals()
    return 2


if __name__ == "__main__":
    sys.exit(main())
