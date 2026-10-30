from app.services.orders import check_inventory, normalize_vernacular, parse_order_text


def test_parse_takes_only_first_item():
    # One entry at a time: anything after "and" is ignored.
    order = parse_order_text("Send 5 bags of sugar and 2 cartons of soap by 4 PM")
    assert len(order.line_items) == 1
    assert order.line_items[0].quantity == 5.0
    assert order.line_items[0].unit == "bags"
    assert "sugar" in order.line_items[0].item_name.lower()
    assert order.deadline_text == "4 PM"
    assert order.parse_confidence == "high"
    # The second item goes in as its own simple order entry.
    second = parse_order_text("2 cartons of soap")
    assert len(second.line_items) == 1
    assert second.line_items[0].item_name.lower().strip() == "soap"


def test_parse_single_item_no_deadline():
    order = parse_order_text("2 kg rice please")
    assert len(order.line_items) == 1
    assert order.line_items[0].quantity == 2.0
    assert order.line_items[0].unit == "kg"
    assert order.deadline_text is None


def test_parse_unmatched_order_flagged_low_confidence():
    order = parse_order_text("need 5 things urgently")
    assert order.parse_confidence == "low"
    assert order.line_items == []


def test_parse_plain_text_no_digits_is_high_confidence_empty():
    order = parse_order_text("hello, is the shop open today?")
    assert order.line_items == []
    assert order.parse_confidence == "high"


def test_parse_vernacular_hindi_voice_transcript():
    """What Vosk returns for "teen kilo cheeni bhej do" — Devanagari,
    no Latin characters at all. It must still become a real ordered item."""
    order = parse_order_text("तीन किलो चीनी भेज दो")
    assert len(order.line_items) == 1
    li = order.line_items[0]
    assert (li.quantity, li.unit, li.item_name) == (3.0, "kg", "sugar")
    assert order.parse_confidence == "high"


def test_parse_hinglish_typed_order():
    order = parse_order_text("2 bags chini bhejo")
    assert (order.line_items[0].quantity, order.line_items[0].unit,
            order.line_items[0].item_name) == (2.0, "bags", "sugar")
    # "do" means two before a unit ...
    assert parse_order_text("do kilo chini").line_items[0].quantity == 2.0
    # ... and is filler elsewhere ("bhej do").
    assert parse_order_text("teen kilo chini bhej do").line_items[0].quantity == 3.0


def test_parse_vernacular_deadline():
    order = parse_order_text("पाँच किलो आटा 4 बजे तक")
    assert (order.line_items[0].quantity, order.line_items[0].unit,
            order.line_items[0].item_name) == (5.0, "kg", "flour")
    assert order.deadline_text == "4"


def test_normalize_keeps_english_untouched():
    assert normalize_vernacular("Send 2 bags of sugar by 9 PM") == "Send 2 bags of sugar by 9 PM"


def test_inventory_check_in_stock():
    order = parse_order_text("3 bags of sugar")
    results = check_inventory(order, {"sugar": 10.0})
    assert results[0].in_stock
    assert results[0].available_qty == 10.0


def test_inventory_check_out_of_stock():
    order = parse_order_text("50 bags of sugar")
    results = check_inventory(order, {"sugar": 10.0})
    assert not results[0].in_stock


def test_inventory_check_unknown_item_treated_as_zero_stock():
    order = parse_order_text("4 boxes of gadgets")
    results = check_inventory(order, {"sugar": 10.0})
    assert not results[0].in_stock
    assert results[0].available_qty == 0.0
