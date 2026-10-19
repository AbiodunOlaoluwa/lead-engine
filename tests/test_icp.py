"""The ICP: read from plain English, grounded in the description, and driving the score."""
import json
from types import SimpleNamespace

from lead_engine import db, icp
from lead_engine.directory import generate
from lead_engine.pipeline import Run
from lead_engine.score import score


def on(parsed: dict) -> dict:
    return {k: v for k, v in parsed["criteria"].items() if v["on"]}


def test_baseline_reads_the_directory_vocabulary():
    parsed = icp.parse_baseline(
        "Property management firms in Lagos with at least 10 homes for rent, ideally on the professional register. Reachable by email."
    )
    c = on(parsed)
    assert c["firm_type"]["types"] == ["management"]
    assert c["location"]["cities"] == ["Lagos"]
    assert (c["rental_book"]["min"], c["rental_book"]["max"]) == (10, None)
    assert c["register"]["must"] is False  # "ideally": a preference, not a must-have
    assert c["channel"]["value"] == "email"
    assert parsed["dropped"] == []
    # every criterion carries the words it came from
    for crit in c.values():
        assert crit["evidence"] and all(e["phrase"] in parsed["description"] for e in crit["evidence"])


def test_must_and_skip_are_read_from_the_sentence():
    parsed = icp.parse_baseline("Short-let operators in Abuja or Port Harcourt with at least 5 short-lets. Must be on the register. Skip firms that are mostly sales.")
    c = on(parsed)
    assert c["register"]["must"] is True
    assert c["mostly_sales"]["must"] is True  # "Skip": left out, not just penalised
    assert c["location"]["cities"] == ["Abuja", "Port Harcourt"] and c["location"]["must"] is False
    assert c["short_lets"]["min"] == 5


def test_something_no_source_has_is_reported_not_mapped():
    parsed = icp.parse_baseline("Principal partners at letting firms in Ibadan with a team of 10 or more and at least 20 rentals, reachable by phone.")
    assert parsed["unsupported"] == [{"what": "team size", "phrase": "a team of 10 or more"}]
    assert on(parsed)["rental_book"]["min"] == 20  # the 10 belongs to the team, not the rental book
    assert on(parsed)["role"]["roles"] == ["principal"]


def test_invented_evidence_is_dropped():
    raw = {
        "locations": [{"value": "Lagos", "evidence": "in Lagos"}, {"value": "Abuja", "evidence": "and Abuja"}],
        "register": {"evidence": "must be licensed"},
        "rental_book": {"min": 50, "max": None, "evidence": "with a rental book"},
        "firm_types": [{"value": "sales", "evidence": "in Lagos"}],
    }
    parsed = icp.ground(raw, "Firms in Lagos with a rental book.", "test")
    c = parsed["criteria"]
    assert c["location"]["cities"] == ["Lagos"]
    assert c["register"]["on"] is False
    assert c["rental_book"]["on"] and c["rental_book"]["min"] is None  # the phrase is real, the 50 is not in it
    assert c["firm_type"]["on"] is False  # "in Lagos" does not say sales
    whys = {(d["field"], d["why"]) for d in parsed["dropped"]}
    assert ("location", "its phrase is not in the description") in whys
    assert ("register", "its phrase is not in the description") in whys
    assert any(f == "rental_book" and "does not contain 50" in w for f, w in whys)
    assert any(f == "firm_type" and "does not say that" in w for f, w in whys)


def test_unknown_city_is_dropped():
    parsed = icp.ground({"locations": [{"value": "Kano", "evidence": "in Kano"}]}, "Agents in Kano.", "test")
    assert parsed["criteria"]["location"]["on"] is False
    assert parsed["dropped"][0]["why"] == "no firm in the directory is in that city"


class FakeClient:
    """Stands in for anthropic.Anthropic(): records the request, returns a canned structured output."""

    def __init__(self, payload: dict, stop_reason: str = "end_turn"):
        self.payload, self.stop_reason, self.calls = payload, stop_reason, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        usage = SimpleNamespace(input_tokens=900, output_tokens=120, cache_read_input_tokens=0, cache_creation_input_tokens=0)
        return SimpleNamespace(stop_reason=self.stop_reason, usage=usage, content=[SimpleNamespace(type="text", text=json.dumps(self.payload))])


