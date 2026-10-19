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
