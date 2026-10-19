"""The ideal customer profile (ICP): who the list is for, written in plain English and read into criteria the scorer uses.

The description is read the way profile pages are: every criterion must carry the exact phrase of the description it
came from, and the server checks that the phrase is really there and really says that. A criterion that fails is
dropped and listed, never guessed. Two parsers share that check: a baseline that knows the directory's vocabulary,
and Claude. Points come from defaults the user can edit; the parser never invents a weighting.

Whether a criterion is a must-have is read from the sentence its phrase sits in ("Must be on the register", "Skip
firms that are mostly sales"), so that is grounded in the description too. Saved ICPs are versioned; the latest one
drives scoring."""
from __future__ import annotations

import copy
import json
import os
import re
import sqlite3

from . import db
from .directory import CITIES

FIRM_TYPES = ("management", "letting", "sales")
ROLES = ("principal", "agent")
CHANNELS = ("email", "phone", "either")
ORDER = ("firm_type", "rental_book", "short_lets", "register", "location", "role", "channel", "mostly_sales", "exclude_locations")
LABELS = {
    "firm_type": "Kind of firm",
    "rental_book": "Rental book",
    "short_lets": "Short-lets",
    "register": "On the professional register",
    "location": "City",
    "role": "Who you reach",
    "channel": "Reachable by",
    "mostly_sales": "Mostly-sales firms",
    "exclude_locations": "Cities to leave out",
}
# What each criterion needs a source to provide (any one of them). The Sources tab checks these against the catalog.
NEEDS = {
    "firm_type": ("services",),
    "rental_book": ("listing_counts",),
    "short_lets": ("listing_counts",),
    "register": ("register",),
    "location": ("location",),
    "role": ("role",),
    "mostly_sales": ("listing_counts",),
    "exclude_locations": ("location",),
}

DEFAULT_DESCRIPTION = (
    "Property management firms with a rental book to manage, ideally on the professional register. "
    "Short-lets count too. Firms that are mostly sales are a poor fit."
)
EXAMPLES = [
    {
        "title": "Lagos managers, reachable by email",
        "text": "Property management firms in Lagos with at least 10 homes for rent, ideally on the professional register. Reachable by email.",
    },
    {
        "title": "Short-let operators, register only",
        "text": "Short-let operators in Abuja or Port Harcourt with at least 5 short-lets. Must be on the register. Skip firms that are mostly sales.",
    },
    {
        "title": "Asks for something no source has",
        "text": "Principal partners at letting firms in Ibadan with a team of 10 or more and at least 20 rentals, reachable by phone.",
    },
]


def blank() -> dict:
    ev = lambda: {"evidence": [], "by_hand": False}  # noqa: E731
    return {
        "description": "",
        "parser": "",
        "criteria": {
            "firm_type": {"on": False, "must": False, "points": 10, "name_points": 8, "types": [], **ev()},
            "rental_book": {"on": False, "must": False, "points": 20, "min": None, "max": None, **ev()},
            "short_lets": {"on": False, "must": False, "points": 20, "min": None, **ev()},
            "register": {"on": False, "must": False, "points": 25, **ev()},
            "location": {"on": False, "must": False, "points": 15, "cities": [], **ev()},
            "role": {"on": False, "must": False, "points": 10, "roles": [], **ev()},
            "channel": {"on": False, "must": True, "points": 0, "value": "email", **ev()},
            "mostly_sales": {"on": False, "must": False, "points": -15, **ev()},
            "exclude_locations": {"on": False, "must": True, "points": 0, "cities": [], **ev()},
        },
        "tiers": {"tier1": 65, "tier2": 45},
        "unsupported": [],
        "dropped": [],
    }


# ------------------------------------------------------------------ grounding

def _norm(s: str) -> str:
    s = (s or "").replace("–", "-").replace("—", "-").replace("’", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", s).strip().lower()


MUST = re.compile(r"\b(must|only|required|requires?|has to|have to|needs? to|at minimum)\b", re.I)
HARD = re.compile(r"\b(skip|exclude|excluding|no|not|never|drop|without|avoid|leave out)\b", re.I)
NUMBER_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
                12: "twelve", 15: "fifteen", 20: "twenty", 25: "twenty-five", 30: "thirty", 40: "forty", 50: "fifty", 100: "a hundred"}

