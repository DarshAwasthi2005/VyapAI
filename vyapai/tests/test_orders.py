from app.services.orders import check_inventory, parse_order_text


def test_parse_simple_order():
    order = parse_order_text("Send 5 bags of sugar and 2 cartons of soap by 4 PM")
    assert len(order.line_items) == 2
    assert order.line_items[0].quantity == 5.0
    assert order.line_items[0].unit == "bags"
    assert "sugar" in order.line_items[0].item_name.lower()
    assert order.line_items[1].item_name.lower().strip() == "soap"
    assert order.deadline_text == "4 PM"
    assert order.parse_confidence == "high"


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
