"""The source catalog: switches, rules, coverage of the ICP, and the one gap that blocks a run."""
from lead_engine import db, icp, sources
from lead_engine.directory import generate
from lead_engine.pipeline import Run


def site(tmp_path, n=30):
    generate(tmp_path / "site", n=n, seed=3)
    return tmp_path / "site"


def test_a_disabled_source_is_never_fetched(tmp_path):
    s = site(tmp_path)
    conn = db.connect(":memory:")
    sources.update(conn, "register", s, enabled=False)
    Run(conn, s).all()
    assert conn.execute("select count(*) from pages where url like 'register/%'").fetchone()[0] == 0
    assert conn.execute("select count(*) from listings where source = 'register'").fetchone()[0] == 0
    assert conn.execute("select count(*) from listings where source = 'portal'").fetchone()[0] > 0


def test_switching_a_source_off_sets_its_listings_aside_and_back_on_reads_it_again(tmp_path):
    s = site(tmp_path)
    conn = db.connect(tmp_path / "a.db")
    full = Run(conn, s).all()
    sources.update(conn, "register", s, enabled=False)
    without = Run(conn, s).all()
    assert "register" not in without["by_source"]
    assert without["listings"] < full["listings"]
    sources.update(conn, "register", s, enabled=True)
    again = Run(conn, s).all()
    assert again == full  # same result as the first run: nothing stale left behind


def test_profiles_need_the_portal(tmp_path):
    s = site(tmp_path)
    conn = db.connect(":memory:")
    sources.update(conn, "portal", s, enabled=False)
    Run(conn, s).all()
    assert conn.execute("select count(*) from pages where source in ('portal', 'profile')").fetchone()[0] == 0
    cfg = sources.config(conn)
    assert "profile" not in sources.effective(cfg)
    warnings = sources.coverage(cfg, [])["warnings"]
    assert any("found through the portal" in w for w in warnings)


def test_max_pages_caps_a_source(tmp_path):
    s = site(tmp_path)
    conn = db.connect(":memory:")
    sources.update(conn, "portal", s, max_pages=1)
    Run(conn, s).all()
    assert conn.execute("select count(*) from source_counts where source = 'portal'").fetchone()[0] == 1
    warn = conn.execute("select message from events where source = 'portal' and level = 'warn' and stage = 'source'").fetchone()
    assert warn and "max pages is 1" in warn[0]


def test_crawl_delay_never_goes_below_robots(tmp_path):
    s = site(tmp_path)
    conn = db.connect(":memory:")
    res = sources.update(conn, "portal", s, crawl_delay=0.2)
    assert res["config"]["crawl_delay"] == 1.0  # robots.txt says Crawl-delay: 1
    assert "robots.txt" in res["note"]


def test_gap_warns_when_the_only_source_for_a_criterion_is_off(tmp_path):
    s = site(tmp_path)
    conn = db.connect(":memory:")
    wants = icp.parse_baseline("Must be on the register. Reachable by email.")
    assert sources.coverage(sources.config(conn), icp.needs(wants))["warnings"] == []
    sources.update(conn, "register", s, enabled=False)
    cov = sources.coverage(sources.config(conn), icp.needs(wants))
    row = next(r for r in cov["rows"] if r["criterion"] == "register")
    assert row["status"] == "gap"
    assert any("Professional register is off" in w for w in cov["warnings"])
    assert cov["blocked"] is None  # gaps warn, they don't block


def test_unsupported_asks_are_listed_as_gaps():
    wants = icp.parse_baseline("Letting firms with a team of 10 or more.")
    cov = sources.coverage({k: {"enabled": True, "crawl_delay": None, "max_pages": None} for k in sources.BY_KEY}, icp.needs(wants), wants["unsupported"])
    assert any("team size" in w for w in cov["warnings"])


def test_suggestions_rank_sources_by_how_much_of_the_icp_they_cover():
    wants = icp.parse_baseline("Property management firms in Lagos with at least 10 rentals, on the register.")
    cfg = {k: {"enabled": True, "crawl_delay": None, "max_pages": None} for k in sources.BY_KEY}
    first = sources.coverage(cfg, icp.needs(wants))["suggestions"][0]
    assert first["key"] == "portal"


def test_no_contact_channel_blocks_the_run(tmp_path):
    s = site(tmp_path)
    conn = db.connect(":memory:")
    for key in ("portal", "register"):
        sources.update(conn, key, s, enabled=False)
    assert sources.blocked(sources.config(conn))  # profile pages alone can't be found without the portal
    sources.update(conn, "register", s, enabled=True)
    assert sources.blocked(sources.config(conn)) is None