CUES = {
    "management": ("manag", "facility", "rent collection", "service charge"),
    "letting": ("letting", "lettings", "short-let", "short let", "tenant sourcing", "to let"),
    "sales": ("sale", "sell", "valuation", "land", "off-plan", "developer"),
    "principal": ("principal", "partner", "owner", "founder", "director", "runs", "head", "decision", "ceo", "md"),
    "agent": ("agent",),
    "rent": ("rent", "rental", "homes", "units", "properties", "book", "let", "landlord", "portfolio"),
    "short": ("short",),
    "register": ("register", "licensed", "licenced", "esv", "niesv", "member"),
    "email": ("email", "e-mail", "mail"),
    "phone": ("phone", "call", "whatsapp", "telephone", "number"),
    "sales_mix": ("sale", "sell"),
}


def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"[.;!?\n]+", text) if s.strip()]


def sentence_of(description: str, phrase: str) -> str:
    p = _norm(phrase)
    return next((s for s in sentences(description) if p in _norm(s)), phrase)


def _has_number(phrase: str, n: int) -> bool:
    p = _norm(phrase)
    return str(n) in re.findall(r"\d+", p) or (n in NUMBER_WORDS and NUMBER_WORDS[n] in p)


def ground(raw: dict, description: str, parser: str) -> dict:
    """Build an ICP from a parser's raw output, keeping only what the description really says."""
    icp = blank()
    icp["description"], icp["parser"] = description, parser
    c, dropped, text = icp["criteria"], icp["dropped"], _norm(description)

    def ok(field: str, value, phrase, cues=(), need: str | None = None) -> bool:
        phrase = str(phrase or "").strip()
        if not phrase or _norm(phrase) not in text:
            dropped.append({"field": field, "value": str(value), "why": "its phrase is not in the description"})
            return False
        p = _norm(phrase)
        if need is not None and _norm(need) not in p:
            dropped.append({"field": field, "value": str(value), "why": f'"{phrase}" does not mention {need}'})
            return False
        if cues and not any(cue in p for cue in cues):
            dropped.append({"field": field, "value": str(value), "why": f'"{phrase}" does not say that'})
            return False
        return True

    def evidence(key: str, phrase: str, for_value=None):
        c[key]["evidence"].append({"phrase": phrase.strip(), "for": for_value})

    def must_from(key: str) -> bool:
        return any(MUST.search(sentence_of(description, e["phrase"])) for e in c[key]["evidence"])

    for item in raw.get("firm_types") or []:
        v = str(item.get("value", "")).lower()
        if v not in FIRM_TYPES:
            dropped.append({"field": "firm_type", "value": v, "why": "not a kind of firm the directory describes"})
        elif ok("firm_type", v, item.get("evidence"), CUES[v]) and v not in c["firm_type"]["types"]:
            c["firm_type"]["types"].append(v)
            evidence("firm_type", item["evidence"], v)
    for item in raw.get("roles") or []:
        v = str(item.get("value", "")).lower()
        if v not in ROLES:
            dropped.append({"field": "role", "value": v, "why": "not a role the directory shows"})
        elif ok("role", v, item.get("evidence"), CUES[v]) and v not in c["role"]["roles"]:
            c["role"]["roles"].append(v)
            evidence("role", item["evidence"], v)
    for key, field in (("location", "locations"), ("exclude_locations", "exclude_locations")):
        for item in raw.get(field) or []:
            city = next((x for x in CITIES if x.lower() == str(item.get("value", "")).strip().lower()), None)
            if not city:
                dropped.append({"field": key, "value": str(item.get("value")), "why": "no firm in the directory is in that city"})
            elif ok(key, city, item.get("evidence"), need=city) and city not in c[key]["cities"]:
                c[key]["cities"].append(city)
                evidence(key, item["evidence"], city)

    rb = raw.get("rental_book")
    if rb and ok("rental_book", "rental book", rb.get("evidence"), CUES["rent"]):
        c["rental_book"]["on"] = True
        evidence("rental_book", rb["evidence"])
        for bound in ("min", "max"):
            n = rb.get(bound)
            if n is None:
                continue
            if isinstance(n, int) and n >= 0 and _has_number(rb["evidence"], n):
                c["rental_book"][bound] = n
            else:
                dropped.append({"field": "rental_book", "value": f"{bound} {n}", "why": f'"{rb["evidence"]}" does not contain {n}'})
    sl = raw.get("short_lets")
    if sl and ok("short_lets", "short-lets", sl.get("evidence"), CUES["short"]):
        c["short_lets"]["on"] = True
        evidence("short_lets", sl["evidence"])
        n = sl.get("min")
        if n is not None:
            if isinstance(n, int) and n >= 0 and _has_number(sl["evidence"], n):
                c["short_lets"]["min"] = n
            else:
                dropped.append({"field": "short_lets", "value": f"min {n}", "why": f'"{sl["evidence"]}" does not contain {n}'})
    reg = raw.get("register")
    if reg and ok("register", "on the register", reg.get("evidence"), CUES["register"]):
        c["register"]["on"] = True
        evidence("register", reg["evidence"])
    ch = raw.get("channel")
    if ch:
        v = str(ch.get("value", "")).lower()
        cues_ok = {"email": CUES["email"], "phone": CUES["phone"], "either": CUES["email"]}.get(v)
        if not cues_ok:
            dropped.append({"field": "channel", "value": v, "why": "not a channel the directory has"})
        elif ok("channel", v, ch.get("evidence"), cues_ok):
            if v == "either" and not any(cue in _norm(ch["evidence"]) for cue in CUES["phone"]):
                dropped.append({"field": "channel", "value": v, "why": f'"{ch["evidence"]}" names only one channel'})
            else:
                c["channel"].update(on=True, value=v)
                evidence("channel", ch["evidence"])
    ms = raw.get("mostly_sales")
    if ms and ok("mostly_sales", "mostly sales", ms.get("evidence"), CUES["sales_mix"]):
        c["mostly_sales"]["on"] = True
        evidence("mostly_sales", ms["evidence"])
        c["mostly_sales"]["must"] = bool(HARD.search(sentence_of(description, ms["evidence"])))
    for item in raw.get("unsupported") or []:
        phrase = str(item.get("evidence") or "").strip()
        if phrase and _norm(phrase) in text:
            icp["unsupported"].append({"what": str(item.get("what") or "something else"), "phrase": phrase})

    for key in ("firm_type", "location", "role", "exclude_locations"):
        listed = c[key].get("types") or c[key].get("cities") or c[key].get("roles")
        c[key]["on"] = bool(listed)
    for key in ("firm_type", "rental_book", "short_lets", "register", "location", "role"):
        if c[key]["on"]:
            c[key]["must"] = must_from(key)
    return icp


