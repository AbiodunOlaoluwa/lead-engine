"""Recognising the same person or firm across listings.

Two kinds of evidence, treated differently:
- Shared identifiers (a phone number or email address): the same contact, linked automatically.
- Similar names (a short form, surname-first, a suffix or two): likely the same, sent to review.
A wrong merge loses a real lead and can message someone twice under the wrong name; a duplicate only costs a
second look. So fuzzy evidence never merges on its own."""
from __future__ import annotations

from dataclasses import dataclass

from .names import given_names_compatible

REVIEW_THRESHOLD = 0.6


@dataclass
class Rec:
    id: str
    company_norm: str
    given: str
    surname: str
    phones: list[str]
    emails: list[str]
    city: str


def _company_similarity(a: str, b: str) -> float:
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def compare(a: Rec, b: Rec) -> tuple[float, list[str], str]:
    """Returns (score 0..1, reasons, decision)."""
    shared_phone = set(a.phones) & set(b.phones)
    shared_email = set(a.emails) & set(b.emails)
    if shared_phone or shared_email:
        what = ", ".join(sorted(shared_phone | shared_email))
        return 1.0, [f"same contact detail: {what}"], "linked"

    reasons: list[str] = []
    score = 0.0
    same_surname = bool(a.surname) and a.surname == b.surname
    given_ok = given_names_compatible(a.given, b.given)
    company = _company_similarity(a.company_norm, b.company_norm)

    if same_surname and given_ok:
        score += 0.55
        reasons.append(
            "same person by name" if a.given == b.given else f"same surname, '{a.given}' and '{b.given}' are the same name"
        )
    elif same_surname:
        reasons.append("same surname only")  # Amadi Amadi problem: common surnames prove nothing alone
        score += 0.1
    if company >= 0.99:
        score += 0.35
        reasons.append("same firm name")
    elif company >= 0.5:
        score += 0.2
        reasons.append(f"similar firm name ({company:.0%} of words shared)")
    if a.city and b.city and a.city.lower() == b.city.lower():
        score += 0.1
        reasons.append("same city")
    elif a.city and b.city:
        score -= 0.2
        reasons.append("different cities")

    decision = "review" if score >= REVIEW_THRESHOLD and (same_surname and given_ok or company >= 0.99) else "different"
    return round(min(score, 0.99), 2), reasons, decision


def candidate_pairs(recs: list[Rec]) -> list[tuple[Rec, Rec]]:
    """Blocking: only compare records that share a surname, a firm-name word or a contact detail."""
    keys: dict[str, list[Rec]] = {}
    for r in recs:
        ks = {f"s:{r.surname}"} if r.surname else set()
        ks |= {f"c:{w}" for w in r.company_norm.split() if len(w) > 3}
        ks |= {f"p:{p}" for p in r.phones} | {f"e:{e}" for e in r.emails}
        for k in ks:
            keys.setdefault(k, []).append(r)
    seen: set[tuple[str, str]] = set()
    pairs = []
    for group in keys.values():
        if len(group) > 60:  # a word like "properties" blocks nothing useful
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = sorted((group[i], group[j]), key=lambda r: r.id)
                if (a.id, b.id) not in seen:
                    seen.add((a.id, b.id))
                    pairs.append((a, b))
    return pairs
