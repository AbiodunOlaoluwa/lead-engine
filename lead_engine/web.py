"""The demo app: every command as a button. Run with `python -m lead_engine serve` and open http://127.0.0.1:8040."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import service as svc
from .report import write_report

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="Lead Engine", docs_url="/api/docs")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class RunBody(BaseModel):
    stop_after: int | None = Field(default=None, ge=1, le=5000)
    pace: str = "watch"  # watch | fast
    fresh: bool = False


class GenerateBody(BaseModel):
    size: int = Field(default=80, ge=10, le=400)
    seed: int = Field(default=7, ge=0, le=99999)


class Decision(BaseModel):
    a: str
    b: str
    decision: str


class RescoreBody(BaseModel):
    pace: str = "watch"  # watch | fast


class Description(BaseModel):
    description: str = Field(min_length=1, max_length=2000)


class IcpBody(BaseModel):
    icp: dict


class SourceBody(BaseModel):
    model_config = {"extra": "forbid"}
    enabled: bool | None = None
    crawl_delay: float | None = Field(default=None, ge=0, le=60)
    max_pages: int | None = Field(default=None, ge=1, le=10000)


def _start(kind: str, fn):
    if not svc.JOBS.start(kind, fn):
        raise HTTPException(409, "Something is already running. Wait for it to finish.")
    return svc.JOBS.snapshot()


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text()


@app.get("/api/state")
def state():
    return svc.state()


@app.get("/api/job")
def job():
    return svc.JOBS.snapshot()


@app.get("/api/job/stream")
async def job_stream(request: Request):
    """Server-sent events: the job's real state, pushed the moment the engine changes it."""

    async def gen():
        version = -1
        while not await request.is_disconnected():
            version, snap = await asyncio.to_thread(svc.JOBS.wait, version, 10.0)
            yield f"data: {json.dumps(snap)}\n\n"
            await asyncio.sleep(0.03)  # coalesce bursts; the next message always carries the latest state

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"cache-control": "no-cache", "x-accel-buffering": "no"})


def _not_blocked():
    reason = svc.run_blocked()
    if reason:
        raise HTTPException(409, reason)


@app.post("/api/jobs/run")
def run(body: RunBody):
    _not_blocked()
    return _start("run", svc.run_job(body.stop_after, 30 if body.pace == "watch" else 0, body.fresh))


@app.post("/api/jobs/rescore")
def rescore(body: RescoreBody):
    if not svc.state()["has_run"]:
        raise HTTPException(409, "Nothing to rescore yet. Run the pipeline first.")
    return _start("rescore", svc.rescore_job(30 if body.pace == "watch" else 0))


@app.post("/api/jobs/drill")
def drill():
    _not_blocked()
    return _start("drill", svc.drill_job)


@app.post("/api/jobs/eval")
def evals():
    return _start("eval", svc.eval_job)


@app.post("/api/jobs/generate")
def regenerate(body: GenerateBody):
    return _start("generate", svc.generate_job(body.size, body.seed))


@app.post("/api/reset")
def reset():
    if svc.JOBS.busy():
        raise HTTPException(409, "Something is running.")
    svc.reset_db()
    return {"ok": True}


@app.get("/api/icp")
def icp():
    return svc.icp_state(svc.conn())


@app.post("/api/icp/parse")
def icp_parse(body: Description):
    return svc.parse_icp(svc.conn(), body.description)


@app.post("/api/icp/coverage")
def icp_coverage(body: IcpBody):
    return svc.coverage_for(svc.conn(), body.icp)


@app.post("/api/icp")
def icp_save(body: IcpBody):
    return svc.save_icp(svc.conn(), body.icp)


@app.get("/api/icp/versions/{version}")
def icp_version(version: int):
    from .icp import get_version

    found = get_version(svc.conn(), version)
    if not found:
        raise HTTPException(404, "No such ICP version.")
    return {"version": version, "icp": found}


@app.get("/api/sources")
def sources():
    return svc.sources_state(svc.conn())


@app.post("/api/sources/{key}")
def source_update(key: str, body: SourceBody):
    if svc.JOBS.busy():
        raise HTTPException(409, "A run is in progress. Change sources when it has finished.")
    try:
        return svc.update_source(svc.conn(), key, body.model_dump(exclude_unset=True))
    except LookupError as e:
        raise HTTPException(404, str(e))


@app.get("/api/contacts")
def contacts(q: str = ""):
    return svc.contacts(svc.conn(), q)


@app.get("/api/contacts/{cid}")
def contact(cid: str):
    d = svc.contact_detail(svc.conn(), cid)
    if not d:
        raise HTTPException(404, "No such contact.")
    return d


@app.get("/api/review")
def review():
    return svc.review_queue(svc.conn())


@app.post("/api/review")
def decide(body: Decision):
    if svc.JOBS.busy():
        raise HTTPException(409, "A run is in progress. Decide when it has finished.")
    try:
        return svc.decide(svc.conn(), body.a, body.b, body.decision)
    except (ValueError, LookupError) as e:
        raise HTTPException(400, str(e))


@app.get("/api/pages")
def pages():
    return svc.pages(svc.conn())


@app.get("/api/events")
def events(limit: int = 40):
    return svc.events(svc.conn(), min(limit, 200))


@app.get("/api/export.csv")
def export():
    text, ready, held, outside = svc.export_csv(svc.conn())
    return Response(
        text,
        media_type="text/csv",
        headers={
            "content-disposition": 'attachment; filename="lead-engine-contacts.csv"',
            "x-held-for-review": str(held), "x-outside-icp": str(outside), "x-exported": str(ready),
        },
    )


@app.get("/report", response_class=HTMLResponse)
def report():
    path = write_report(svc.conn(), svc.SITE, Path("out/report.html"))
    return path.read_text()


@app.get("/site/{path:path}")
def site(path: str):
    """The generated directory, so you can see exactly what the crawler read."""
    root = svc.SITE.resolve()
    target = (root / path).resolve()
    if not str(target).startswith(str(root)) or not target.is_file():
        raise HTTPException(404)
    if target.suffix == ".txt":
        return PlainTextResponse(target.read_text())
    return FileResponse(target)