# ------------------------------------------------------------------ baseline parser

_CITY = r"(?:Port Harcourt|Lagos|Abuja|Ibadan|Enugu)"
_CITY_LIST = rf"{_CITY}(?:(?:\s*,\s*(?:or\s+|and\s+)?|\s+or\s+|\s+and\s+){_CITY})*"
EXCLUDE_CITIES = re.compile(rf"(?:not in|outside(?: of)?|except(?: for)?|excluding|apart from|but not|leave out|skip)\s+{_CITY_LIST}", re.I)
INCLUDE_CITIES = re.compile(rf"(?:(?:in|across|around|based in|from|within)\s+)?{_CITY_LIST}", re.I)
FIRM = {
    "management": re.compile(r"(?:property|facility|estate|rental)\s+manag\w*(?:\s+(?:firms?|companies|agencies|operators))?|manag(?:e|es|ing)\s+(?:homes|properties|rentals|estates)|rent collection|service charge", re.I),
    "letting": re.compile(r"\blett(?:ing|ings)\b(?:\s+(?:agents?|firms?|agencies|companies))?", re.I),
    "sales": re.compile(r"\b(?:land sales|sales\s+(?:firms?|agents?|agencies|companies)|selling\s+\w+|developers?|off-plan)\b", re.I),
}
MOSTLY_SALES = re.compile(
    r"(?:(?:skip|exclude|excluding|no|not|avoid|drop|without)\s+(?:\w+\s+){0,3}?)?(?:firms?\s+that\s+are\s+)?(?:mostly|mainly|largely|primarily)[- ]sales"
    r"|sales[- ]only(?:\s+firms?)?|(?:just|only)\s+sell\w*",
    re.I,
)
_RENT_NOUN = r"(?:rentals?|rental units?|homes?\s+for\s+rent|homes?\s+to\s+let|units?\s+for\s+rent|properties\s+for\s+rent|lettable\s+units?|homes\s+under\s+management)"
RENT_RANGE = re.compile(rf"(\d+)\s*(?:to|-)\s*(\d+)\s+{_RENT_NOUN}", re.I)
RENT_MIN = re.compile(rf"(?:(at least|a minimum of|minimum of|over|more than|upwards of)\s+)?(\d+)(\+)?\s*(?:or more\s+)?{_RENT_NOUN}", re.I)
RENT_MAX = re.compile(rf"(at most|up to|fewer than|less than|under|no more than)\s+(\d+)\s+{_RENT_NOUN}", re.I)
RENT_ANY = re.compile(r"(?:a\s+)?(?:rental book|rent roll)(?:\s+to\s+manage)?|(?:homes|properties|rentals)\s+to\s+manage|(?:a\s+)?rental\s+portfolio", re.I)
SHORT_MIN = re.compile(r"(?:(at least|a minimum of|minimum of|over|more than)\s+)?(\d+)(\+)?\s*(?:or more\s+)?short[- ]?lets?", re.I)
SHORT_ANY = re.compile(r"short[- ]?let\w*(?:\s+(?:operators?|counts?\s+too|too|managers?|hosts?))?", re.I)
REGISTER = re.compile(
    r"(?:on|in)\s+the\s+(?:professional\s+)?register|registered\s+(?:estate\s+)?(?:surveyors?|firms?|members?)|licen[cs]ed(?:\s+(?:firms?|surveyors?))?|\bESVs?\b|NIESV\s+members?",
    re.I,
)
PRINCIPAL = re.compile(
    r"principal\s+partners?|managing\s+(?:partners?|directors?)|\bfounders?\b|\bowners?\b|decision[- ]makers?|the\s+(?:person|people)\s+who\s+runs?\s+(?:the\s+|each\s+)?firms?|heads?\s+of\s+(?:the\s+)?firms?",
    re.I,
)
EITHER = re.compile(r"e-?mail\s+or\s+(?:phone|whatsapp|a call)|(?:phone|whatsapp)\s+or\s+e-?mail", re.I)
EMAIL = re.compile(
    r"(?:reachable|contactable|reached|contact(?:ed)?|reach)\s+(?:by|via|on|through)\s+e-?mail|with\s+(?:an?\s+)?(?:working\s+|verified\s+|deliverable\s+)?e-?mail(?:\s+address)?|e-?mail\s+address(?:es)?",
    re.I,
)
PHONE = re.compile(
    r"(?:reachable|contactable|reached|contact(?:ed)?|reach)\s+(?:by|via|on|through)\s+(?:phone|whatsapp|a call|telephone)|with\s+(?:a\s+)?(?:phone|whatsapp)(?:\s+number)?|phone\s+numbers?|on\s+whatsapp",
    re.I,
)
UNSUPPORTED = [
    (re.compile(r"(?:a\s+)?team\s+of\s+\d+(?:\s+or\s+more|\+)?|\d+\+?\s+(?:staff|employees)|(?:staff|team|head)\s*(?:size|count)", re.I), "team size"),
    (re.compile(r"(?:annual\s+)?(?:revenue|turnover)(?:\s+(?:of|above|over)\s+[\w$₦,.]+)?", re.I), "revenue"),
    (re.compile(r"(?:use|uses|using)\s+(?:excel|spreadsheets?|google sheets|software|a crm|property software)", re.I), "software they use"),
    (re.compile(r"\b(?:raised|funded|funding|investors?)\b", re.I), "funding"),
    (re.compile(r"\d+\+?\s+years?\s+in\s+business|(?:founded|established)\s+(?:before|after|in)\s+\d{4}", re.I), "how long the firm has run"),
    (re.compile(r"\b(?:instagram|facebook|linkedin|social media)\b", re.I), "social media"),
]


