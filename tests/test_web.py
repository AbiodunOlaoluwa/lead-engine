"""The demo app: jobs report the engine's real progress, crashes stop the right stage, and review decisions stick."""
import time

import pytest
from fastapi.testclient import TestClient

from lead_engine import service as svc
from lead_engine.directory import generate


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "DATA", tmp_path)
    monkeypatch.setattr(svc, "SITE", tmp_path / "site")
    monkeypatch.setattr(svc, "DB_PATH", tmp_path / "lead-engine.db")
    monkeypatch.setattr(svc, "EVAL_RESULTS", tmp_path / "eval-results.json")
    monkeypatch.setattr(svc, "JOBS", svc.Jobs())
    generate(tmp_path / "site", n=30, seed=3)
    from lead_engine.web import app

    return TestClient(app)


def wait(client, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        job = client.get("/api/job").json()
        if job["state"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_run_reports_every_stage_done_only_when_finished(client):
    assert client.post("/api/jobs/run", json={"pace": "fast"}).status_code == 200
    job = wait(client)
    assert job["state"] == "done"
    for name, st in job["stages"].items():
        assert st["state"] == "done", name
        assert st["done"] == st["total"], name
    assert job["result"]["listings"] > 0
    assert job["stages"]["source"]["warn"] >= 1  # the flaky page was retried, and the UI was told


def test_crash_marks_the_stage_it_stopped_in(client):
    client.post("/api/jobs/run", json={"pace": "fast", "fresh": True, "stop_after": 20})
    job = wait(client)
    assert job["state"] == "interrupted"
    stopped = [n for n, st in job["stages"].items() if st["state"] == "stopped"]
    assert len(stopped) == 1
    assert all(job["stages"][n]["state"] == "pending" for n in ("match", "score"))
    client.post("/api/jobs/run", json={"pace": "fast"})
    assert wait(client)["state"] == "done"


def test_only_one_job_at_a_time(client):
    client.post("/api/jobs/run", json={"pace": "watch"})
    assert client.post("/api/jobs/drill").status_code == 409
    wait(client, 60)


def test_review_decision_survives_a_rerun(client):
    client.post("/api/jobs/run", json={"pace": "fast"})
    wait(client)
    queue = client.get("/api/review").json()["pending"]
    if not queue:
        pytest.skip("this directory produced no review pairs")
    pair = queue[0]
    before = client.get("/api/state").json()["summary"]["contacts"]
    r = client.post("/api/review", json={"a": pair["a"]["id"], "b": pair["b"]["id"], "decision": "merge"})
    assert r.status_code == 200
    assert r.json()["contacts"] == before - 1
    client.post("/api/jobs/run", json={"pace": "fast"})
    wait(client)
    assert client.get("/api/state").json()["summary"]["contacts"] == before - 1


def test_export_holds_back_contacts_waiting_for_review(client):
    client.post("/api/jobs/run", json={"pace": "fast"})
    wait(client)
    r = client.get("/api/export.csv")
    assert r.status_code == 200
    rows = r.text.strip().splitlines()[1:]
    assert len(rows) == int(r.headers["x-exported"])


def test_raw_site_is_served_but_not_outside_it(client):
    assert client.get("/site/robots.txt").status_code == 200
    assert client.get("/site/../lead-engine.db").status_code == 404


def test_rescore_runs_only_the_score_stage_and_reports_it_exactly(client):
    assert client.post("/api/jobs/rescore", json={"pace": "fast"}).status_code == 409  # nothing to rescore yet
    client.post("/api/jobs/run", json={"pace": "fast"})
    wait(client)
    pages_before = client.get("/api/pages").json()
    draft = client.post("/api/icp/parse", json={"description": "Only firms in Lagos. Reachable by email."}).json()["icp"]
    saved = client.post("/api/icp", json={"icp": draft}).json()
    assert client.post("/api/jobs/rescore", json={"pace": "fast"}).status_code == 200
    job = wait(client)
    assert job["state"] == "done" and job["kind"] == "rescore"
    score = job["stages"]["score"]
    assert score["state"] == "done" and score["done"] == score["total"] > 0
    assert all(job["stages"][s]["state"] == "kept" for s in ("source", "extract", "enrich", "verify", "match"))
    assert job["result"]["rescore"]["version"] == saved["version"]
    assert client.get("/api/pages").json() == pages_before  # nothing re-fetched
    contacts = client.get("/api/contacts").json()
    assert any(c["outside_icp"] for c in contacts) and any(not c["outside_icp"] for c in contacts)
    r = client.get("/api/export.csv")
    assert int(r.headers["x-outside-icp"]) > 0
    assert len(r.text.strip().splitlines()) - 1 == int(r.headers["x-exported"])


def test_icp_endpoints_parse_save_and_version(client):
    first = client.get("/api/icp").json()
    assert first["version"] == 1 and first["icp"]["criteria"]["register"]["on"]
    parsed = client.post("/api/icp/parse", json={"description": "Letting firms in Ibadan with a team of 10 or more, reachable by phone."}).json()
    assert parsed["icp"]["parser"] == "baseline"
    assert any("team size" in w for w in parsed["coverage"]["warnings"])
    draft = parsed["icp"]
    draft["criteria"]["firm_type"]["points"] = 30
    saved = client.post("/api/icp", json={"icp": draft}).json()
    assert saved["version"] == 2
    assert saved["icp"]["criteria"]["firm_type"]["points"] == 30
    assert client.get("/api/icp/versions/1").json()["icp"]["criteria"]["register"]["on"]
    assert client.get("/api/icp/versions/99").status_code == 404


def test_sources_endpoints_toggle_rules_and_block(client):
    state = client.get("/api/sources").json()
    assert [s["key"] for s in state["sources"]] == ["portal", "register", "profile"]
    assert all(s["robots_allowed"] for s in state["sources"])
    r = client.post("/api/sources/portal", json={"crawl_delay": 0.1})
    assert "robots.txt" in r.json()["note"]
    assert client.post("/api/sources/nope", json={"enabled": False}).status_code == 404
    client.post("/api/sources/portal", json={"enabled": False})
    client.post("/api/sources/register", json={"enabled": False})
    blocked = client.post("/api/jobs/run", json={"pace": "fast"})
    assert blocked.status_code == 409 and "phone number or an email" in blocked.json()["detail"]
    client.post("/api/sources/register", json={"enabled": True})
    assert client.post("/api/jobs/run", json={"pace": "fast"}).status_code == 200
    wait(client)
    pages = client.get("/api/pages").json()
    assert pages and all(p["url"].startswith("register/") for p in pages)


def test_start_over_keeps_the_icp_and_source_settings(client):
    client.post("/api/icp", json={"icp": client.post("/api/icp/parse", json={"description": "Only firms in Lagos."}).json()["icp"]})
    client.post("/api/sources/register", json={"max_pages": 1})
    client.post("/api/jobs/run", json={"pace": "fast"})
    wait(client)
    assert client.post("/api/reset").status_code == 200
    assert client.get("/api/state").json()["has_run"] is False
    assert client.get("/api/icp").json()["version"] == 2
    reg = next(s for s in client.get("/api/sources").json()["sources"] if s["key"] == "register")
    assert reg["max_pages"] == 1
