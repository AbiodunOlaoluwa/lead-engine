"""Turn fetched pages into listings. Each page records how many listings it showed and how many we kept:
a silent gap between the two is how a parser that skips a layout gets caught."""
from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

from .names import find_emails, normalise_phones


def _int(s: str | None) -> int:
    m = re.search(r"\d+", s or "")
    return int(m.group(0)) if m else 0


def parse_portal_page(body: str, url: str) -> tuple[list[dict], int]:
    soup = BeautifulSoup(body, "html.parser")
    cards = soup.select(".listing")
    out: list[dict] = []
    for card in cards:
        lid = card.get("data-id")
        if not isinstance(lid, str):
            continue
        if "listing--premium" in (card.get("class") or []):
            out.append(_premium(card, lid, url))
        else:
            out.append(_standard(card, lid, url))
    return out, len(cards)


def _text(node: Tag, selector: str) -> str:
    el = node.select_one(selector)
    return el.get_text(" ", strip=True) if el else ""


def _standard(card: Tag, lid: str, url: str) -> dict:
    h3 = card.select_one("h3")
    rank = h3.select_one(".rank") if h3 else None
    if rank:
        rank.extract()
    profile = card.select_one("a.profile")
    return {
        "id": lid,
        "source": "portal",
        "url": url,
        "layout": "standard",
        "company": h3.get_text(" ", strip=True) if h3 else "",
        "contact_raw": _text(card, ".contact"),
        "phones": normalise_phones(_text(card, ".phone")),
        "emails": find_emails(_text(card, ".email")),
        "city": _text(card, ".city"),
        "for_rent": _int(_text(card, ".rent")),
        "short_let": _int(_text(card, ".short")),
        "for_sale": _int(_text(card, ".sale")),
        "services": _text(card, ".services"),
        "profile_href": profile.get("href") if profile else None,
    }


def _premium(card: Tag, lid: str, url: str) -> dict:
    counts = {li.get("data-k"): _int(li.get_text()) for li in card.select(".counts li")}
    profile = card.select_one("a.profile")
    return {
        "id": lid,
        "source": "portal",
        "url": url,
        "layout": "premium",
        "company": _text(card, "h3"),
        "contact_raw": _text(card, ".agent").removeprefix("Agent:").strip(),
        "phones": normalise_phones(_text(card, ".tel")),
        "emails": find_emails(_text(card, ".mail")),
        "city": _text(card, ".loc"),
        "for_rent": counts.get("rent", 0),
        "short_let": counts.get("short", 0),
        "for_sale": counts.get("sale", 0),
        "services": _text(card, ".about"),
        "profile_href": profile.get("href") if profile else None,
    }


def parse_register_page(body: str, url: str) -> tuple[list[dict], int]:
    soup = BeautifulSoup(body, "html.parser")
    rows = [r for r in soup.select("tr") if r.get("data-id")]
    out = []
    for r in rows:
        cells = [c.get_text(" ", strip=True) for c in r.select("td")]
        if len(cells) < 5:
            continue
        member_no, person, firm, tel, state = cells[:5]
        out.append(
            {
                "id": r.get("data-id"),
                "source": "register",
                "url": url,
                "layout": "register",
                "company": firm,
                "contact_raw": person,  # SURNAME FIRST: parsed with surname_first=True later
                "phones": normalise_phones(tel),
                "emails": [],
                "city": state.title(),
                "for_rent": None,
                "short_let": None,
                "for_sale": None,
                "services": "",
                "licensed": 1,
                "member_no": member_no,
            }
        )
    return out, len(rows)


def profile_text(body: str) -> tuple[str, str]:
    """The company name and the visible text of a free-text profile page."""
    soup = BeautifulSoup(body, "html.parser")
    main = soup.select_one("main") or soup
    h1 = main.select_one("h1")
    company = h1.get_text(" ", strip=True) if h1 else ""
    paras = [p.get_text(" ", strip=True) for p in main.select("p")]
    return company, "\n".join(paras)
