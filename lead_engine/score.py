"""A points score anyone can read. When someone asks why a lead is at the top, the breakdown is the answer."""
from __future__ import annotations

OPS_WORDS = ("property management", "rent collection", "facility management", "service charge", "tenant")


def score(rec: dict) -> tuple[int, int, list[dict]]:
    """Returns (points, tier, breakdown). Tier 1 >= 65, tier 2 >= 45, else 3."""
    parts: list[dict] = []

    def add(points: int, why: str):
        parts.append({"points": points, "why": why})

    if rec.get("licensed"):
        add(25, "licensed: on the professional register")
    rent, short, sale = rec.get("for_rent") or 0, rec.get("short_let") or 0, rec.get("for_sale") or 0
    if rent:
        add(min(20, 4 + rent // 6), f"{rent} homes for rent (a rental book to manage)")
    if short:
        add(min(20, 4 + short // 3), f"{short} short-lets")
    text = f"{rec.get('company', '')} {rec.get('services', '')}".lower()
    if any(w in text for w in OPS_WORDS):
        add(10, "describes management work, not just sales")
    if "management" in (rec.get("company") or "").lower():
        add(8, '"Management" in the firm name')
    if sale > max(rent + short, 1) * 2:
        add(-15, f"mostly sales ({sale} for sale)")
    total = sum(p["points"] for p in parts)
    tier = 1 if total >= 65 else 2 if total >= 45 else 3
    return total, tier, parts
