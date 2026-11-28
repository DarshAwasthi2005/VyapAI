from app.services.orders import check_inventory, normalize_vernacular, parse_order_text


def test_parse_multi_item_single_order():
    # A whole shopping list in one message becomes ONE order with BOTH items.
    order = parse_order_text("Send 5 bags of sugar and 2 cartons of soap by 4 PM")
    assert len(order.line_items) == 2
    by_name = {li.item_name.lower(): li for li in order.line_items}
    sugar, soap = by_name["sugar"], by_name["soap"]
    assert (sugar.quantity, sugar.unit) == (5.0, "bags")
    assert (soap.quantity, soap.unit) == (2.0, "cartons")
    assert order.deadline_text == "4 PM"
    assert order.parse_confidence == "high"


def test_parse_comma_and_newline_separated_items():
    order = parse_order_text("3 kg sugar, 2 soap\n1 kg rice")
    by_name = {li.item_name.lower(): li for li in order.line_items}
    assert (by_name["sugar"].quantity, by_name["sugar"].unit) == (3.0, "kg")
    assert (by_name["soap"].quantity, by_name["soap"].unit) == (2.0, "units")  # bare qty, no unit
    assert (by_name["rice"].quantity, by_name["rice"].unit) == (1.0, "kg")


def test_parse_hinglish_multi_item():
    # "aur" = "and": the Hindi list connector joins items in one order.
    order = parse_order_text("3 kg chini aur 2 kg aata bhejo")
    by_name = {li.item_name.lower(): li for li in order.line_items}
    assert (by_name["sugar"].quantity, by_name["sugar"].unit) == (3.0, "kg")
    assert (by_name["flour"].quantity, by_name["flour"].unit) == (2.0, "kg")


def test_parse_multi_item_totals_priced_together():
    # End-to-end guarantee the caller cares about: one graph run prices the
    # WHOLE list (sugar ₹42/kg + soap ₹30/carton, demo catalog).
    from app.agents.graph_b2c import build_b2c_graph
    state = build_b2c_graph().invoke({
        "raw_message": "5 bags of sugar and 2 cartons of soap",
        "stock_levels": {"sugar": 100.0, "soap": 50.0},
        "unit_prices": {"sugar": 42.0, "soap": 30.0},
    })
    assert state["all_in_stock"] is True
    assert state["total_amount"] == 42.0 * 5 + 30.0 * 2
    assert len(state["line_items"]) == 2


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
