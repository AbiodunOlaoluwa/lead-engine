"""A polite fetcher: reads robots.txt, honours the crawl delay, retries with backoff, and records every page
in the ledger so a stopped crawl resumes where it left off.

The transport reads the generated site from disk and can inject failures, so behaviour under errors is testable.
In production the same interface sits on an HTTP client."""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.robotparser import RobotFileParser

from . import db


class TransientError(Exception):
    pass


@dataclass
class FileTransport:
    root: Path
    flaky: dict[str, int] = field(default_factory=dict)  # path -> how many times it fails before succeeding

    def get(self, path: str) -> str:
        if self.flaky.get(path, 0) > 0:
            self.flaky[path] -= 1
            raise TransientError(f"503 on {path}")
        target = (self.root / path).resolve()
        if not str(target).startswith(str(self.root.resolve())) or not target.exists():
            raise FileNotFoundError(path)
        return target.read_text()


@dataclass
class Fetcher:
    conn: sqlite3.Connection
    transport: FileTransport
    delay_scale: float = 0.0  # 1.0 honours the real crawl delay; tests and demos shrink it
    max_attempts: int = 3
    _robots: RobotFileParser | None = None
    _last: float = 0.0

    def robots(self) -> RobotFileParser:
        if self._robots is None:
            rp = RobotFileParser()
            rp.parse(self.transport.get("robots.txt").splitlines())
            self._robots = rp
        return self._robots

    def get(self, path: str, source: str) -> str | None:
        row = self.conn.execute("select status, body from pages where url = ?", (path,)).fetchone()
        if row and row["status"] == "ok":
            return row["body"]  # already fetched on an earlier run: no second request
        if not self.robots().can_fetch("lead-engine", "/" + path):
            self.conn.execute("insert or replace into pages (url, source, status) values (?, ?, 'disallowed')", (path, source))
            db.log(self.conn, "fetch", f"robots.txt disallows {path}; skipped", "warn")
            return None
        delay = (self.robots().crawl_delay("lead-engine") or 0) * self.delay_scale
        attempts = 0
        while attempts < self.max_attempts:
            attempts += 1
            wait = self._last + delay - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                body = self.transport.get(path)
            except TransientError as e:
                db.log(self.conn, "fetch", f"{e}; retrying ({attempts}/{self.max_attempts})", "warn")
                time.sleep(0.01 * 2**attempts * self.delay_scale)
                continue
            self.conn.execute(
                "insert or replace into pages (url, source, status, attempts, body, fetched_at) values (?, ?, 'ok', ?, ?, ?)",
                (path, source, attempts, body, db.now()),
            )
            self.conn.commit()
            return body
        self.conn.execute("insert or replace into pages (url, source, status, attempts) values (?, ?, 'failed', ?)", (path, source, attempts))
        db.log(self.conn, "fetch", f"gave up on {path} after {attempts} attempts", "error")
        return None
