from lead_engine.match import Rec, compare


def rec(id, given, surname, company="palmview properties", phones=(), emails=(), city="Lagos"):
    return Rec(id, company, given, surname, list(phones), list(emails), city)


def test_shared_phone_links_automatically():
    s, _, d = compare(rec("a", "funmi", "adeyemi", phones=["+2348005551000"]), rec("b", "olufunmilayo", "adeyemi", phones=["+2348005551000"]))
    assert d == "linked"


def test_similar_names_go_to_review_never_merge():
    _, reasons, d = compare(rec("a", "funmi", "adeyemi"), rec("b", "olufunmilayo", "adeyemi"))
    assert d == "review"
    assert any("same name" in r for r in reasons)


def test_common_surname_alone_is_not_a_match():
    _, _, d = compare(rec("a", "amadi", "amadi", company="greenfield homes"), rec("b", "chinedu", "amadi", company="cedar point realty"))
    assert d == "different"


def test_same_firm_name_in_different_cities_is_not_merged():
    _, _, d = compare(rec("a", "tunde", "bello", city="Lagos"), rec("b", "temitope", "eze", city="Abuja"))
    assert d == "different"
