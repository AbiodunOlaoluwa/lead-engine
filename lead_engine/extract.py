"""Grounded extraction from free-text pages.

Every extracted field must come with a quote from the page, and the server checks that the quote is really
there and really contains the value. Fields that fail are dropped and counted: empty beats invented.
Two extractors share that contract: a regex baseline and Claude. The evals compare them on the same pages."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from .names import HONORIFICS, find_emails, fold, normalise_phones

FIELD_KINDS = ("contact_name", "phones", "emails", "services")


@dataclass
class Extraction:
    contact_name: str | None = None
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)  # fields that failed grounding, with why
    contact_role: str | None = None  # "principal" when the name's own quote says they run the firm
    model: str = "scripted"
    usage: dict = field(default_factory=dict)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


# A role is only read from the quote that named the person, so "office manager, Ifeoma Nnadi" is not a principal.
PRINCIPAL_CUE = re.compile(r"principal|managing (?:partner|director)|founder|owner|\bceo\b|led by|leads", re.I)


def ground(raw: dict, text: str, model: str, usage: dict | None = None) -> Extraction:
    """Keep only the fields whose quote appears in the page and actually contains the value."""
    page = _norm(text)
    out = Extraction(model=model, usage=usage or {})

    def quoted(item: dict | None, kind: str) -> tuple[str, str] | None:
        if not item or not item.get("value"):
            return None
        quote, value = str(item.get("quote", "")), str(item["value"])
        if not quote or _norm(quote) not in page:
            out.dropped.append({"field": kind, "value": value, "why": "quote not found on the page"})
            return None
        return value, quote

    name = quoted(raw.get("contact_name"), "contact_name")
    if name:
        value, quote = name
        tokens = [t for t in fold(value).split() if t not in HONORIFICS and len(t) > 1]
        if tokens and all(t in fold(quote).split() for t in tokens):
            out.contact_name = value
            out.contact_role = "principal" if PRINCIPAL_CUE.search(quote) else None
        else:
            out.dropped.append({"field": "contact_name", "value": value, "why": "name not in its quote"})
    for item in raw.get("phones") or []:
        got = quoted(item, "phones")
        if got:
            want = normalise_phones(got[0])
            if want and want[0] in normalise_phones(got[1]):
                out.phones.append(want[0]) if want[0] not in out.phones else None
            else:
                out.dropped.append({"field": "phones", "value": got[0], "why": "number not in its quote"})
    for item in raw.get("emails") or []:
        got = quoted(item, "emails")
        if got:
            value = got[0].lower().strip()
            if value in find_emails(got[1]):
                out.emails.append(value) if value not in out.emails else None
            else:
                out.dropped.append({"field": "emails", "value": value, "why": "address not in its quote"})
    for item in raw.get("services") or []:
        got = quoted(item, "services")
        if got and _norm(got[0]) in _norm(got[1]):
            out.services.append(got[0])
        elif got:
            out.dropped.append({"field": "services", "value": got[0], "why": "service not in its quote"})
    return out


# ------------------------------------------------------------------ regex baseline

PHONE_SPAN = re.compile(r"(?:\+?234[\s-]?|0)?[789]\d{2}[\s-]?\d{3}[\s-]?\d{4}")
NAME_SPAN = re.compile(
    r"(?:principal partner|managing partner|managing director|led by|speak to|contact)\s*,?\s+"
    r"((?:(?:ESV|Engr|Mrs?|Ms|Dr|Chief|Arc)\.?\s+)?[A-Z][a-z]+(?:\s+[A-Z]\.)?(?:\s+[A-Z][a-z]+)+)"
)
SERVICES_SPAN = re.compile(r"(?:handles|offers|provides|specialis(?:e|es) in)\s+([^.]+)\.")
EMAIL_SPAN = re.compile(r"[a-z0-9._%+-]+\s*(?:@|\[at\]|\(at\))\s*[a-z0-9-]+(?:(?:\.|[ \t]+\.[ \t]+|\s*\[dot\]\s*|\s*\(dot\)\s*)[a-z0-9-]+)+", re.I)


def extract_scripted(text: str) -> Extraction:
    raw: dict = {"phones": [], "emails": [], "services": []}
    if m := NAME_SPAN.search(text):
        raw["contact_name"] = {"value": m.group(1), "quote": m.group(0)}
    for m in PHONE_SPAN.finditer(text):
        raw["phones"].append({"value": m.group(0), "quote": m.group(0)})
    for m in EMAIL_SPAN.finditer(text):
        found = find_emails(m.group(0))
        if found:
            raw["emails"].append({"value": found[0], "quote": m.group(0)})
    if m := SERVICES_SPAN.search(text):
        for part in re.split(r",\s*|\s+and\s+", m.group(1)):
            if part.strip():
                raw["services"].append({"value": part.strip(), "quote": m.group(0)})
    return ground(raw, text, "scripted")


# ------------------------------------------------------------------ Claude

SYSTEM = """You extract contact details from a property firm's "About" page.