def parse_baseline(description: str) -> dict:
    text = description or ""
    raw: dict = {"firm_types": [], "roles": [], "locations": [], "exclude_locations": [], "unsupported": []}
    taken: list[tuple[int, int]] = []

    def free(m: re.Match) -> bool:
        return not any(m.start() < b and a < m.end() for a, b in taken)

    if m := MOSTLY_SALES.search(text):
        raw["mostly_sales"] = {"evidence": m.group(0)}
        taken.append(m.span())
    for m in EXCLUDE_CITIES.finditer(text):
        taken.append(m.span())
        for city in re.findall(_CITY, m.group(0), re.I):
            raw["exclude_locations"].append({"value": city, "evidence": m.group(0)})
    for m in INCLUDE_CITIES.finditer(text):
        if free(m):
            for city in re.findall(_CITY, m.group(0), re.I):
                raw["locations"].append({"value": city, "evidence": m.group(0)})
    for kind, rx in FIRM.items():
        for m in rx.finditer(text):
            if free(m):
                raw["firm_types"].append({"value": kind, "evidence": m.group(0)})
    if m := PRINCIPAL.search(text):
        raw["roles"].append({"value": "principal", "evidence": m.group(0)})

    if m := RENT_RANGE.search(text):
        raw["rental_book"] = {"min": int(m.group(1)), "max": int(m.group(2)), "evidence": m.group(0)}
    elif m := RENT_MIN.search(text):
        n = int(m.group(2))
        raw["rental_book"] = {"min": n + 1 if (m.group(1) or "").lower() in ("over", "more than") else n, "max": None, "evidence": m.group(0)}
    elif m := RENT_MAX.search(text):
        n = int(m.group(2))
        raw["rental_book"] = {"min": None, "max": n - 1 if m.group(1).lower() in ("fewer than", "less than", "under") else n, "evidence": m.group(0)}
    elif m := RENT_ANY.search(text):
        raw["rental_book"] = {"min": None, "max": None, "evidence": m.group(0)}
    if m := SHORT_MIN.search(text):
        n = int(m.group(2))
        raw["short_lets"] = {"min": n + 1 if (m.group(1) or "").lower() in ("over", "more than") else n, "evidence": m.group(0)}
    elif m := SHORT_ANY.search(text):
        raw["short_lets"] = {"min": None, "evidence": m.group(0)}
    if m := REGISTER.search(text):
        raw["register"] = {"evidence": m.group(0)}
    if m := EITHER.search(text):
        raw["channel"] = {"value": "either", "evidence": m.group(0)}
    elif m := EMAIL.search(text):
        raw["channel"] = {"value": "email", "evidence": m.group(0)}
    elif m := PHONE.search(text):
        raw["channel"] = {"value": "phone", "evidence": m.group(0)}
    for rx, what in UNSUPPORTED:
        for m in rx.finditer(text):
            raw["unsupported"].append({"what": what, "evidence": m.group(0)})
    return ground(raw, text, "baseline")


