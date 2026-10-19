"""Normalising people, companies and phone numbers so the same entity can be recognised across sources.

Every rule here was learned the hard way on real Nigerian directories:
- Registers write names SURNAME FIRST ("ADEYEMI FUNMILAYO OLUWASEUN"); read as written, a greeting uses the wrong name.
- People go by short forms (Funmi for Olufunmilayo, Toyin for Oluwatoyin), so exact name matching misses them.
- Honorifics (ESV, Engr, Chief, Alhaji) and company suffixes (Ltd, Nig, & Co) hide matches.
- Phone numbers arrive as 0800..., 800..., +234 800... or two numbers in one cell.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

HONORIFICS = {
    "mr", "mrs", "ms", "miss", "dr", "engr", "esv", "arc", "barr", "chief", "alhaji", "alhaja", "otunba",
    "prof", "pastor", "rev", "sir", "hon", "mallam", "prince", "princess", "surv", "bldr", "qs", "pharm",
}

# Short form -> canonical full forms it can stand for. Matching treats any two names in one group as compatible.
SHORT_FORM_GROUPS: list[set[str]] = [
    {"funmi", "funmilayo", "olufunmilayo", "oluwafunmilayo"},
    {"toyin", "oluwatoyin", "toyosi"},
    {"seun", "oluwaseun"},
    {"tunde", "babatunde", "olatunde", "ayotunde"},
    {"bayo", "adebayo"},
    {"gbenga", "adegbenga", "oluwagbenga"},
    {"dami", "damilare", "oluwadamilare", "damilola", "oluwadamilola"},
    {"kemi", "oluwakemi", "kemisola"},
    {"tobi", "oluwatobi", "tobiloba"},
    {"femi", "oluwafemi", "olufemi"},
    {"deji", "ayodeji", "adedeji"},
    {"wale", "adewale", "olawale"},
    {"jide", "babajide", "olajide"},
    {"seyi", "oluwaseyi"},
    {"tope", "temitope"},
    {"bukky", "buki", "bukola", "oluwabukola"},
    {"bimbo", "abimbola"},
    {"lanre", "olanrewaju"},
    {"yemi", "oluwayemisi", "opeyemi"},
    {"emeka", "chukwuemeka"},
    {"nedu", "chinedu"},
    {"ngozi", "ngozichukwu"},
    {"uche", "uchechukwu", "uchenna"},
    {"obi", "obinna"},
    {"chi", "chioma", "chiamaka"},
    {"mohammed", "muhammad", "muhammed", "mohammad"},
    {"abu", "abubakar"},
    {"aisha", "aishat", "aishatu"},
]
_GROUP_OF: dict[str, int] = {n: i for i, g in enumerate(SHORT_FORM_GROUPS) for n in g}

COMPANY_SUFFIXES = {
    "ltd", "limited", "plc", "nig", "nigeria", "co", "company", "enterprises", "enterprise", "ventures",
    "services", "intl", "international", "inc", "llc", "global", "group", "partners",
}
COMPANY_ABBREVIATIONS = {"props": "properties", "prop": "properties", "mgt": "management", "mgmt": "management", "est": "estate", "&": "and"}


def fold(s: str) -> str:
    """Lowercase, strip accents and punctuation, collapse spaces."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9& ]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


@dataclass(frozen=True)
class Person:
    given: str  # first given name, folded
    middle: tuple[str, ...]
    surname: str

    @property
    def display(self) -> str:
        return " ".join(p.title() for p in (self.given, *self.middle, self.surname) if p)


def parse_person(raw: str, surname_first: bool = False) -> Person | None:
    """Parse a name. Registers write SURNAME FIRST; directories write given names first."""
    tokens = [t for t in fold(raw.replace(".", " ")).split() if t not in HONORIFICS]
    if not tokens:
        return None
    if len(tokens) == 1:
        return Person(given="", middle=(), surname=tokens[0])
    if surname_first:
        return Person(given=tokens[1], middle=tuple(tokens[2:]), surname=tokens[0])
    return Person(given=tokens[0], middle=tuple(tokens[1:-1]), surname=tokens[-1])


def given_names_compatible(a: str, b: str) -> bool:
    """Same name, same short-form group, or one is an initial of the other."""
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) == 1 or len(b) == 1:
        return a[0] == b[0]
    ga, gb = _GROUP_OF.get(a), _GROUP_OF.get(b)
    return ga is not None and ga == gb


def normalise_company(raw: str) -> str:
    """'Palmview Props Nig. Ltd' and 'PALMVIEW PROPERTIES LIMITED' both become 'palmview properties'."""
    words = [COMPANY_ABBREVIATIONS.get(w, w) for w in fold(raw).split()]
    words = [w for w in words if w not in COMPANY_SUFFIXES and w != "and"]
    return " ".join(words)


def normalise_phones(raw: str) -> list[str]:
    """Return E.164 numbers found in a cell. Handles 0800..., 800... (leading 0 dropped), +234..., and several per cell."""
    out: list[str] = []
    for chunk in re.split(r"[/,;]|\bor\b", raw or ""):
        digits = re.sub(r"\D", "", chunk)
        if digits.startswith("234") and len(digits) == 13:
            e164 = "+" + digits
        elif digits.startswith("0") and len(digits) == 11:
            e164 = "+234" + digits[1:]
        elif len(digits) == 10 and digits[0] in "789":
            e164 = "+234" + digits  # register exports often drop the leading 0
        else:
            continue
        if e164 not in out:
            out.append(e164)
    return out


EMAIL_OBFUSCATION = [
    (re.compile(r"(?<=[a-z0-9])\s+@\s+(?=[a-z0-9])", re.I), "@"),
    (re.compile(r"\s*[\[\(]\s*at\s*[\]\)]\s*", re.I), "@"),
    (re.compile(r"\s*[\[\(]\s*dot\s*[\]\)]\s*", re.I), "."),
    (re.compile(r"\s+at\s+(?=[a-z0-9-]+\s*(?:\.|\[dot\]|\(dot\)))", re.I), "@"),
]
# A literal dot may have spaces only on both sides ("cedarpoint . example"); a full stop ending a sentence never joins.
EMAIL = re.compile(r"[a-z0-9._%+-]+@[a-z0-9-]+(?:(?:\.|[ \t]+\.[ \t]+)[a-z0-9-]+)+", re.I)


def find_emails(text: str) -> list[str]:
    """Emails, including the '[at]' / '(dot)' disguises listings use to dodge scrapers."""
    t = text or ""
    for pattern, repl in EMAIL_OBFUSCATION:
        t = pattern.sub(repl, t)
    found = []
    for m in EMAIL.finditer(t):
        e = re.sub(r"\s+", "", m.group(0)).lower().rstrip(".")
        if e not in found:
            found.append(e)
    return found