Return JSON matching the schema. For every value, include "quote": the exact span of the page it came from,
copied character for character. The server checks each quote against the page and drops any field whose quote
is missing or doesn't contain the value, so never paraphrase a quote and never fill a field from general knowledge.

- contact_name: the main person named as running the firm (keep any title such as ESV). null if nobody is named.
- phones: every phone number, value as written.
- emails: every email address, written normally (turn "[at]" into "@" and "(dot)" into ".") in value; quote it as it appears.
- services: each service the firm says it provides, as short phrases.
Names of buildings or streets are not people."""

_QUOTED = {
    "type": "object",
    "additionalProperties": False,
    "required": ["value", "quote"],
    "properties": {"value": {"type": "string"}, "quote": {"type": "string"}},
}
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["contact_name", "phones", "emails", "services"],
    "properties": {
        "contact_name": {"anyOf": [_QUOTED, {"type": "null"}]},
        "phones": {"type": "array", "items": _QUOTED},
        "emails": {"type": "array", "items": _QUOTED},
        "services": {"type": "array", "items": _QUOTED},
    },
}
PRICES = {"claude-opus-5": (5, 25), "claude-opus-5-5": (4, 20), "claude-sonnet-5": (2, 10), "claude-haiku-4-5": (1, 5)}


def usage_of(u, model: str) -> dict:
    """Tokens and cost in micro-dollars for one call, cache reads and writes priced separately."""
    pin, pout = PRICES.get(model, PRICES["claude-opus-5"])
    cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
    return {
        "input_tokens": u.input_tokens + cache_read + cache_write,
        "output_tokens": u.output_tokens,
        "cache_read_tokens": cache_read,
        "cost_micros": round(u.input_tokens * pin + cache_write * pin * 1.25 + cache_read * pin * 0.1 + u.output_tokens * pout),
    }


def extract_claude(text: str, model: str | None = None) -> Extraction:
    import anthropic

    model = model or os.environ.get("LEAD_ENGINE_MODEL", "claude-opus-5")
    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=model,
        max_tokens=4000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": f"Page text:\n\n{text}"}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
    )
    usage = usage_of(response.usage, model)
    if response.stop_reason == "refusal":
        return Extraction(model=model, usage=usage, dropped=[{"field": "*", "value": "", "why": "model declined"}])
    body = next((b.text for b in response.content if b.type == "text"), "{}")
    try:
        raw = json.loads(body)
    except json.JSONDecodeError:
        return Extraction(model=model, usage=usage, dropped=[{"field": "*", "value": "", "why": "output was not valid JSON"}])
    return ground(raw, text, model, usage)


def extractor():
    """Claude when switched on and a key is present; the regex baseline otherwise."""
    if os.environ.get("LEAD_ENGINE_MODE") == "anthropic" and os.environ.get("ANTHROPIC_API_KEY"):
        return extract_claude
    return extract_scripted