# ------------------------------------------------------------------ Claude

SYSTEM = """You turn a plain-English description of an ideal customer into criteria for a lead scorer.

The directory being scored only contains:
- kinds of firm: management (property or facility management, rent collection), letting, sales
- cities: Lagos, Abuja, Port Harcourt, Ibadan, Enugu
- listing counts: homes for rent, short-lets, homes for sale
- membership of the professional register
- who the contact is: principal (named as running the firm) or agent (the person on a portal listing)
- contact channels: email, phone

Return JSON matching the schema. For every criterion include "evidence": the exact words of the description it came
from, copied character for character, covering the whole clause (keep words such as "must", "only", "ideally" or
"skip"). The server checks every evidence phrase against the description and drops anything it can't find, so never
paraphrase and never add a criterion the description doesn't state. Use null or an empty list for anything not
mentioned. Numbers go in min/max only when the description gives them.

mostly_sales: set it when the description says firms that mostly sell are a poor fit or should be skipped.
unsupported: anything the description asks for that the directory can't provide (team size, revenue, software,
funding, social media...). Put it here with its evidence; never map it onto another criterion."""

_EV = {"type": "string"}


def _list_of(values) -> dict:
    return {
        "type": "array",
        "items": {"type": "object", "additionalProperties": False, "required": ["value", "evidence"],
                  "properties": {"value": {"type": "string", "enum": list(values)}, "evidence": _EV}},
    }


def _nullable(obj: dict) -> dict:
    return {"anyOf": [obj, {"type": "null"}]}