def test_claude_parser_goes_through_the_same_grounding():
    text = "Lettings firms in Enugu, reachable by email or phone. Avoid anyone mostly selling land."
    payload = {
        "firm_types": [{"value": "letting", "evidence": "Lettings firms"}],
        "roles": [],
        "locations": [{"value": "Enugu", "evidence": "in Enugu"}, {"value": "Lagos", "evidence": "in Lagos"}],
        "exclude_locations": [],
        "rental_book": None,
        "short_lets": None,
        "register": None,
        "channel": {"value": "either", "evidence": "reachable by email or phone"},
        "mostly_sales": {"evidence": "Avoid anyone mostly selling land"},
        "unsupported": [],
    }
    client = FakeClient(payload)
    parsed, usage = icp.parse_claude(text, client=client, model="claude-opus-5")
    req = client.calls[0]
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert req["fallbacks"] == "default"
    c = parsed["criteria"]
    assert parsed["parser"] == "claude-opus-5"
    assert c["firm_type"]["types"] == ["letting"]
    assert c["location"]["cities"] == ["Enugu"]  # Lagos had no phrase in the text
    assert c["channel"]["value"] == "either"
    assert c["mostly_sales"]["must"] is True  # "Avoid"
    assert usage["cost_micros"] > 0


def test_claude_refusal_gives_an_empty_icp_not_a_guess():
    parsed, _ = icp.parse_claude("anything", client=FakeClient({}, stop_reason="refusal"))
    assert not on(parsed)
    assert parsed["dropped"][0]["why"] == "the model declined"


def test_default_icp_reproduces_the_original_scoring(tmp_path):
    """The default ICP was written to score exactly like the fixed rules it replaced."""

    def legacy(rec):
        total = 0
        if rec.get("licensed"):
            total += 25
        rent, short, sale = rec.get("for_rent") or 0, rec.get("short_let") or 0, rec.get("for_sale") or 0
        total += min(20, 4 + rent // 6) if rent else 0
        total += min(20, 4 + short // 3) if short else 0
        text = f"{rec.get('company', '')} {rec.get('services', '')}".lower()
        total += 10 if any(w in text for w in ("property management", "rent collection", "facility management", "service charge", "tenant")) else 0
        total += 8 if "management" in (rec.get("company") or "").lower() else 0
        total -= 15 if sale > max(rent + short, 1) * 2 else 0
        return total, 1 if total >= 65 else 2 if total >= 45 else 3

    meta = generate(tmp_path / "site", n=60, seed=5)
    default = icp.default_icp()
    for f in meta["firms"]:
        rec = {"company": f["company"], "licensed": f["licensed"], "for_rent": f["for_rent"], "short_let": f["short_let"],
               "for_sale": f["for_sale"], "services": ", ".join(f["services"])}
        points, tier, _ = score(rec, default)
        assert (points, tier) == legacy(rec), f["company"]


def test_icp_drives_the_score_and_names_its_evidence(tmp_path):
    generate(tmp_path / "site", n=40, seed=3)
    conn = db.connect(":memory:")
    run = Run(conn, tmp_path / "site")
    run.all()
    strict = icp.parse_baseline("Only firms in Lagos. Must be on the register. Reachable by email.")
    version = icp.save(conn, strict)
    Run(conn, tmp_path / "site", icp=icp.get_version(conn, version), icp_version=version).only("score")
    rows = conn.execute("select * from listings").fetchall()
    assert {r["icp_version"] for r in rows} == {version}
    for r in rows:
        parts = json.loads(r["score_breakdown"])
        outside = [p for p in parts if p["kind"] == "outside"]
        assert (r["icp_fit"] == 0) == bool(outside)
        if r["icp_fit"] == 0:
            assert r["tier"] == 3
        for p in parts:
            assert p["evidence"], p  # every line points back to the ICP's words
    reg = [p for r in rows for p in json.loads(r["score_breakdown"]) if p["criterion"] == "register" and p["kind"] == "points"]
    assert reg and reg[0]["evidence"] == ["on the register"]


def test_saving_drops_evidence_that_is_not_in_the_description():
    parsed = icp.parse_baseline("Property management firms in Lagos.")
    parsed["criteria"]["location"]["cities"].append("Ibadan")
    parsed["criteria"]["location"]["evidence"].append({"phrase": "in Ibadan", "for": "Ibadan"})
    clean = icp.normalise(parsed)
    phrases = [e["phrase"] for e in clean["criteria"]["location"]["evidence"]]
    assert phrases == ["in Lagos"]
    assert clean["criteria"]["location"]["cities"] == ["Lagos", "Ibadan"]
    clean["criteria"]["register"]["on"] = True  # switched on by hand: no words behind it
    assert icp.normalise(clean)["criteria"]["register"]["by_hand"] is True
