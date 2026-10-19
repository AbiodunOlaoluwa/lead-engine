"""A points score anyone can read, set by the ICP. When someone asks why a lead is at the top, the breakdown is the answer:
every line names the criterion, its points, and the phrase of the ICP it came from.

A must-have the contact fails puts it outside the ICP: it keeps its points for reference, drops to tier 3, and is held
back from the export. The built-in default ICP reproduces the original fixed scoring exactly."""
from __future__ import annotations

OPS_WORDS = ("property management", "rent collection", "facility management", "service charge", "tenant")
TYPE_WORDS = {
    "management": OPS_WORDS,
    "letting": ("letting", "short-let management", "tenant sourcing"),
    "sales": ("property sales", "land sales", "valuation", "off-plan"),
}
NAME_WORDS = {"management": ("management",), "letting": ("letting",), "sales": ("realty", "sales")}
ROLE_TEXT = {"principal": "named as running the firm on its own page", "agent": "the contact on a portal listing"}


def _phrases(crit: dict) -> list[str]:
    seen: list[str] = []
    for e in crit.get("evidence") or []:
        if e["phrase"] not in seen:
            seen.append(e["phrase"])
    return seen


def score(rec: dict, icp: dict | None = None) -> tuple[int, int, list[dict]]:
    """Returns (points, tier, breakdown). Each breakdown line: points, why, criterion, kind (points | must | outside), evidence."""
    if icp is None:
        from .icp import default_icp

        icp = default_icp()
    c = icp["criteria"]
    parts: list[dict] = []

    def add(key: str, points: int, why: str, kind: str = "points"):
        parts.append({"points": points, "why": why, "criterion": key, "kind": kind, "evidence": _phrases(c[key])})

    def miss(key: str, why: str):
        if c[key]["must"]:
            add(key, 0, why, "outside")

    rent, short, sale = rec.get("for_rent") or 0, rec.get("short_let") or 0, rec.get("for_sale") or 0
    company = (rec.get("company") or "").lower()
    text = f"{rec.get('company', '')} {rec.get('services', '')}".lower()
    cities = set(rec.get("cities") or [])

    if c["register"]["on"]:
        if rec.get("licensed"):
            add("register", c["register"]["points"], "on the professional register")
        else:
            miss("register", "not on the professional register, which the ICP requires")

    rb = c["rental_book"]
    if rb["on"]:
        lo, hi = rb.get("min"), rb.get("max")
        if lo is None and hi is None:
            if rent:  # no threshold: a bigger book earns more, up to the criterion's points
                add("rental_book", min(rb["points"], 4 + rent // 6), f"{rent} homes for rent (a rental book to manage)")
            else:
                miss("rental_book", "no homes for rent, and the ICP needs a rental book")
        else:
            bounds = " and ".join(x for x in (f"at least {lo}" if lo is not None else "", f"at most {hi}" if hi is not None else "") if x)
            if rent and (lo is None or rent >= lo) and (hi is None or rent <= hi):
                add("rental_book", rb["points"], f"rental book {rent}, {bounds}")
            else:
                miss("rental_book", f"rental book {rent}, the ICP needs {bounds}")

    sl = c["short_lets"]
    if sl["on"]:
        lo = sl.get("min")
        if lo is None:
            if short:
                add("short_lets", min(sl["points"], 4 + short // 3), f"{short} short-lets")
            else:
                miss("short_lets", "no short-lets, and the ICP needs them")
        elif short and short >= lo:
            add("short_lets", sl["points"], f"{short} short-lets, at least {lo}")
        else:
            miss("short_lets", f"{short} short-lets, the ICP needs at least {lo}")

    ft = c["firm_type"]
    if ft["on"] and ft["types"]:
        does = [t for t in ft["types"] if any(w in text for w in TYPE_WORDS[t])]
        named = [t for t in ft["types"] if any(w in company for w in NAME_WORDS[t])]
        if does:
            label = "management work, not just sales" if does == ["management"] else " and ".join(does) + " work"
            add("firm_type", ft["points"], f"describes {label}")
        if named:
            word = next(w for t in named for w in NAME_WORDS[t] if w in company)
            add("firm_type", ft["name_points"], f'"{word.title()}" in the firm name')
        if not does and not named:
            miss("firm_type", f"does not describe {' or '.join(ft['types'])} work")

    ms = c["mostly_sales"]
    if ms["on"] and sale > max(rent + short, 1) * 2:
        if ms["must"]:
            add("mostly_sales", 0, f"mostly sales ({sale} for sale), which the ICP leaves out", "outside")
        else:
            add("mostly_sales", ms["points"], f"mostly sales ({sale} for sale)")

    loc = c["location"]
    if loc["on"] and loc["cities"]:
        hit = sorted(cities & set(loc["cities"]))
        if hit:
            add("location", loc["points"], f"in {', '.join(hit)}")
        else:
            miss("location", f"in {', '.join(sorted(cities)) or 'no city given'}, not {' or '.join(loc['cities'])}")

    ex = c["exclude_locations"]
    if ex["on"] and ex["cities"]:
        hit = sorted(cities & set(ex["cities"]))
        if hit:
            add("exclude_locations", 0, f"in {', '.join(hit)}, which the ICP leaves out", "outside")

    ro = c["role"]
    if ro["on"] and ro["roles"]:
        hit = [r for r in ro["roles"] if r in set(rec.get("roles") or [])]
        if hit:
            add("role", ro["points"], ROLE_TEXT[hit[0]])
        else:
            miss("role", "nobody " + " or ".join(ROLE_TEXT[r] for r in ro["roles"]))

    ch = c["channel"]
    if ch["on"]:
        email, phone = bool(rec.get("has_email")), bool(rec.get("has_phone"))
        reach = {"email": email, "phone": phone, "either": email or phone}[ch["value"]]
        has = {"email": "has a deliverable email", "phone": "has a phone number", "either": "has an email or a phone number"}[ch["value"]]
        lacks = {"email": "no deliverable email", "phone": "no phone number", "either": "neither an email nor a phone number"}[ch["value"]]
        if reach:
            add("channel", ch["points"], has, "points" if ch["points"] else "must")
        else:
            add("channel", 0, f"{lacks}, and the ICP needs one", "outside")

    total = sum(p["points"] for p in parts)
    t1, t2 = icp["tiers"]["tier1"], icp["tiers"]["tier2"]
    tier = 3 if not fits(parts) else 1 if total >= t1 else 2 if total >= t2 else 3
    return total, tier, parts


def fits(parts: list[dict]) -> bool:
    return not any(p.get("kind") == "outside" for p in parts)