_INT = {"anyOf": [{"type": "integer"}, {"type": "null"}]}
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["firm_types", "roles", "locations", "exclude_locations", "rental_book", "short_lets", "register", "channel", "mostly_sales", "unsupported"],
    "properties": {
        "firm_types": _list_of(FIRM_TYPES),
        "roles": _list_of(ROLES),
        "locations": _list_of(CITIES),
        "exclude_locations": _list_of(CITIES),
        "rental_book": _nullable({"type": "object", "additionalProperties": False, "required": ["min", "max", "evidence"],
                                  "properties": {"min": _INT, "max": _INT, "evidence": _EV}}),
        "short_lets": _nullable({"type": "object", "additionalProperties": False, "required": ["min", "evidence"],
                                 "properties": {"min": _INT, "evidence": _EV}}),
        "register": _nullable({"type": "object", "additionalProperties": False, "required": ["evidence"], "properties": {"evidence": _EV}}),
        "channel": _nullable({"type": "object", "additionalProperties": False, "required": ["value", "evidence"],
                              "properties": {"value": {"type": "string", "enum": list(CHANNELS)}, "evidence": _EV}}),
        "mostly_sales": _nullable({"type": "object", "additionalProperties": False, "required": ["evidence"], "properties": {"evidence": _EV}}),
        "unsupported": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["what", "evidence"],
                                                   "properties": {"what": {"type": "string"}, "evidence": _EV}}},
    },
}


def parse_claude(description: str, client=None, model: str | None = None) -> tuple[dict, dict]:
    """Returns (icp, usage). Same call pattern as profile extraction: JSON schema, low effort, cached system prompt."""
    from .extract import usage_of

    model = model or os.environ.get("LEAD_ENGINE_MODEL", "claude-opus-5")
    if client is None:
        import anthropic

        client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=model,
        max_tokens=4000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": f"Description:\n\n{description}"}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
    )
    usage = usage_of(response.usage, model)
    if response.stop_reason == "refusal":
        icp = ground({}, description, model)
        icp["dropped"].append({"field": "*", "value": "", "why": "the model declined"})
        return icp, usage
    body = next((b.text for b in response.content if b.type == "text"), "{}")
    try:
        raw = json.loads(body)
    except json.JSONDecodeError:
        icp = ground({}, description, model)
        icp["dropped"].append({"field": "*", "value": "", "why": "the model's output was not valid JSON"})
        return icp, usage
    return ground(raw, description, model), usage


def claude_on() -> bool:
    return os.environ.get("LEAD_ENGINE_MODE") == "anthropic" and bool(os.environ.get("ANTHROPIC_API_KEY"))


def parse(description: str) -> tuple[dict, dict]:
    """Claude when switched on and a key is present; the baseline otherwise. A failed Claude call falls back, and says so."""
    if claude_on():
        try:
            return parse_claude(description)
        except Exception as e:  # network, auth, overload: the baseline still gives an answer
            icp = parse_baseline(description)
            icp["note"] = f"Claude could not be reached ({type(e).__name__}), so the baseline parser read it."
            return icp, {}
    return parse_baseline(description), {}


# ------------------------------------------------------------------ editing, saving, versions

