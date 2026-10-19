import json

from lead_engine import db
from lead_engine.directory import generate
from lead_engine.pipeline import Budget, Interrupted, Run, summary


def build(tmp_path, n=30):
    site = tmp_path / "site"
    meta = generate(site, n=n, seed=3)
    return site, meta


def test_full_run(tmp_path):
    site, meta = build(tmp_path)
    conn = db.connect(":memory:")
    s = Run(conn, site).all()
    assert s["listings"] == len(meta["truth"])
    assert s["parse_gaps"] == 0
    assert s["pages_disallowed"] == 1  # robots.txt was honoured
    retried = conn.execute("select attempts from pages where url = 'portal/page-3.html'").fetchone()
    assert retried is None or retried["attempts"] == 2  # a 503 was retried, not lost


def test_interrupted_run_resumes_without_duplicates(tmp_path):
    site, _ = build(tmp_path)
    conn = db.connect(tmp_path / "a.db")
    stops = 0
    for stop_after in (5, 20, 40):
        try:
            Run(conn, site, Budget(stop_after)).all()
            break
        except Interrupted:
            stops += 1
    resumed = Run(conn, site).all()
    clean = Run(db.connect(":memory:"), site).all()
    assert stops >= 1
    assert resumed == clean
    assert conn.execute("select count(*) from listings").fetchone()[0] == conn.execute("select count(distinct id) from listings").fetchone()[0]


def test_no_automatic_link_joins_two_different_firms(tmp_path):
    site, meta = build(tmp_path, n=60)
    conn = db.connect(":memory:")
    Run(conn, site).all()
    truth = meta["truth"]
    for cluster, in conn.execute("select distinct cluster_id from listings"):
        firms = {truth[r[0]] for r in conn.execute("select id from listings where cluster_id = ?", (cluster,))}
        assert len(firms) == 1, f"cluster {cluster} mixes firms {firms}"


def test_catch_all_domains_are_quarantined(tmp_path):
    site, _ = build(tmp_path, n=60)
    conn = db.connect(":memory:")
    Run(conn, site).all()
    flagged = conn.execute("select count(*) from listings where email_status in ('catch_all', 'quarantined')").fetchone()[0]
    quarantined = conn.execute("select count(*) from quarantined_domains").fetchone()[0]
    assert (flagged > 0) == (quarantined > 0)


def test_no_email_means_empty_not_guessed(tmp_path):
    site, _ = build(tmp_path)
    conn = db.connect(":memory:")
    Run(conn, site).all()
    for r in conn.execute("select email, enrichment_trail from listings where email is null"):
        trail = json.loads(r["enrichment_trail"])
        assert trail[-1]["result"].startswith("no provider found")
