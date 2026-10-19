from lead_engine.extract import extract_scripted, ground

PAGE = "Our principal partner, ESV Tobi Afolabi, leads the team. Call 0812 555 0123. Visit Aliyu Turaki House."


def test_grounded_fields_are_kept():
    ex = ground({"contact_name": {"value": "ESV Tobi Afolabi", "quote": "Our principal partner, ESV Tobi Afolabi"},
                 "phones": [{"value": "0812 555 0123", "quote": "Call 0812 555 0123"}]}, PAGE, "test")
    assert ex.contact_name == "ESV Tobi Afolabi"
    assert ex.phones == ["+2348125550123"]
    assert ex.dropped == []


def test_invented_quote_is_dropped():
    ex = ground({"emails": [{"value": "tobi@afolabi.example", "quote": "email tobi@afolabi.example"}]}, PAGE, "test")
    assert ex.emails == []
    assert ex.dropped[0]["why"] == "quote not found on the page"


def test_value_must_be_inside_its_quote():
    ex = ground({"phones": [{"value": "0812 555 9999", "quote": "Call 0812 555 0123"}]}, PAGE, "test")
    assert ex.phones == []
    assert ex.dropped[0]["why"] == "number not in its quote"


def test_a_building_is_not_a_person():
    ex = extract_scripted("Visit us at Aliyu Turaki House, Wuse II. Reach the front desk on 0809 555 0111.")
    assert ex.contact_name is None
    assert ex.phones == ["+2348095550111"]
