"""Enrichment across several providers, tried in order, with a trail of what each one said.

The providers are mocks with realistic behaviour: misses, timeouts and rate limits, decided deterministically
from the record so runs are reproducible. If none of them finds an email, the field stays empty."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass


class ProviderTimeout(Exception):
    pass


class RateLimited(Exception):
    pass


def _roll(*parts: str) -> float:
    h = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return int(h[:8], 16) / 0xFFFFFFFF


@dataclass
class MockProvider:
    name: str
    hit_rate: float
    timeout_rate: float = 0.0
    rate_limit_rate: float = 0.0
    # Some providers guess a pattern address on the company domain. Those are the ones that bounce later.
    guesses_patterns: bool = False

    def lookup(self, listing_id: str, given: str | None, domain: str | None, attempt: int) -> dict:
        if attempt == 1 and _roll(self.name, listing_id, "timeout") < self.timeout_rate:
            raise ProviderTimeout(self.name)
        if attempt == 1 and _roll(self.name, listing_id, "429") < self.rate_limit_rate:
            raise RateLimited(self.name)
        if not domain or _roll(self.name, listing_id, "hit") >= self.hit_rate:
            return {"email": None}
        local = (given or "info").lower() if self.guesses_patterns else "info"
        return {"email": f"{local}@{domain}", "confidence": "pattern" if self.guesses_patterns else "listed"}


PROVIDERS = [
    MockProvider("profiles-api", hit_rate=0.45, timeout_rate=0.06),
    MockProvider("contact-finder", hit_rate=0.35, rate_limit_rate=0.08),
    MockProvider("pattern-guesser", hit_rate=0.5, guesses_patterns=True),
]


def enrich(listing_id: str, given: str | None, domain: str | None) -> tuple[dict | None, list[dict]]:
    """Try each provider in order. Returns (result or None, trail)."""
    trail: list[dict] = []
    for p in PROVIDERS:
        for attempt in (1, 2):
            try:
                r = p.lookup(listing_id, given, domain, attempt)
            except ProviderTimeout:
                trail.append({"provider": p.name, "result": "timeout", "attempt": attempt})
                continue
            except RateLimited:
                trail.append({"provider": p.name, "result": "rate limited, backed off", "attempt": attempt})
                continue
            if r["email"]:
                trail.append({"provider": p.name, "result": "found", "email": r["email"], "confidence": r["confidence"]})
                return {"email": r["email"], "source": p.name, "confidence": r["confidence"]}, trail
            trail.append({"provider": p.name, "result": "nothing"})
            break
    trail.append({"provider": "-", "result": "no provider found an email; left empty"})
    return None, trail


# ------------------------------------------------------------------ verification

def verify(email: str) -> str:
    """Mock verifier. Returns deliverable | catch_all | invalid | unknown, deterministically per address.

    catch_all is the trap: the mail server accepts anything, so a verifier calls it fine and it bounces later."""
    domain = email.split("@")[-1]
    d = _roll("domain", domain)
    if d < 0.18:
        return "catch_all"
    a = _roll("addr", email)
    if a < 0.1:
        return "invalid"
    if a < 0.14:
        return "unknown"
    return "deliverable"