def _int(v, lo: int, hi: int, default=None):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def normalise(data: dict) -> dict:
    """Validate an ICP coming back from the form. Unknown values are dropped, numbers clamped, and any evidence
    that is no longer in the description is removed, which marks that criterion as set by hand."""
    icp = blank()
    icp["description"] = str(data.get("description") or "")[:2000]
    icp["parser"] = str(data.get("parser") or "by hand")[:60]
    text = _norm(icp["description"])
    incoming = data.get("criteria") or {}
    for key in ORDER:
        src, dst = incoming.get(key) or {}, icp["criteria"][key]
        dst["on"] = bool(src.get("on"))
        dst["must"] = True if key in ("channel", "exclude_locations") else bool(src.get("must"))
        dst["points"] = _int(src.get("points"), -100, 100, dst["points"])
        if key == "firm_type":
            dst["name_points"] = _int(src.get("name_points"), -100, 100, dst["name_points"])
            dst["types"] = [t for t in FIRM_TYPES if t in (src.get("types") or [])]
        if key in ("location", "exclude_locations"):
            dst["cities"] = [c for c in CITIES if c in (src.get("cities") or [])]
        if key == "role":
            dst["roles"] = [r for r in ROLES if r in (src.get("roles") or [])]
        if key == "channel":
            dst["value"] = src.get("value") if src.get("value") in CHANNELS else "email"
        if key in ("rental_book", "short_lets"):
            dst["min"] = _int(src.get("min"), 0, 10000)
        if key == "rental_book":
            dst["max"] = _int(src.get("max"), 0, 10000)
            if dst["min"] is not None and dst["max"] is not None and dst["max"] < dst["min"]:
                dst["min"], dst["max"] = dst["max"], dst["min"]
        dst["evidence"] = [
            {"phrase": str(e.get("phrase")), "for": e.get("for")}
            for e in (src.get("evidence") or [])
            if isinstance(e, dict) and e.get("phrase") and _norm(str(e["phrase"])) in text
        ]
        dst["by_hand"] = bool(src.get("by_hand")) or (dst["on"] and not dst["evidence"])
        if key in ("firm_type", "location", "role", "exclude_locations") and not (dst.get("types") or dst.get("cities") or dst.get("roles")):
            dst["on"] = False
    t1 = _int((data.get("tiers") or {}).get("tier1"), 1, 300, 65)
    t2 = _int((data.get("tiers") or {}).get("tier2"), 0, 300, 45)
    icp["tiers"] = {"tier1": max(t1, t2 + 1), "tier2": min(t2, t1 - 1)}
    icp["unsupported"] = [
        {"what": str(u.get("what"))[:80], "phrase": str(u.get("phrase"))}
        for u in (data.get("unsupported") or [])
        if isinstance(u, dict) and u.get("phrase") and _norm(str(u["phrase"])) in text
    ]
    icp["dropped"] = [
        {k: str(d.get(k, ""))[:200] for k in ("field", "value", "why")} for d in (data.get("dropped") or [])[:30] if isinstance(d, dict)
    ]
    return icp


def default_icp() -> dict:
    return parse_baseline(DEFAULT_DESCRIPTION)


def _insert(conn: sqlite3.Connection, icp: dict, label: str) -> int:
    cur = conn.execute(
        "insert into icp_versions (description, parser, icp, created_at) values (?, ?, ?, ?)",
        (icp["description"], label, json.dumps(icp), db.now()),
    )
    conn.commit()
    return cur.lastrowid


def _ensure_default(conn: sqlite3.Connection) -> None:
    """Version 1 is always the built-in default, so going back to the original scoring is one click."""
    if conn.execute("select 1 from icp_versions limit 1").fetchone() is None:
        _insert(conn, normalise(default_icp()), "baseline (built-in default)")


def save(conn: sqlite3.Connection, data: dict) -> int:
    _ensure_default(conn)
    icp = normalise(data)
    edited = any(icp["criteria"][k]["by_hand"] for k in ORDER if icp["criteria"][k]["on"])
    label = icp["parser"] + (", edited by hand" if edited and "edited" not in icp["parser"] else "")
    return _insert(conn, icp, label)


def current(conn: sqlite3.Connection) -> tuple[int, dict]:
    """The latest saved ICP. A new database starts with the built-in default, which reproduces the original scoring."""
    _ensure_default(conn)
    row = conn.execute("select version, icp from icp_versions order by version desc limit 1").fetchone()
    return row["version"], json.loads(row["icp"])


def versions(conn: sqlite3.Connection, limit: int = 12) -> list[dict]:
    return [
        {"version": r["version"], "description": r["description"], "parser": r["parser"], "created_at": r["created_at"]}
        for r in conn.execute("select version, description, parser, created_at from icp_versions order by version desc limit ?", (limit,))
    ]


def get_version(conn: sqlite3.Connection, version: int) -> dict | None:
    row = conn.execute("select icp from icp_versions where version = ?", (version,)).fetchone()
    return json.loads(row["icp"]) if row else None


def needs(icp: dict) -> list[dict]:
    """The capabilities each switched-on criterion needs from the sources, for the coverage check."""
    out = []
    c = icp["criteria"]
    for key in ORDER:
        crit = c[key]
        if not crit["on"]:
            continue
        if key == "channel":
            caps = {"email": ("email",), "phone": ("phone",), "either": ("email", "phone")}[crit["value"]]
        elif key == "role":
            caps = tuple(sorted({"principal": "role", "agent": "agent"}[r] for r in crit["roles"]))
        else:
            caps = NEEDS[key]
        out.append({"criterion": key, "label": LABELS[key], "caps": list(caps), "must": crit["must"]})
    return out


def copy_of(icp: dict) -> dict:
    return copy.deepcopy(icp)
