from lead_engine.names import find_emails, given_names_compatible, normalise_company, normalise_phones, parse_person


def test_register_names_are_surname_first():
    p = parse_person("ADEYEMI OLUFUNMILAYO OLUWASEUN", surname_first=True)
    assert (p.given, p.surname) == ("olufunmilayo", "adeyemi")


def test_honorifics_are_ignored():
    p = parse_person("ESV Engr. Toyin Okafor")
    assert (p.given, p.surname) == ("toyin", "okafor")


def test_short_forms():
    assert given_names_compatible("funmi", "olufunmilayo")
    assert given_names_compatible("emeka", "chukwuemeka")
    assert given_names_compatible("o", "oluwafemi")  # an initial
    assert not given_names_compatible("seun", "seyi")
    assert not given_names_compatible("bayo", "gbenga")


def test_company_suffixes_and_abbreviations():
    assert normalise_company("Palmview Props Nig. Ltd") == normalise_company("PALMVIEW PROPERTIES LIMITED") == "palmview properties"


def test_phone_formats():
    assert normalise_phones("0800 555 1017") == ["+2348005551017"]
    assert normalise_phones("800 555 1017") == ["+2348005551017"]  # leading zero dropped by an export
    assert normalise_phones("+234-800-555-1017") == ["+2348005551017"]
    assert normalise_phones("0800 555 1017 / 0809 555 2017") == ["+2348005551017", "+2348095552017"]
    assert normalise_phones("2,500,000 naira") == []


def test_email_disguises():
    assert find_emails("info [at] palmview (dot) example") == ["info@palmview.example"]
    assert find_emails("bookings @ cedarpoint . example") == ["bookings@cedarpoint.example"]
    assert find_emails("write to us at hello@lagoon.example.\nOpening hours") == ["hello@lagoon.example"]
